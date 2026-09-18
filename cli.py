"""Interactive menu and flag-driven command dispatch.

Bare invocation prints the action menu (start / stop / create subagent / status / quit) and then
runs the chosen action exactly as the flag-driven path does. Existing flags still work for
scripting. Connection defaults come from ``config.toml``; an explicit flag overrides an
environment variable, which overrides the config value.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from . import diagnostics, state, transport
from .agents import (
    agent_path,
    remove_subagents,
    resolve_agent_path,
    stamp_subagent,
    subagent_specs,
    template_description,
)
from .catalog import (
    CONNECTION,
    MODELS,
    ModelSpec,
    SubagentSpec,
    model_by_key,
    provider_for,
)
from .identity import ENV_PREFIX
from .lifecycle import (
    clean_command,
    estimated_vram_mib,
    plan_start,
    start_model,
    status_command,
    stop_session,
)
from .reconcile import reconcile_command
from .transport import Connection


def _prompt_index(prompt: str, count: int, default: int | None = None) -> int:
    """Read a 1-based selection from the terminal, or fail clearly when not interactive.

    When ``default`` is given, an empty answer accepts it.
    """
    if not sys.stdin.isatty():
        raise SystemExit(f"{prompt} requires a terminal; pass explicit flags instead")
    suffix = f" (default {default})" if default is not None else ""
    while True:
        raw = input(f"{prompt} [1-{count}]{suffix}: ").strip()
        if not raw and default is not None:
            return default
        if raw.isdigit() and 1 <= int(raw) <= count:
            return int(raw)
        print(f"Enter a number between 1 and {count}.")


def _print_models() -> None:
    """Print the numbered model catalog."""
    print("Available models:")
    for index, model in enumerate(MODELS, start=1):
        print(f"  {index}) {model.key}")


def _print_active_sessions(sessions: Sequence[dict[str, object]]) -> None:
    """Print the numbered active-instance list shared by stop and create subagent."""
    for index, session in enumerate(sessions, start=1):
        print(
            f"  {index}) {session.get('provider')} ({session.get('key')} "
            f"context {session.get('context')}, reasoning {session.get('reasoning')}, "
            f"port {session.get('port')})"
        )


def select_model() -> ModelSpec:
    """Interactively select a model from the catalog, skipping the prompt when only one exists."""
    if len(MODELS) == 1:
        return MODELS[0]
    _print_models()
    return MODELS[_prompt_index("Select model", len(MODELS)) - 1]


def select_context(model: ModelSpec) -> int:
    """Select a context size, skipping the prompt when only one is offered."""
    if len(model.context_sizes) == 1:
        return model.context_sizes[0]
    print(f"Context sizes for {model.key}:")
    for index, value in enumerate(model.context_sizes, start=1):
        marker = " (default)" if value == model.default_context else ""
        print(f"  {index}) {value}{marker}")
    default_index = model.context_sizes.index(model.default_context) + 1
    return model.context_sizes[
        _prompt_index("Select context", len(model.context_sizes), default=default_index) - 1
    ]


def select_reasoning(model: ModelSpec) -> str:
    """Select a reasoning level, skipping the prompt when only one is offered."""
    if len(model.reasoning_levels) == 1:
        return model.reasoning_levels[0]
    print(f"Reasoning levels for {model.key}:")
    for index, value in enumerate(model.reasoning_levels, start=1):
        marker = " (default)" if value == model.default_reasoning else ""
        print(f"  {index}) {value}{marker}")
    default_index = model.reasoning_levels.index(model.default_reasoning) + 1
    return model.reasoning_levels[
        _prompt_index("Select reasoning", len(model.reasoning_levels), default=default_index) - 1
    ]


def prompt_action() -> str:
    """Ask which action to run, or fail clearly when there is no terminal to ask on."""
    if not sys.stdin.isatty():
        raise SystemExit(
            "no action given; pass start|stop|status|create-subagent|doctor|logs|"
            "subagents|models|where|reconcile|restart|clean"
        )
    options = ["start", "stop", "create subagent", "status", "doctor", "quit"]
    print("What do you want to do?")
    for index, label in enumerate(options, start=1):
        print(f"  {index}) {label}")
    choice = options[_prompt_index("Select action", len(options)) - 1]
    if choice == "quit":
        raise SystemExit(0)
    return "create-subagent" if choice == "create subagent" else choice


def select_session(
    sessions: Sequence[dict[str, object]], provider: str | None = None
) -> dict[str, object]:
    """Resolve the active instance named by ``provider`` or prompt for one."""
    if provider is not None:
        matches = [session for session in sessions if session.get("provider") == provider]
        if not matches:
            raise SystemExit(f"provider {provider!r} is not active")
        return matches[0]
    if len(sessions) == 1:
        return sessions[0]
    print("Active instances:")
    _print_active_sessions(sessions)
    return sessions[_prompt_index("Select instance", len(sessions)) - 1]


def select_template() -> SubagentSpec:
    """Select one manifest subagent template, skipping the prompt when only one exists."""
    specs = subagent_specs()
    if not specs:
        raise SystemExit("no subagent templates are configured")
    if len(specs) == 1:
        return specs[0]
    print("Subagent templates:")
    for index, spec in enumerate(specs, start=1):
        print(f"  {index}) {spec.name} - {template_description(spec)}")
    return specs[_prompt_index("Select template", len(specs)) - 1]


def models_command(as_json: bool = False) -> int:
    """Print the configured model catalog with context, reasoning, and VRAM estimates."""
    records = [
        {
            "key": model.key,
            "display": model.display,
            "alias": model.alias,
            "slug": model.slug,
            "port_base": model.port_base,
            "context_sizes": list(model.context_sizes),
            "default_context": model.default_context,
            "reasoning_levels": list(model.reasoning_levels),
            "default_reasoning": model.default_reasoning,
            "supports_reasoning": model.supports_reasoning,
            "tool_call": model.tool_call,
            "estimated_vram_mib": round(estimated_vram_mib(model, model.default_context), 1),
        }
        for model in MODELS
    ]
    if as_json:
        print(json.dumps({"models": records}, indent=2))
        return 0
    print("Configured models:")
    for index, record in enumerate(records, start=1):
        print(f"  {index}) {record['key']} - {record['display']}")
        contexts = ", ".join(str(value) for value in record["context_sizes"])
        reasoning = ", ".join(record["reasoning_levels"])
        print(f"     context   : {contexts} (default {record['default_context']})")
        print(f"     reasoning : {reasoning} (default {record['default_reasoning']})")
        print(
            f"     alias     : {record['alias']} | port base {record['port_base']} | "
            f"vram est ~{record['estimated_vram_mib']:.0f} MiB @ {record['default_context']}"
        )
        print(
            f"     tool call : {'yes' if record['tool_call'] else 'no'} | "
            f"reasoning: {'yes' if record['supports_reasoning'] else 'no'}"
        )
    return 0


def _record_stamp(session: dict[str, object], destinations: Path | Sequence[Path]) -> None:
    """Append one or more stamped files to the instance's entry in the private state, then persist."""
    paths = [destinations] if isinstance(destinations, Path) else list(destinations)
    sessions = state.load_state()
    for item in sessions:
        if item.get("provider") != session.get("provider"):
            continue
        agents = item.get("agents")
        if not isinstance(agents, list):
            agents = []
        agents.extend(str(path) for path in paths)
        item["agents"] = agents
        state.save_state(sessions)
        return
    raise SystemExit("instance vanished from state before its subagent could be recorded")


def create_subagent_command(args: argparse.Namespace) -> None:
    """Stamp one or all configured templates with a live instance's model and write agent files."""
    sessions = state.active_sessions()
    if not sessions:
        raise SystemExit("no active models; start one before creating a subagent")
    if args.all and args.template:
        raise SystemExit("--all stamps every template; do not pass --template")
    if args.all:
        specs = subagent_specs()
    elif args.template:
        matches = [spec for spec in subagent_specs() if spec.name == args.template]
        if not matches:
            known = ", ".join(spec.name for spec in subagent_specs())
            raise SystemExit(f"unknown template {args.template!r}; known templates: {known}")
        specs = (matches[0],)
    else:
        specs = (select_template(),)
    session = select_session(sessions, args.provider)

    key = session.get("key")
    index = session.get("index")
    if not isinstance(key, str) or not isinstance(index, int):
        raise SystemExit(f"active instance is malformed in state: {session!r}")
    model = model_by_key(key)
    model_id = f"{provider_for(model, index)}/{model.alias}"

    if args.dry_run:
        print("Dry run; nothing was written or recorded.")
        print(f"  model id          : {model_id}")
        for spec in specs:
            print(f"  would stamp       : {agent_path(spec, model, index)}")
        return

    destinations = [stamp_subagent(spec, model, index) for spec in specs]
    _record_stamp(session, destinations)
    print(f"Stamped {len(destinations)} subagent(s) for {session.get('provider')} ({model.alias})")
    for destination in destinations:
        print(f"  subagent          : {destination}")


def remove_subagent_command(args: argparse.Namespace) -> None:
    """Remove one stamped subagent file and drop it from the owning instance's state entry."""
    if not args.provider:
        raise SystemExit("remove-subagent requires --provider")
    if args.template and args.file:
        raise SystemExit("pass either --template or --file, not both")
    if not args.template and not args.file:
        raise SystemExit("remove-subagent requires --template or --file")

    sessions = state.load_state()
    matches = [session for session in sessions if session.get("provider") == args.provider]
    if not matches:
        raise SystemExit(f"provider {args.provider!r} is not recorded in state")
    session = matches[0]

    if args.template:
        key = session.get("key")
        index = session.get("index")
        if not isinstance(key, str) or not isinstance(index, int):
            raise SystemExit(f"recorded instance is malformed: {session!r}")
        model = model_by_key(key)
        specs = [spec for spec in subagent_specs() if spec.name == args.template]
        if not specs:
            known = ", ".join(spec.name for spec in subagent_specs())
            raise SystemExit(f"unknown template {args.template!r}; known templates: {known}")
        path = agent_path(specs[0], model, index)
    else:
        path = resolve_agent_path(args.file)

    recorded = session.get("agents")
    recorded_paths = [str(entry) for entry in recorded] if isinstance(recorded, list) else []
    recorded_by_resolved = {Path(entry).resolve(): entry for entry in recorded_paths}
    if path.resolve() not in recorded_by_resolved:
        raise SystemExit(f"{path} is not recorded for provider {args.provider!r}")
    recorded_path = recorded_by_resolved[path.resolve()]

    removed = remove_subagents([recorded_path])
    if not removed:
        raise SystemExit(
            f"{recorded_path} is recorded for {args.provider!r} but does not exist on disk"
        )
    session["agents"] = [entry for entry in recorded_paths if entry != recorded_path]
    state.save_state(sessions)
    print(f"Removed subagent {recorded_path}")


def prune_subagents_command(args: argparse.Namespace) -> None:
    """Delete (or, by default, list) stamped files whose provider has no state entry."""
    paths = diagnostics.prune_subagents(args.execute)
    if not paths:
        print("No orphan stamps.")
        return
    verb = "Removed" if args.execute else "Would remove"
    for path in paths:
        print(f"  {verb}: {path}")
    if not args.execute:
        print(f"{len(paths)} orphan stamp(s); re-run with --execute to delete.")


def logs_command(connection: Connection, args: argparse.Namespace) -> None:
    """Show the remote llama-server log and the local keeper log for one instance.

    Both files are addressed directly, so a failed start (which leaves no state entry) can still
    be inspected with ``--model`` and ``--instance``. The remote read is a one-shot ``tail``.
    """
    if args.lines < 1:
        raise SystemExit("--lines must be a positive integer")
    if args.model:
        model = model_by_key(args.model)
        instance = args.instance if args.instance is not None else 1
    else:
        sessions = state.active_sessions()
        if not sessions:
            raise SystemExit("logs requires --model when no instances are active")
        session = select_session(sessions, args.provider)
        key = session.get("key")
        index = session.get("index")
        if not isinstance(key, str) or not isinstance(index, int):
            raise SystemExit(f"active instance is malformed: {session!r}")
        model = model_by_key(key)
        instance = args.instance if args.instance is not None else index

    name = f"{model.key}-{instance}"
    remote_log = f"{state.REMOTE_STATE_DIR}/{name}.log"
    print(f"Remote log ({name} on {connection.ssh_target}):")
    script = "\n".join(
        [
            "set -uo pipefail",
            f'log="{remote_log}"',
            'if test -f "$log"; then',
            f'  tail -n {args.lines} "$log"',
            "else",
            '  echo "__MISSING__"',
            "fi",
        ]
    )
    result = transport.run_remote(connection, script)
    text = result.stdout.decode("utf-8", errors="replace").rstrip("\n")
    if not text or text == "__MISSING__":
        print(f"  remote log not found: {remote_log}")
    else:
        print(text)

    keeper_log = state.STATE_DIR / f"{name}.keeper.log"
    print(f"Local keeper log ({keeper_log}):")
    if keeper_log.is_file() and not keeper_log.is_symlink():
        lines = keeper_log.read_text(encoding="utf-8", errors="replace").splitlines()
        print("\n".join(lines[-args.lines :]) if lines else "  (empty)")
    else:
        print("  local keeper log not found")


def restart_command(connection: Connection, args: argparse.Namespace) -> None:
    """Stop one active instance and start it again with the same model/context/reasoning."""
    sessions = state.active_sessions()
    if not sessions:
        raise SystemExit("no active models to restart")
    session = select_session(sessions, args.provider)
    key = session.get("key")
    context = session.get("context")
    reasoning = session.get("reasoning")
    if not isinstance(key, str) or not isinstance(context, int) or not isinstance(reasoning, str):
        raise SystemExit(f"active instance is malformed: {session!r}")
    model = model_by_key(key)
    provider = session.get("provider")
    stop_session(connection, session)
    start_model(connection, model, context, reasoning)
    print(
        f"Restarted {provider}; the new instance uses the next index and a new provider id.",
        flush=True,
    )


def _stop_selected(
    connection: Connection, sessions: Sequence[dict[str, object]], args: argparse.Namespace
) -> None:
    """Resolve which active instances to stop from --provider/--all/--model/--instance or a prompt."""
    if args.provider and args.all:
        raise SystemExit("pass either --provider or --all, not both")
    if args.provider:
        matches = [session for session in sessions if session.get("provider") == args.provider]
        if not matches:
            raise SystemExit(f"provider {args.provider!r} is not active")
        stop_session(connection, matches[0])
        return
    if args.all:
        for session in sessions:
            stop_session(connection, session)
        return
    if args.model:
        matches = [session for session in sessions if session.get("key") == args.model]
        if not matches:
            raise SystemExit(f"model {args.model!r} is not active")
        if args.instance is not None:
            selected = [s for s in matches if s.get("index") == args.instance]
            if not selected:
                raise SystemExit(f"{args.model!r} instance {args.instance} is not active")
            stop_session(connection, selected[0])
            return
        if len(matches) > 1:
            indexes = ", ".join(str(s.get("index")) for s in matches)
            raise SystemExit(
                f"{args.model!r} has several active instances ({indexes}); pass --instance"
            )
        stop_session(connection, matches[0])
        return
    if not sys.stdin.isatty():
        raise SystemExit("stop requires a terminal; pass --model or --all instead")
    print("Active instances:")
    _print_active_sessions(sessions)
    stop_session(connection, sessions[_prompt_index("Select instance to stop", len(sessions)) - 1])


def _string_setting(flag: str | None, env_var: str, fallback: str) -> str:
    """Resolve a string setting: explicit flag, else environment, else config value."""
    if flag is not None:
        return flag
    return os.environ.get(env_var, fallback)


def _resolve_connection(args: argparse.Namespace) -> Connection:
    """Resolve connection identity from flags over environment over config.toml."""
    return Connection(
        ssh_target=_string_setting(
            args.ssh_target, f"{ENV_PREFIX}SSH_TARGET", CONNECTION.ssh_target
        ),
        wsl_distro=_string_setting(
            args.wsl_distro, f"{ENV_PREFIX}WSL_DISTRO", CONNECTION.wsl_distro
        ),
        worker_root=_string_setting(
            args.worker_root, f"{ENV_PREFIX}WORKER_ROOT", CONNECTION.worker_root
        ),
        connect_timeout=(
            args.connect_timeout if args.connect_timeout is not None else CONNECTION.connect_timeout
        ),
        command_timeout=CONNECTION.command_timeout,
        health_timeout=CONNECTION.health_timeout,
    )


EPILOG = """examples:
  python3 <tool-dir> models --json
  python3 <tool-dir> start --model <model-key> --context 32768 --reasoning off
  python3 <tool-dir> start --model <model-key> --dry-run
  python3 <tool-dir> status --json
  python3 <tool-dir> doctor
  python3 <tool-dir> where
  python3 <tool-dir> logs --model <model-key> --lines 200
  python3 <tool-dir> create-subagent --provider local-<model-key> --all
  python3 <tool-dir> reconcile --execute
  python3 <tool-dir> clean --execute
"""


def build_parser() -> argparse.ArgumentParser:
    """Build the optional flag-driven CLI; every action is also reachable from the menu."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG,
    )
    parser.add_argument(
        "action",
        nargs="?",
        choices=(
            "start",
            "stop",
            "status",
            "create-subagent",
            "doctor",
            "verify",
            "logs",
            "subagents",
            "remove-subagent",
            "prune-subagents",
            "reconcile",
            "restart",
            "models",
            "where",
            "clean",
        ),
        default=None,
        help="lifecycle action; omit to get the interactive menu",
    )
    parser.add_argument("--model", help="model key; skips the interactive model list")
    parser.add_argument(
        "--context", type=int, help="context size; skips the interactive context list"
    )
    parser.add_argument("--reasoning", help="reasoning level; skips the interactive reasoning list")
    parser.add_argument("--instance", type=int, help="instance number for stop/logs")
    parser.add_argument(
        "--all",
        action="store_true",
        help="with stop: stop every active instance; with create-subagent: stamp every template",
    )
    parser.add_argument("--template", help="with create-subagent/remove-subagent: template name")
    parser.add_argument("--file", help="with remove-subagent: stamped agent file path")
    parser.add_argument("--provider", help="active provider id for stop/create/remove/restart")
    parser.add_argument(
        "--json", action="store_true", help="with status/doctor/models/where: print JSON"
    )
    parser.add_argument(
        "--vram", action="store_true", help="with status: also read worker free VRAM over SSH"
    )
    parser.add_argument(
        "--lines", type=int, default=100, help="with logs: lines per source (default 100)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "with create-subagent/start: show the target without writing or starting; "
            "default for reconcile/prune-subagents/clean"
        ),
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="with reconcile/prune-subagents/clean: apply the reported changes",
    )
    parser.add_argument(
        "--count", type=int, default=1, help="with start: how many instances to launch (serial)"
    )
    parser.add_argument("--ssh-target", default=None)
    parser.add_argument("--wsl-distro", default=None)
    parser.add_argument("--worker-root", default=None)
    parser.add_argument("--connect-timeout", type=int, default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse one action (or ask for it) and run the instance lifecycle or subagent stamping."""
    args = build_parser().parse_args(argv)
    connection = _resolve_connection(args)

    action = args.action if args.action is not None else prompt_action()

    if action == "status":
        status_command(connection, as_json=args.json, vram=args.vram)
        return 0

    if action == "models":
        return models_command(as_json=args.json)

    if action == "where":
        return diagnostics.where_command(as_json=args.json)

    if action in ("doctor", "verify"):
        return diagnostics.doctor_command(as_json=args.json)

    if action == "clean":
        return clean_command(connection, apply=args.execute)

    if action == "logs":
        logs_command(connection, args)
        return 0

    if action == "subagents":
        diagnostics.subagents_report()
        return 0

    if action == "remove-subagent":
        remove_subagent_command(args)
        return 0

    if action == "prune-subagents":
        prune_subagents_command(args)
        return 0

    if action == "reconcile":
        return reconcile_command(connection, apply=args.execute)

    if action == "start":
        if args.count < 1:
            raise SystemExit("--count must be a positive integer")
        model = model_by_key(args.model) if args.model else select_model()
        context = args.context if args.context is not None else select_context(model)
        reasoning = args.reasoning if args.reasoning is not None else select_reasoning(model)
        if args.dry_run:
            estimate = estimated_vram_mib(model, context)
            print("Dry run; nothing was started.")
            for item in plan_start(model, args.count):
                print(
                    f"  {model.key} instance {item['instance']} -> provider {item['provider']}, "
                    f"port {item['port']}, model id {item['provider']}/{model.alias}, "
                    f"vram est ~{estimate:.0f} MiB"
                )
            return 0
        for _ in range(args.count):
            start_model(connection, model, context, reasoning)
        return 0

    if action == "create-subagent":
        create_subagent_command(args)
        return 0

    if action == "restart":
        restart_command(connection, args)
        return 0

    sessions = state.active_sessions()
    if not sessions:
        print("No active models.")
        return 0
    _stop_selected(connection, sessions, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
