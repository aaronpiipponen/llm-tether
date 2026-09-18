"""Private local state: which instances this tool started, and their local keeper PIDs.

The state file is the single source of truth for active instances. Each entry also records the
global subagent files stamped for that instance, so stopping an instance removes exactly the
files it owns.
"""

from __future__ import annotations

import json
import os
import signal
import time
from collections.abc import Sequence
from pathlib import Path

from .catalog import model_by_key_optional
from .identity import ENV_PREFIX, STATE_DIR_NAME

STATE_DIR = Path(
    os.environ.get(
        f"{ENV_PREFIX}STATE_DIR",
        os.path.join(
            os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")),
            STATE_DIR_NAME,
        ),
    )
)
STATE_FILE = STATE_DIR / "state.json"
REMOTE_STATE_DIR = f"$HOME/.local/state/{STATE_DIR_NAME}"


def load_state() -> list[dict[str, object]]:
    """Load the private active-instance list, or an empty list when none exists."""
    if not STATE_FILE.exists():
        return []
    if STATE_FILE.is_symlink() or not STATE_FILE.is_file():
        raise SystemExit(f"state path is not a regular file: {STATE_FILE}")
    try:
        payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"state file is not valid UTF-8 JSON: {STATE_FILE}") from exc
    sessions = payload.get("sessions")
    if not isinstance(sessions, list):
        raise SystemExit(f"state file has no session list: {STATE_FILE}")
    return sessions


def save_state(sessions: Sequence[dict[str, object]]) -> None:
    """Atomically write the private active-instance list at mode 0600."""
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    temporary.write_text(
        json.dumps({"sessions": list(sessions)}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, STATE_FILE)


def pid_alive(pid: int | None) -> bool:
    """Return whether a recorded local process identifier still exists and is not a zombie.

    ``os.kill(pid, 0)`` succeeds for a zombie, and a keeper we killed but did not reap stays a
    zombie until this process exits, so the process state is checked explicitly.
    """
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[0]
    except OSError, IndexError:
        return True
    return state not in {"Z", "X"}


def stop_pid(pid: int | None, timeout: float) -> None:
    """Terminate one recorded local process and wait for it to exit."""
    if not pid_alive(pid):
        return
    assert pid is not None
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return
        time.sleep(0.2)
    raise SystemExit(f"local process {pid} did not exit within {timeout:.0f}s")


def active_sessions() -> list[dict[str, object]]:
    """Return the stored instances that still look alive and are known to the catalog."""
    sessions = []
    for session in load_state():
        key = session.get("key")
        if not isinstance(key, str) or model_by_key_optional(key) is None:
            continue
        if pid_alive(session.get("keeper_pid")):  # type: ignore[arg-type]
            sessions.append(session)
    return sessions
