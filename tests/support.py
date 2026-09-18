"""Shared helpers for the standalone tool's tests.

Every test builds a throwaway workspace and points the tool's environment overrides at it, so the
operator's real config, state, OpenCode config, and agents directory are never touched. The tests
use only the published example template and synthetic model entries; nothing here refers to a real
machine or a private catalog.

Nothing here hardcodes the package name: it is derived from the tool directory, exactly as the
entrypoint does, so the directory can be renamed without editing the tests.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parents[1]
MODEL_SMALL = "small"
MODEL_LARGE = "large"


def _derived_name() -> str:
    name = "".join(
        character if (character.isalnum() or character == "_") else "_"
        for character in TOOL_DIR.name
    )
    if name[:1].isdigit():
        name = "_" + name
    return name


def _load_identity():
    spec = importlib.util.spec_from_file_location("_tool_identity", TOOL_DIR / "identity.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load identity.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ENV_PREFIX = _load_identity().ENV_PREFIX


def load_package():
    """Import the tool package from its directory and return its cli/state/transport modules."""
    name = _derived_name()
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, TOOL_DIR / "__init__.py", submodule_search_locations=[str(TOOL_DIR)]
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load {name} from {TOOL_DIR}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    cli = importlib.import_module(f"{name}.cli")
    state = importlib.import_module(f"{name}.state")
    transport = importlib.import_module(f"{name}.transport")
    return cli, state, transport, transport.Connection


CONFIG_TOML = """\
[connection]
ssh_target = "test-worker"
wsl_distro = "TestDistro"
worker_root = "/srv/worker"
connect_timeout = 5
command_timeout = 30
health_timeout = 30

[[subagents]]
name = "example-reviewer"
file = "example-reviewer.md"

[[models]]
key = "small"
display = "Small Test Model"
alias = "small"
slug = "small"
artifact = "models/small/small.gguf"
runtime_revision = "test-revision"
port_base = 19100
context_sizes = [1024, 2048]
default_context = 1024
reasoning_levels = ["off", "low"]
default_reasoning = "off"
supports_reasoning = true
tool_call = true
output_tokens = 512
weight_mib = 100.0
kv_mib_per_1k_context = 1.0
fixed_kv_mib = 0.0
compute_mib = 10.0

[[models]]
key = "large"
display = "Large Test Model"
alias = "large"
slug = "large"
artifact = "models/large/large.gguf"
runtime_revision = "test-revision"
port_base = 19200
context_sizes = [1024, 4096]
default_context = 4096
reasoning_levels = ["off"]
default_reasoning = "off"
supports_reasoning = false
tool_call = false
output_tokens = 512
weight_mib = 200.0
kv_mib_per_1k_context = 2.0
fixed_kv_mib = 0.0
compute_mib = 20.0
"""


class Workspace:
    """A throwaway config/state/opencode/agents layout plus the env that selects it."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="llm-tether-test-"))
        self.agents = self.root / "agents"
        self.agents.mkdir()
        self.state_dir = self.root / "state"
        self.state_dir.mkdir()
        self.config = self.root / "config.toml"
        self.config.write_text(CONFIG_TOML, encoding="utf-8")
        self.opencode = self.root / "opencode.json"
        self.write_opencode({"provider": {"nvidia": {}}})
        self.env = dict(os.environ)
        self.env.update(
            {
                f"{ENV_PREFIX}CONFIG": str(self.config),
                f"{ENV_PREFIX}STATE_DIR": str(self.state_dir),
                f"{ENV_PREFIX}OPENCODE_CONFIG": str(self.opencode),
                f"{ENV_PREFIX}OPENCODE_AGENTS_DIR": str(self.agents),
            }
        )

    def write_opencode(self, document: dict[str, object]) -> None:
        self.opencode.write_text(json.dumps(document), encoding="utf-8")

    def read_opencode(self) -> dict[str, object]:
        return json.loads(self.opencode.read_text(encoding="utf-8"))

    def write_state(self, sessions: list[dict[str, object]]) -> None:
        (self.state_dir / "state.json").write_text(
            json.dumps({"sessions": sessions}), encoding="utf-8"
        )

    def read_state(self) -> list[dict[str, object]]:
        path = self.state_dir / "state.json"
        return json.loads(path.read_text(encoding="utf-8"))["sessions"]

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(TOOL_DIR), *args],
            cwd=TOOL_DIR,
            env=self.env,
            capture_output=True,
            text=True,
        )

    def cleanup(self) -> None:
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)


def session(provider: str, model: str, index: int, agents: list[str]) -> dict[str, object]:
    """Build one state entry that looks active (pid 1 is always alive)."""
    return {
        "key": model,
        "index": index,
        "provider": provider,
        "alias": model,
        "port": 19900 + index,
        "context": 1024,
        "reasoning": "off",
        "keeper_pid": 1,
        "agents": agents,
        "ssh_target": "test-worker",
        "wsl_distro": "TestDistro",
        "worker_root": "/srv/worker",
        "started": "2000-01-01T00:00:00Z",
    }
