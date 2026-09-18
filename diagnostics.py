"""Read-only diagnostics: ``doctor`` plus shared stamp ownership and pruning helpers.

``doctor`` cross-checks everything that has to line up for a provider or a stamped subagent to
be usable: the worker config, the subagent templates, the OpenCode config, the recorded state,
the forwarding health endpoints, and the live-reload plugin. Nothing in this module writes,
deletes, or starts remote work.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from . import agents, config, opencode, state
from .catalog import ModelSpec, SubagentSpec, provider_for
from .lifecycle import health_probe, stale_keeper_logs
from .opencode import OPENCODE_AGENTS_DIR, OPENCODE_CONFIG

RELOAD_PLUGIN_NAME = "config-reload.js"


@dataclass(frozen=True)
class Check:
    """One diagnostic result: a name, an ``ok``/``warn``/``fail`` status, and a detail string."""

    name: str
    status: str
    detail: str


def stamp_owners() -> list[tuple[Path, SubagentSpec, ModelSpec, int]]:
    """Return every recognized stamped agent file and the template/model/instance it encodes."""
    if not OPENCODE_AGENTS_DIR.is_dir():
        return []
    owners: list[tuple[Path, SubagentSpec, ModelSpec, int]] = []
    for path in sorted(OPENCODE_AGENTS_DIR.glob("*.md")):
        match = agents.match_stamp(path.stem)
        if match is not None:
            owners.append((path, *match))
    return owners


def orphan_stamps(
    sessions: list[dict[str, object]] | None = None,
) -> list[tuple[Path, SubagentSpec, ModelSpec, int]]:
    """Return recognized stamps whose provider is absent from the recorded state.

    ``sessions`` defaults to the current state; pass an explicit list to check against proposed
    state without writing it (used by ``reconcile`` dry-runs).
    """
    if sessions is None:
        sessions = state.load_state()
    providers = {session.get("provider") for session in sessions}
    return [owner for owner in stamp_owners() if provider_for(owner[2], owner[3]) not in providers]


def prune_subagents(apply: bool, sessions: list[dict[str, object]] | None = None) -> list[Path]:
    """Remove orphan stamp files when ``apply`` is true; return the affected paths either way."""
    paths = [path for path, *_ in orphan_stamps(sessions)]
    if apply:
        return agents.remove_subagents(paths)
    return paths


def subagents_report() -> None:
    """Print the recorded stamps per session, then the orphan stamps found on disk."""
    sessions = state.load_state()
    if sessions:
        print("Recorded subagents:")
        for session in sessions:
            print(f"  {session.get('provider')}:")
            recorded = session.get("agents")
            names = [Path(str(raw)).name for raw in recorded] if isinstance(recorded, list) else []
            if not names:
                print("    (none)")
            for name in names:
                print(f"    {name}")
    else:
        print("No recorded sessions.")
    print("Orphan stamps:")
    orphans = orphan_stamps(sessions)
    if not orphans:
        print("  (none)")
    for path, _spec, model, instance in orphans:
        print(f"  {path.name} -> {provider_for(model, instance)} (no state entry)")


def _check_config() -> list[Check]:
    try:
        config.load_worker_config()
    except SystemExit as exc:
        return [Check("config", "fail", str(exc))]
    return [Check("config", "ok", "")]


def _check_templates() -> list[Check]:
    findings = agents.manifest_findings()
    if findings:
        return [Check("templates", "fail", "; ".join(findings))]
    return [Check("templates", "ok", "")]


def _check_opencode() -> tuple[list[Check], dict[str, object] | None]:
    try:
        document = opencode.load_config()
    except SystemExit as exc:
        return [Check("opencode", "fail", str(exc))], None
    provider_map = document.get("provider")
    if not isinstance(provider_map, dict):
        return [Check("opencode", "fail", "provider section is not an object")], None
    return [Check("opencode", "ok", f"{len(provider_map)} provider(s)")], provider_map


def _check_state(provider_map: dict[str, object]) -> list[Check]:
    checks: list[Check] = []
    for index, session in enumerate(state.load_state()):
        provider = session.get("provider")
        problems: list[str] = []
        if not isinstance(provider, str) or provider not in provider_map:
            problems.append(f"provider {provider!r} missing from OpenCode config")
        recorded = session.get("agents")
        if isinstance(recorded, list):
            for raw in recorded:
                if not Path(str(raw)).exists():
                    problems.append(f"stamped agent missing: {raw}")
        name = f"state[{provider if isinstance(provider, str) else index}]"
        checks.append(Check(name, "fail" if problems else "ok", "; ".join(problems)))
    if not checks:
        checks.append(Check("state", "ok", "no sessions"))
    return checks


def _check_health() -> list[Check]:
    checks: list[Check] = []
    for session in state.active_sessions():
        provider = session.get("provider")
        port = session.get("port")
        if not isinstance(port, int):
            checks.append(Check(f"health[{provider}]", "fail", "no port recorded"))
            continue
        ready, detail = health_probe(port)
        checks.append(Check(f"health[{provider}]", "ok" if ready else "fail", detail))
    if not checks:
        checks.append(Check("health", "ok", "no active instances"))
    return checks


def _check_stale() -> list[Check]:
    checks: list[Check] = []
    for session in state.load_state():
        if state.pid_alive(session.get("keeper_pid")):
            continue
        checks.append(Check(f"stale[{session.get('provider')}]", "fail", "keeper pid is not alive"))
    return checks


def _check_orphans() -> list[Check]:
    checks: list[Check] = []
    for path, _spec, model, instance in orphan_stamps():
        checks.append(
            Check(
                f"orphan[{path.name}]",
                "fail",
                f"no state entry for {provider_for(model, instance)}",
            )
        )
    return checks


def _plugin_path() -> Path | None:
    """Return the installed live-reload plugin path, or None when it is absent."""
    parent = OPENCODE_CONFIG.parent
    for candidate in (
        parent / "plugin" / RELOAD_PLUGIN_NAME,
        parent / "plugins" / RELOAD_PLUGIN_NAME,
    ):
        if candidate.is_file():
            return candidate
    return None


def resolved_paths() -> dict[str, str]:
    """Return the resolved config/state/opencode/agents/plugin paths currently in effect."""
    plugin = _plugin_path()
    return {
        "config": str(config.CONFIG_PATH),
        "state": str(state.STATE_DIR),
        "opencode": str(OPENCODE_CONFIG),
        "agents": str(OPENCODE_AGENTS_DIR),
        "plugin": str(plugin) if plugin is not None else "(not installed)",
    }


def where_command(as_json: bool = False) -> int:
    """Print the resolved paths the tool uses, honoring the environment overrides."""
    paths = resolved_paths()
    if as_json:
        print(json.dumps(paths, indent=2))
        return 0
    for name, value in paths.items():
        print(f"  {name:<8}: {value}")
    return 0


def _check_plugin() -> list[Check]:
    plugin = _plugin_path()
    if plugin is not None:
        return [Check("reload plugin", "ok", str(plugin))]
    return [
        Check(
            "reload plugin",
            "warn",
            f"{RELOAD_PLUGIN_NAME} not found; a running OpenCode needs a restart to see changes",
        )
    ]


def _check_keeper_logs() -> list[Check]:
    stale = stale_keeper_logs()
    if stale:
        names = ", ".join(path.name for path in stale)
        return [Check("keeper logs", "warn", f"{len(stale)} stale: {names}")]
    return [Check("keeper logs", "ok", "")]


def doctor_command(as_json: bool = False) -> int:
    """Run every read-only check, print the results, and return 1 when any check fails."""
    checks: list[Check] = []
    checks += _check_config()
    checks += _check_templates()
    opencode_checks, provider_map = _check_opencode()
    checks += opencode_checks
    if provider_map is not None:
        checks += _check_state(provider_map)
    checks += _check_health()
    checks += _check_stale()
    checks += _check_orphans()
    checks += _check_keeper_logs()
    checks += _check_plugin()

    problems = sum(1 for check in checks if check.status == "fail")
    if as_json:
        print(
            json.dumps(
                {
                    "problems": problems,
                    "paths": resolved_paths(),
                    "checks": [asdict(check) for check in checks],
                },
                indent=2,
            )
        )
        return 1 if problems else 0

    print("Paths:")
    for path_name, path_value in resolved_paths().items():
        print(f"  {path_name:<8}: {path_value}")
    for check in checks:
        label = check.status if check.status == "ok" else check.status.upper()
        line = f"{label:<4} {check.name}"
        if check.detail:
            line += f" - {check.detail}"
        print(line)
    print(f"{problems} problem(s)")
    return 1 if problems else 0
