"""OpenCode v2 config: one provider entry per running instance, added on start and removed on stop.

The global config is the file OpenCode itself reads; the config-reload plugin watches it and
reloads providers in the running OpenCode service, so this tool only writes files. Entries use
the native v2 schema under ``providers`` (``package``/``settings``/``capabilities``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .catalog import ModelSpec, provider_for
from .identity import ENV_PREFIX

# Top-level OpenCode v2 config key that holds provider entries.
PROVIDERS_KEY = "providers"

# OpenCode config that owns the provider entries for these worker instances. `start` adds one
# provider entry per instance and `stop` removes it. Override with the *_OPENCODE_CONFIG env var.
OPENCODE_CONFIG = Path(
    os.environ.get(
        f"{ENV_PREFIX}OPENCODE_CONFIG",
        str(Path.home() / ".config" / "opencode" / "opencode.json"),
    )
)

# Global OpenCode agents directory that holds stamped subagent files. Override with the
# *_OPENCODE_AGENTS_DIR env var.
OPENCODE_AGENTS_DIR = Path(
    os.environ.get(
        f"{ENV_PREFIX}OPENCODE_AGENTS_DIR",
        str(OPENCODE_CONFIG.parent / "agents"),
    )
)


def load_config() -> dict[str, object]:
    """Load the OpenCode config as plain JSON, failing loudly on any problem."""
    if not OPENCODE_CONFIG.is_file() or OPENCODE_CONFIG.is_symlink():
        raise SystemExit(
            f"OpenCode config not found at {OPENCODE_CONFIG}; set {ENV_PREFIX}OPENCODE_CONFIG"
        )
    try:
        document = json.loads(OPENCODE_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(
            f"OpenCode config is not plain UTF-8 JSON (comments unsupported): {OPENCODE_CONFIG}"
        ) from exc
    providers = document.setdefault(PROVIDERS_KEY, {})
    if not isinstance(providers, dict):
        raise SystemExit("OpenCode config providers section is not an object")
    return document


def write_config(document: dict[str, object]) -> None:
    """Atomically publish the OpenCode config at mode 0600."""
    temporary = OPENCODE_CONFIG.with_name(OPENCODE_CONFIG.name + ".tmp")
    temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, OPENCODE_CONFIG)


def opencode_entry(
    model: ModelSpec, instance: int, port: int, context: int, reasoning: str
) -> dict[str, object]:
    """Build the OpenCode v2 provider entry for one running instance."""
    entry: dict[str, object] = {
        "name": model.display,
        "capabilities": {"tools": model.tool_call, "input": ["text"], "output": ["text"]},
        "limit": {"context": context, "output": model.output_tokens},
    }
    if model.supports_reasoning:
        entry["settings"] = {"reasoningEffort": "none" if reasoning == "off" else reasoning}
    return {
        "name": f"Local worker {model.key} #{instance}",
        "package": "aisdk:@ai-sdk/openai-compatible",
        "settings": {
            "baseURL": f"http://127.0.0.1:{port}/v1",
            "apiKey": "dummy",
        },
        "models": {model.alias: entry},
    }


def add_provider(model: ModelSpec, instance: int, port: int, context: int, reasoning: str) -> str:
    """Add one instance's provider entry to the OpenCode config and return its provider id."""
    provider = provider_for(model, instance)
    document = load_config()
    provider_map = document[PROVIDERS_KEY]
    assert isinstance(provider_map, dict)
    provider_map[provider] = opencode_entry(model, instance, port, context, reasoning)
    write_config(document)
    print(f"  opencode provider : {provider}/{model.alias} added to {OPENCODE_CONFIG}")
    return provider


def remove_provider(provider: str) -> None:
    """Remove one instance's provider entry from the OpenCode config if it is present."""
    document = load_config()
    provider_map = document[PROVIDERS_KEY]
    assert isinstance(provider_map, dict)
    if provider in provider_map:
        del provider_map[provider]
        write_config(document)
        print(f"  opencode provider : {provider} removed from {OPENCODE_CONFIG}")
