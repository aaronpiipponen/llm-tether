"""SSH-over-WSL transport to the private GPU worker.

The base64-through-``wsl.exe -d <distro> -- bash -lc`` wrapping and the detached-keeper technique
are copied from the repository's proven translation controller so this tool stays self-contained.
"""

from __future__ import annotations

import base64
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .state import STATE_DIR

# Windows OpenSSH hands the remote command to cmd.exe before wsl.exe sees it, so cmd
# metacharacters must be caret-escaped or they are consumed before bash runs.
_CMD_METACHARACTERS = frozenset('&|<>^()"')


@dataclass(frozen=True)
class Connection:
    """Resolved worker connection identity for one invocation."""

    ssh_target: str
    wsl_distro: str
    worker_root: str
    connect_timeout: int
    command_timeout: int
    health_timeout: int


def remote_command(connection: Connection, script: str) -> str:
    """Wrap one bash script for ``wsl.exe -d <distro> -- bash -lc``.

    The script is base64-encoded and decoded on the worker side rather than embedded literally.
    ``wsl.exe -d <distro> -- bash -lc <script>`` corrupts embedded newlines and quoting, and the
    cmd.exe caret-escape pass mangles shell metacharacters; a single base64 token has neither
    newlines nor metacharacters, so it survives both layers intact. The outer ``|`` in the decode
    pipeline is still caret-escaped for cmd.exe.
    """
    token = base64.b64encode(script.encode("utf-8")).decode("ascii")
    command = (
        f"wsl.exe -d {connection.wsl_distro} -- bash -lc "
        f"{shlex.quote(f'echo {token} | base64 -d | bash')}"
    )
    return "".join(
        f"^{character}" if character in _CMD_METACHARACTERS else character for character in command
    )


def ssh_argv(connection: Connection, script: str) -> list[str]:
    """Build the local SSH argv that runs one remote WSL bash script."""
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={connection.connect_timeout}",
        connection.ssh_target,
        remote_command(connection, script),
    ]


def run_remote(connection: Connection, script: str) -> subprocess.CompletedProcess[bytes]:
    """Run one remote script to completion, surfacing stderr on failure or timeout."""
    try:
        result = subprocess.run(
            ssh_argv(connection, script),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=connection.command_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise SystemExit(
            f"remote command did not finish within {connection.command_timeout}s on "
            f"{connection.ssh_target}"
        ) from exc
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise SystemExit(f"remote command failed (exit {result.returncode}): {message}")
    return result


def start_keeper(connection: Connection, port: int, log_path: Path) -> int:
    """Start one detached local SSH keeper that forwards the port and holds WSL open.

    stderr goes to a private file rather than a pipe, because the keeper is intentionally
    long-lived and nobody drains a pipe; an unread pipe would eventually block the keeper.
    """
    argv = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        f"ConnectTimeout={connection.connect_timeout}",
        "-L",
        f"127.0.0.1:{port}:127.0.0.1:{port}",
        connection.ssh_target,
        remote_command(connection, "exec sleep infinity"),
    ]
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log,
            start_new_session=True,
        )
    time.sleep(1.5)
    if process.poll() is not None:
        detail = ""
        try:
            detail = log_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            pass
        raise SystemExit(
            f"SSH keeper for port {port} exited immediately: {detail or 'unknown error'}"
        )
    return process.pid


def terminate_wsl(connection: Connection) -> None:
    """Terminate the worker WSL distribution so it stops holding model page cache in RAM.

    Reading a multi-GB GGUF leaves that file cached in the WSL VM, and the VM stays resident after
    the model process exits, so without this the host keeps showing model-sized RAM use. Starting a
    model again transparently boots the distribution. A failure here is reported, not swallowed.
    """
    try:
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                connection.ssh_target,
                f"wsl.exe --terminate {connection.wsl_distro}",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=connection.command_timeout,
        )
    except subprocess.TimeoutExpired:
        print(
            f"  warning: could not terminate WSL {connection.wsl_distro} within "
            f"{connection.command_timeout}s"
        )
        return
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        print(f"  warning: could not terminate WSL {connection.wsl_distro}: {detail}")
    else:
        print(f"  released WSL {connection.wsl_distro} memory")
