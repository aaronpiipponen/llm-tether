"""Rebuild the OpenCode config, recorded state, and stamped agents from what is actually running.

The tool records its instances in a private state file and mirrors each one into the OpenCode
config and the agents directory. If a start is interrupted or a session dies ungracefully those
three views can drift apart. ``reconcile`` reports every difference first; changes are only made
when ``--execute`` is passed.
"""

from __future__ import annotations

from pathlib import Path

from . import agents, diagnostics, opencode, state, transport
from .catalog import model_by_key_optional
from .lifecycle import stop_script
from .transport import Connection


def _readd_provider(session: dict[str, object]) -> None:
    """Recreate one active session's OpenCode provider entry from its recorded facts."""
    key = session.get("key")
    index = session.get("index")
    port = session.get("port")
    context = session.get("context")
    reasoning = session.get("reasoning")
    if (
        not isinstance(key, str)
        or not isinstance(index, int)
        or not isinstance(port, int)
        or not isinstance(context, int)
        or not isinstance(reasoning, str)
    ):
        raise SystemExit(f"active session is malformed: {session!r}")
    model = model_by_key_optional(key)
    if model is None:
        raise SystemExit(f"active session names an unknown model: {key!r}")
    opencode.add_provider(model, index, port, context, reasoning)


def _local_providers(document: dict[str, object]) -> dict[str, object]:
    """Return the provider map, failing loudly if the OpenCode config is malformed."""
    provider_map = document.get(opencode.PROVIDERS_KEY)
    if not isinstance(provider_map, dict):
        raise SystemExit("OpenCode config providers section is not an object")
    return provider_map


def reconcile_command(connection: Connection, apply: bool) -> int:
    """Report (or, with ``apply``, fix) drift and return a process exit code."""
    sessions = state.load_state()
    active = [session for session in sessions if state.pid_alive(session.get("keeper_pid"))]
    active_providers = {session.get("provider") for session in active}
    actions: list[str] = []

    for removed in _drop_stale_sessions(connection, sessions, apply):
        actions.append(removed)

    for path in diagnostics.prune_subagents(apply, sessions=active):
        verb = "remove" if apply else "would remove"
        actions.append(f"{verb} orphan stamp {path.name}")

    actions += _restore_stamps(active, apply)
    actions += _reconcile_providers(active, active_providers, apply)

    if apply and not state.active_sessions():
        transport.terminate_wsl(connection)

    prefix = "reconcile applied" if apply else "reconcile dry-run"
    print(f"{prefix}: {len(actions)} action(s)")
    for action in actions:
        print(f"  - {action}")
    if not apply:
        print("re-run with --execute to apply")
    return 0


def _drop_stale_sessions(
    connection: Connection, sessions: list[dict[str, object]], apply: bool
) -> list[str]:
    """Stop remote servers for dead sessions and drop them from state, returning action lines."""
    actions: list[str] = []
    kept: list[dict[str, object]] = []
    for session in sessions:
        if state.pid_alive(session.get("keeper_pid")):
            kept.append(session)
            continue
        key = session.get("key")
        index = session.get("index")
        model = model_by_key_optional(key) if isinstance(key, str) else None
        if apply and model is not None and isinstance(index, int):
            transport.run_remote(connection, stop_script(model, index))
        actions.append(f"drop stale session {session.get('provider')}")
    if apply:
        state.save_state(kept)
    return actions


def _restore_stamps(active: list[dict[str, object]], apply: bool) -> list[str]:
    """Re-stamp recorded agent files that are missing on disk, when their name still resolves."""
    actions: list[str] = []
    for session in active:
        recorded = session.get("agents")
        if not isinstance(recorded, list):
            continue
        for raw in recorded:
            path = Path(str(raw))
            if path.exists():
                continue
            match = agents.match_stamp(path.stem)
            if match is None:
                actions.append(f"leave unrecognized missing stamp {path.name}")
                continue
            spec, model, instance = match
            if agents.agent_path(spec, model, instance) != path:
                actions.append(f"leave missing stamp {path.name} (name no longer matches its path)")
                continue
            if apply:
                agents.stamp_subagent(spec, model, instance)
            actions.append(f"{'restore' if apply else 'would restore'} stamp {path.name}")
    return actions


def _reconcile_providers(
    active: list[dict[str, object]], active_providers: set[object], apply: bool
) -> list[str]:
    """Add providers missing for active sessions and remove ``local-*`` entries with no owner."""
    actions: list[str] = []
    provider_map = _local_providers(opencode.load_config())

    for session in active:
        provider = session.get("provider")
        if not isinstance(provider, str) or provider in provider_map:
            continue
        if apply:
            _readd_provider(session)
            provider_map = _local_providers(opencode.load_config())
        actions.append(f"re-add provider {provider}")

    for provider in list(provider_map):
        if provider.startswith("local-") and provider not in active_providers:
            if apply:
                opencode.remove_provider(provider)
            actions.append(f"remove provider {provider}")
    return actions
