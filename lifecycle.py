"""Instance lifecycle: start, stop, and status for worker models."""

from __future__ import annotations

import json
import re
import shlex
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from . import state, transport
from .agents import remove_stale_temps, remove_subagents
from .catalog import MODELS, ModelSpec, model_by_key_optional, provider_for
from .opencode import add_provider, remove_provider
from .state import REMOTE_STATE_DIR
from .transport import Connection

_FREE_VRAM_PATTERN = re.compile(r"\(\s*(\d+)\s*MiB,\s*(\d+)\s*MiB free\s*\)")


def _port_in_use(port: int) -> bool:
    """Return whether a TCP port is already listening on this host."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def allocate_port(model: ModelSpec, sessions: Sequence[dict[str, object]]) -> int:
    """Return the lowest free port at or above the model's port base.

    Nothing bounds the number of instances: ports are taken until a free one is found. A port is
    skipped when another recorded instance already owns it or when it is already listening here.
    """
    taken = {session.get("port") for session in sessions if session.get("key") == model.key}
    port = model.port_base
    while True:
        if port not in taken and not _port_in_use(port):
            return port
        port += 1


def instance_index(sessions: Sequence[dict[str, object]], model: ModelSpec) -> int:
    """Return one plus the highest instance index already used for this model."""
    indexes = [
        session.get("index")
        for session in sessions
        if session.get("key") == model.key and isinstance(session.get("index"), int)
    ]
    return (max(indexes) + 1) if indexes else 1


def plan_start(model: ModelSpec, count: int) -> list[dict[str, object]]:
    """Return the instance, port, and provider each successive launch would use, without starting.

    The plan folds each planned instance back into the session list so the real allocation
    helpers resolve the same indexes and ports ``start_model`` would.
    """
    sessions = state.active_sessions()
    plan: list[dict[str, object]] = []
    for _ in range(count):
        instance = instance_index(sessions, model)
        port = allocate_port(model, sessions)
        plan.append({"instance": instance, "port": port, "provider": provider_for(model, instance)})
        sessions = [*sessions, {"key": model.key, "index": instance, "port": port}]
    return plan


def launch_script(
    connection: Connection,
    model: ModelSpec,
    instance: int,
    port: int,
    context: int,
    reasoning: str,
) -> str:
    """Build the remote script that starts one detached llama-server instance on the worker.

    The reasoning level becomes the runtime default: ``--reasoning off`` disables thinking, while
    an effort level is passed to ``--reasoning-effort`` with ``--reasoning on``. Models whose
    runtime lacks these flags (older builds) get no reasoning arguments at all; sending them fails
    the launch.

    ``--n-gpu-layers`` is left unset when the model's ``gpu_layers`` is ``auto``, so llama.cpp's
    ``--fit`` can choose how many layers fit the requested context in the worker's free VRAM
    instead of committing every layer and spilling into shared memory. An explicit ``all`` or
    layer count is passed through verbatim.
    """
    runtime = f"$HOME/.local/src/llama.cpp-{model.runtime_revision}/build/bin/llama-server"
    name = f"{model.key}-{instance}"
    log = f"{REMOTE_STATE_DIR}/{name}.log"
    pidfile = f"{REMOTE_STATE_DIR}/{name}.pid"
    reasoning_args: list[str] = []
    if model.supports_reasoning:
        if reasoning == "off":
            reasoning_args = ["--reasoning", "off"]
        else:
            reasoning_args = ["--reasoning", "on", "--reasoning-effort", reasoning]
    args = [
        "$runtime",
        "--model",
        '"$model"',
        "--alias",
        model.alias,
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--ctx-size",
        str(context),
        "--parallel",
        "1",
    ]
    if model.gpu_layers != "auto":
        args += ["--n-gpu-layers", model.gpu_layers]
    args += [
        "--jinja",
        "--offline",
        "--no-webui",
        *reasoning_args,
        *model.extra_args,
    ]
    return "\n".join(
        [
            "set -euo pipefail",
            'export PATH="$HOME/.local/bin:$PATH"',
            f'runtime="{runtime}"',
            'test -x "$runtime"',
            f'model="{connection.worker_root}/{model.artifact}"',
            'test -s "$model"',
            f'mkdir -p "{REMOTE_STATE_DIR}"',
            f'chmod 700 "{REMOTE_STATE_DIR}"',
            f'nohup env HSA_ENABLE_DXG_DETECTION=1 {" ".join(args)} > "{log}" 2>&1 </dev/null &',
            f'echo $! > "{pidfile}"',
            "sleep 1",
        ]
    )


def stop_script(model: ModelSpec, instance: int) -> str:
    """Build the remote script that terminates one instance's llama-server."""
    pidfile = f"{REMOTE_STATE_DIR}/{model.key}-{instance}.pid"
    return "\n".join(
        [
            "set -uo pipefail",
            f'pidfile="{pidfile}"',
            'if test -f "$pidfile"; then',
            '  pid="$(cat "$pidfile")"',
            '  if kill -0 "$pid" 2>/dev/null; then start=1; else start=0; fi',
            '  kill "$pid" 2>/dev/null || true',
            "  i=0",
            '  while kill -0 "$pid" 2>/dev/null; do',
            "    i=$((i+1))",
            '    if test "$i" -ge 30; then break; fi',
            "    sleep 1",
            "  done",
            '  rm -f "$pidfile"',
            '  echo "stopped(was_running=$start) pid=$pid"',
            "else",
            '  echo "no pidfile"',
            "fi",
        ]
    )


def health_probe(port: int, timeout: float = 3.0) -> tuple[bool, str]:
    """Probe the forwarded loopback health endpoint once and return ``(ready, detail)``."""
    url = f"http://127.0.0.1:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            if response.status == 200:
                return True, "ready"
            return False, f"HTTP {response.status}"
    except (urllib.error.URLError, OSError) as exc:
        return False, str(exc)


def wait_healthy(connection: Connection, model: ModelSpec, instance: int, port: int) -> None:
    """Poll the forwarded loopback health endpoint until the instance reports healthy."""
    deadline = time.monotonic() + connection.health_timeout
    last = "no response"
    while time.monotonic() < deadline:
        ready, detail = health_probe(port, timeout=5.0)
        if ready:
            return
        last = detail
        time.sleep(2)
    raise SystemExit(
        f"instance {model.key}-{instance} did not become healthy on port {port} "
        f"within {connection.health_timeout}s ({last}); "
        f"check {REMOTE_STATE_DIR}/{model.key}-{instance}.log"
    )


def estimated_vram_mib(model: ModelSpec, context: int) -> float:
    """Return the estimated full-offload VRAM footprint for one instance and context."""
    return (
        model.weight_mib
        + model.fixed_kv_mib
        + model.kv_mib_per_1k_context * (context / 1000.0)
        + model.compute_mib
    )


def remote_free_vram_mib(connection: Connection, model: ModelSpec) -> int | None:
    """Return the worker's currently free VRAM in MiB, or None when it cannot be read.

    This is a diagnostic only, so a failed or unrecognised device listing returns None rather than
    aborting the launch; the fit warning then degrades to the static estimate.
    """
    runtime = f"$HOME/.local/src/llama.cpp-{model.runtime_revision}/build/bin/llama-server"
    result = subprocess.run(
        transport.ssh_argv(
            connection,
            "set -uo pipefail\n"
            f'runtime="{runtime}"\n'
            'test -x "$runtime" || { echo "runtime missing"; exit 0; }\n'
            'HSA_ENABLE_DXG_DETECTION=1 "$runtime" --list-devices 2>&1 || true',
        ),
        stdin=subprocess.DEVNULL,
        capture_output=True,
    )
    if result.returncode != 0:
        return None
    text = result.stdout.decode("utf-8", errors="replace")
    matches = _FREE_VRAM_PATTERN.findall(text)
    if not matches:
        return None
    return int(matches[0][1])


def warn_if_vram_short(connection: Connection, model: ModelSpec, context: int) -> None:
    """Warn (never block) when the estimated footprint exceeds the currently free VRAM."""
    estimate = estimated_vram_mib(model, context)
    free = remote_free_vram_mib(connection, model)
    if free is None:
        print(f"  warning: could not read free VRAM; estimated need ~{estimate:.0f} MiB")
        return
    if estimate > free:
        print(
            f"  warning: estimated {estimate:.0f} MiB needed but only {free} MiB free; "
            "the model may spill into shared memory and run slowly"
        )
    else:
        print(f"  vram      : ~{estimate:.0f} MiB needed, {free} MiB free")


def start_model(connection: Connection, model: ModelSpec, context: int, reasoning: str) -> None:
    """Start one model instance on the worker and forward it to this host, then return."""
    if context not in model.context_sizes:
        allowed = ", ".join(str(value) for value in model.context_sizes)
        raise SystemExit(f"context {context} not allowed for {model.key}; choose: {allowed}")
    if reasoning not in model.reasoning_levels:
        allowed = ", ".join(model.reasoning_levels)
        raise SystemExit(f"reasoning {reasoning!r} not allowed for {model.key}; choose: {allowed}")

    for removed in remove_stale_temps():
        print(f"  stale temp        : {removed} removed", flush=True)

    sessions = state.active_sessions()
    instance = instance_index(sessions, model)
    port = allocate_port(model, sessions)

    warn_if_vram_short(connection, model, context)
    provider = add_provider(model, instance, port, context, reasoning)
    keeper_log = state.STATE_DIR / f"{model.key}-{instance}.keeper.log"
    keeper_pid = transport.start_keeper(connection, port, keeper_log)
    try:
        transport.run_remote(
            connection, launch_script(connection, model, instance, port, context, reasoning)
        )
        wait_healthy(connection, model, instance, port)
    except BaseException:
        try:
            transport.run_remote(connection, stop_script(model, instance))
        except BaseException as cleanup_error:
            print(
                f"  warning: could not stop {model.key}-{instance} after a failed start: "
                f"{cleanup_error}",
                flush=True,
            )
        state.stop_pid(keeper_pid, connection.connect_timeout)
        remove_provider(provider)
        if not state.active_sessions():
            transport.terminate_wsl(connection)
        raise

    sessions.append(
        {
            "key": model.key,
            "index": instance,
            "provider": provider,
            "alias": model.alias,
            "port": port,
            "context": context,
            "reasoning": reasoning,
            "keeper_pid": keeper_pid,
            "agents": [],
            "ssh_target": connection.ssh_target,
            "wsl_distro": connection.wsl_distro,
            "worker_root": connection.worker_root,
            "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    )
    state.save_state(sessions)
    print(
        f"Started {model.key} instance {instance} (context {context}, reasoning {reasoning}) "
        f"on 127.0.0.1:{port}",
        flush=True,
    )
    print(f"  opencode model id : {provider}/{model.alias}", flush=True)
    print(f"  create subagent   : create-subagent --provider {provider}", flush=True)
    print(f"  logs              : logs --model {model.key} --instance {instance}", flush=True)


def stop_session(connection: Connection, session: dict[str, object]) -> None:
    """Stop one active instance's remote server, keeper, provider entry, and stamped subagents.

    The WSL distribution is terminated only once no other instance remains, so stopping one of
    several running instances never disrupts the others.
    """
    for removed in remove_stale_temps():
        print(f"  stale temp        : {removed} removed", flush=True)

    key = str(session["key"])
    index = session.get("index")
    model = model_by_key_optional(key)
    if model is not None and isinstance(index, int):
        transport.run_remote(connection, stop_script(model, index))
    state.stop_pid(session.get("keeper_pid"), connection.connect_timeout)  # type: ignore[arg-type]
    provider = session.get("provider")
    if isinstance(provider, str):
        remove_provider(provider)

    agents = session.get("agents")
    if isinstance(agents, list):
        for removed in remove_subagents(agents):
            print(f"  subagent          : {removed} removed", flush=True)

    remaining = [
        item for item in state.load_state() if item.get("provider") != session.get("provider")
    ]
    state.save_state(remaining)
    print(f"Stopped {provider if isinstance(provider, str) else key}", flush=True)
    if not state.active_sessions():
        transport.terminate_wsl(connection)


def _keeper_log_owner(name: str) -> ModelSpec | None:
    """Return the catalog model a ``<model key>-<instance>`` log stem belongs to, or None."""
    for model in MODELS:
        prefix = f"{model.key}-"
        if not name.startswith(prefix):
            continue
        suffix = name[len(prefix) :]
        if suffix.isdigit() and int(suffix) >= 1:
            return model
    return None


def stale_keeper_logs(
    sessions: Sequence[dict[str, object]] | None = None,
) -> list[Path]:
    """Return local keeper logs for instances that have no entry in the recorded state."""
    if sessions is None:
        sessions = state.load_state()
    known = {
        f"{session.get('key')}-{session.get('index')}"
        for session in sessions
        if isinstance(session.get("key"), str) and isinstance(session.get("index"), int)
    }
    if not state.STATE_DIR.is_dir():
        return []
    stale: list[Path] = []
    for path in sorted(state.STATE_DIR.glob("*.keeper.log")):
        name = path.name[: -len(".keeper.log")]
        if name in known or _keeper_log_owner(name) is None:
            continue
        stale.append(path)
    return stale


def _clean_remote_logs(connection: Connection, known: Sequence[str], apply: bool) -> list[str]:
    """Remove remote logs and pidfiles whose instance is not in ``known``; return action lines."""
    verb = "removed" if apply else "would remove"
    remove_command = "rm -f" if apply else "true"
    lines = [
        "set -uo pipefail",
        f'dir="{REMOTE_STATE_DIR}"',
        'test -d "$dir" || exit 0',
        'for f in "$dir"/*.log "$dir"/*.pid; do',
        '  test -e "$f" || continue',
        '  base="$(basename "$f")"',
        '  name="${base%.log}"',
        '  name="${name%.pid}"',
    ]
    if known:
        pattern_args = " ".join(f"-e {shlex.quote(name)}" for name in sorted(known))
        lines.append(f"  if printf '%s\\n' \"$name\" | grep -Fxq {pattern_args}; then continue; fi")
    lines += [
        f'  echo "{verb} remote $base"',
        f'  {remove_command} "$f"',
        "done",
    ]
    result = transport.run_remote(connection, "\n".join(lines))
    text = result.stdout.decode("utf-8", errors="replace").strip()
    return [line for line in text.splitlines() if line]


def clean_command(connection: Connection, apply: bool) -> int:
    """Remove stale local keeper logs and remote logs/pidfiles for instances with no state entry."""
    sessions = state.load_state()
    known = [
        f"{session.get('key')}-{session.get('index')}"
        for session in sessions
        if isinstance(session.get("key"), str) and isinstance(session.get("index"), int)
    ]
    actions: list[str] = []
    for path in stale_keeper_logs(sessions):
        if apply:
            path.unlink()
        actions.append(f"{'removed' if apply else 'would remove'} local {path.name}")
    actions += _clean_remote_logs(connection, known, apply)

    prefix = "clean applied" if apply else "clean dry-run"
    print(f"{prefix}: {len(actions)} action(s)")
    for action in actions:
        print(f"  - {action}")
    if not apply:
        print("re-run with --execute to apply")
    return 0


def _uptime_seconds(started: object) -> int | None:
    """Return the whole seconds since the recorded ``started`` timestamp, or None if unreadable."""
    if not isinstance(started, str):
        return None
    try:
        then = datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    return max(0, int((datetime.now(UTC) - then).total_seconds()))


def _format_uptime(seconds: int | None) -> str:
    """Render an uptime in a compact human form, or ``unknown`` when it could not be computed."""
    if seconds is None:
        return "unknown"
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def status_command(
    connection: Connection | None = None, as_json: bool = False, vram: bool = False
) -> None:
    """Report active instances, their health, uptime, stamps, and optionally worker free VRAM.

    ``--vram`` shells out over SSH, so it is opt-in and only probes the distinct runtime revisions
    in use once each; the default path is entirely local.
    """
    sessions = state.active_sessions()
    vram_by_revision: dict[str, int | None] = {}
    if vram and connection is not None:
        revisions: dict[str, ModelSpec] = {}
        for session in sessions:
            key = session.get("key")
            model = model_by_key_optional(str(key)) if isinstance(key, str) else None
            if model is not None:
                revisions.setdefault(model.runtime_revision, model)
        for revision, model in revisions.items():
            vram_by_revision[revision] = remote_free_vram_mib(connection, model)

    records: list[dict[str, object]] = []
    for session in sessions:
        port = session.get("port")
        health = "unknown"
        if isinstance(port, int):
            ready, detail = health_probe(port)
            health = "ready" if ready else f"down ({detail})"
        recorded = session.get("agents")
        subagents = [Path(str(raw)).name for raw in recorded] if isinstance(recorded, list) else []
        records.append(
            {
                "provider": session.get("provider"),
                "key": session.get("key"),
                "context": session.get("context"),
                "reasoning": session.get("reasoning"),
                "port": port,
                "health": health,
                "uptime_seconds": _uptime_seconds(session.get("started")),
                "subagents": subagents,
            }
        )

    if as_json:
        payload: dict[str, object] = {"sessions": records}
        if vram and connection is not None:
            payload["vram_free_mib"] = vram_by_revision
        print(json.dumps(payload, indent=2))
        return

    if not records:
        print("No active models.")
        return
    print("Active instances:")
    for index, record in enumerate(records, start=1):
        print(
            f"  {index}) {record['provider']} ({record['key']} "
            f"context {record['context']}, reasoning {record['reasoning']}, "
            f"port {record['port']})"
        )
        print(f"     health    : {record['health']}")
        print(f"     uptime    : {_format_uptime(record['uptime_seconds'])}")
        stamps = record["subagents"]
        assert isinstance(stamps, list)
        print(f"     subagents : {', '.join(stamps) if stamps else '(none)'}")
    if vram and connection is not None:
        for revision, free in vram_by_revision.items():
            shown = f"{free} MiB free" if free is not None else "unavailable"
            print(f"  vram {revision[:12]}: {shown}")
