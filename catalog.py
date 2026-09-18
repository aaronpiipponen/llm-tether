"""Loaded worker catalog and model identity helpers.

The values come from ``config.toml`` (see ``config.py``); this module loads the
config once at import and exposes the model/subagent/connection data the rest of the package
uses. Adding a model or subagent means editing that file, not this module.
"""

from __future__ import annotations

from .config import (
    ConnectionConfig,
    ModelSpec,
    SubagentSpec,
    WorkerConfig,
    load_worker_config,
)

CONFIG: WorkerConfig = load_worker_config()
MODELS: tuple[ModelSpec, ...] = CONFIG.models
SUBAGENTS: tuple[SubagentSpec, ...] = CONFIG.subagents
CONNECTION: ConnectionConfig = CONFIG.connection

__all__ = (
    "CONFIG",
    "CONNECTION",
    "MODELS",
    "SUBAGENTS",
    "ModelSpec",
    "SubagentSpec",
    "model_by_key",
    "model_by_key_optional",
    "provider_for",
)


def model_by_key(key: str) -> ModelSpec:
    """Return the catalog entry with this key, or fail loudly."""
    model = model_by_key_optional(key)
    if model is None:
        known = ", ".join(spec.key for spec in MODELS)
        raise SystemExit(f"unknown model {key!r}; known models: {known}")
    return model


def model_by_key_optional(key: str) -> ModelSpec | None:
    """Return the catalog entry with this key, or None."""
    for model in MODELS:
        if model.key == key:
            return model
    return None


def provider_for(model: ModelSpec, index: int) -> str:
    """Return the OpenCode provider id for one instance.

    The first instance keeps the bare ``local-<key>`` name; later concurrent copies append their
    index. The key is always the full configured key, so two models whose keys share a prefix can
    never collide.
    """
    return f"local-{model.key}" if index == 1 else f"local-{model.key}-{index}"
