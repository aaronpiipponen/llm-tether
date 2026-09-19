"""Load and validate the worker's TOML configuration.

``config.toml`` beside this module is the single place model entries, connection defaults, and
the exposed subagent manifest are defined; upgrading means editing that file, not Python. The
loader is strict: a missing file, unknown key, wrong type, or inconsistent value fails loudly
instead of silently falling back.

Override the config path with ``LLM_TETHER_CONFIG`` (used by tests and by copies).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .identity import ENV_PREFIX

CONFIG_PATH = Path(
    os.environ.get(f"{ENV_PREFIX}CONFIG", str(Path(__file__).with_name("config.toml")))
)

_TOP_KEYS = {"connection", "subagents", "models"}
_CONNECTION_KEYS = {
    "ssh_target",
    "wsl_distro",
    "worker_root",
    "connect_timeout",
    "command_timeout",
    "health_timeout",
}
_SUBAGENT_KEYS = {"name", "file"}
_MODEL_KEYS = {
    "key",
    "display",
    "alias",
    "slug",
    "artifact",
    "runtime_revision",
    "port_base",
    "context_sizes",
    "default_context",
    "reasoning_levels",
    "default_reasoning",
    "supports_reasoning",
    "tool_call",
    "output_tokens",
    "weight_mib",
    "kv_mib_per_1k_context",
    "fixed_kv_mib",
    "compute_mib",
    "gpu_layers",
    "extra_args",
}
_MODEL_REQUIRED = _MODEL_KEYS - {"extra_args", "gpu_layers"}


@dataclass(frozen=True)
class ModelSpec:
    """One configured worker model and the context/reasoning/VRAM choices allowed for it."""

    key: str
    display: str
    alias: str
    slug: str
    artifact: str
    runtime_revision: str
    port_base: int
    context_sizes: tuple[int, ...]
    default_context: int
    reasoning_levels: tuple[str, ...]
    default_reasoning: str
    supports_reasoning: bool
    tool_call: bool
    output_tokens: int
    weight_mib: float
    kv_mib_per_1k_context: float
    fixed_kv_mib: float
    compute_mib: float
    gpu_layers: str = "auto"
    extra_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class SubagentSpec:
    """One subagent template exposed by the config manifest."""

    name: str
    file: str


@dataclass(frozen=True)
class ConnectionConfig:
    """Worker connection and runtime timeouts."""

    ssh_target: str
    wsl_distro: str
    worker_root: str
    connect_timeout: int
    command_timeout: int
    health_timeout: int


@dataclass(frozen=True)
class WorkerConfig:
    """Everything the config file owns."""

    connection: ConnectionConfig
    subagents: tuple[SubagentSpec, ...]
    models: tuple[ModelSpec, ...]


def _table(value: object, where: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise SystemExit(f"{where} must be a TOML table")
    return value


def _keys(table: dict[str, object], allowed: set[str], where: str) -> None:
    unknown = set(table) - allowed
    if unknown:
        raise SystemExit(f"{where} has unknown keys: {', '.join(sorted(unknown))}")


def _required(table: dict[str, object], keys: set[str], where: str) -> None:
    missing = [key for key in sorted(keys) if key not in table]
    if missing:
        raise SystemExit(f"{where} is missing keys: {', '.join(missing)}")


def _str(table: dict[str, object], key: str, where: str) -> str:
    value = table[key]
    if not isinstance(value, str) or not value.strip():
        raise SystemExit(f"{where}.{key} must be a non-empty string")
    return value


def _int(table: dict[str, object], key: str, where: str) -> int:
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise SystemExit(f"{where}.{key} must be an integer")
    return value


def _float(table: dict[str, object], key: str, where: str) -> float:
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SystemExit(f"{where}.{key} must be a number")
    return float(value)


def _bool(table: dict[str, object], key: str, where: str) -> bool:
    value = table[key]
    if not isinstance(value, bool):
        raise SystemExit(f"{where}.{key} must be a boolean")
    return value


def _gpu_layers(table: dict[str, object], where: str) -> str:
    """Return the normalized ``--n-gpu-layers`` value: ``auto``, ``all``, or a count.

    ``auto`` (the default when the key is omitted) is returned as-is so the launcher can leave the
    flag unset and let llama.cpp fit the offload to the worker's free VRAM.
    """
    value = table.get("gpu_layers", "auto")
    if isinstance(value, bool):
        raise SystemExit(f"{where}.gpu_layers must be 'auto', 'all', or a non-negative integer")
    if isinstance(value, int):
        if value < 0:
            raise SystemExit(f"{where}.gpu_layers must be 'auto', 'all', or a non-negative integer")
        return str(value)
    if isinstance(value, str) and value in {"auto", "all"}:
        return value
    raise SystemExit(f"{where}.gpu_layers must be 'auto', 'all', or a non-negative integer")


def _str_list(table: dict[str, object], key: str, where: str) -> tuple[str, ...]:
    value = table[key]
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise SystemExit(f"{where}.{key} must be a non-empty list of strings")
    return tuple(value)


def _int_list(table: dict[str, object], key: str, where: str) -> tuple[int, ...]:
    value = table[key]
    if (
        not isinstance(value, list)
        or not value
        or not all(
            isinstance(item, int) and not isinstance(item, bool) and item > 0 for item in value
        )
    ):
        raise SystemExit(f"{where}.{key} must be a non-empty list of positive integers")
    return tuple(value)


def _str_tuple(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SystemExit(f"{where} must be a list of strings")
    return tuple(value)


def _unique(values: list[str], where: str) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise SystemExit(f"{where} repeats {value!r}")
        seen.add(value)


def _parse_connection(document: dict[str, object]) -> ConnectionConfig:
    table = _table(document.get("connection"), "config.connection")
    _keys(table, _CONNECTION_KEYS, "config.connection")
    _required(table, _CONNECTION_KEYS, "config.connection")
    return ConnectionConfig(
        ssh_target=_str(table, "ssh_target", "config.connection"),
        wsl_distro=_str(table, "wsl_distro", "config.connection"),
        worker_root=_str(table, "worker_root", "config.connection"),
        connect_timeout=_int(table, "connect_timeout", "config.connection"),
        command_timeout=_int(table, "command_timeout", "config.connection"),
        health_timeout=_int(table, "health_timeout", "config.connection"),
    )


def _parse_subagent(table: object, index: int) -> SubagentSpec:
    where = f"config.subagents[{index}]"
    entry = _table(table, where)
    _keys(entry, _SUBAGENT_KEYS, where)
    _required(entry, _SUBAGENT_KEYS, where)
    return SubagentSpec(name=_str(entry, "name", where), file=_str(entry, "file", where))


def _parse_model(table: object, index: int) -> ModelSpec:
    where = f"config.models[{index}]"
    entry = _table(table, where)
    _keys(entry, _MODEL_KEYS, where)
    _required(entry, _MODEL_REQUIRED, where)
    extra_args = _str_tuple(entry.get("extra_args", []), f"{where}.extra_args")
    model = ModelSpec(
        key=_str(entry, "key", where),
        display=_str(entry, "display", where),
        alias=_str(entry, "alias", where),
        slug=_str(entry, "slug", where),
        artifact=_str(entry, "artifact", where),
        runtime_revision=_str(entry, "runtime_revision", where),
        port_base=_int(entry, "port_base", where),
        context_sizes=_int_list(entry, "context_sizes", where),
        default_context=_int(entry, "default_context", where),
        reasoning_levels=_str_list(entry, "reasoning_levels", where),
        default_reasoning=_str(entry, "default_reasoning", where),
        supports_reasoning=_bool(entry, "supports_reasoning", where),
        tool_call=_bool(entry, "tool_call", where),
        output_tokens=_int(entry, "output_tokens", where),
        weight_mib=_float(entry, "weight_mib", where),
        kv_mib_per_1k_context=_float(entry, "kv_mib_per_1k_context", where),
        fixed_kv_mib=_float(entry, "fixed_kv_mib", where),
        compute_mib=_float(entry, "compute_mib", where),
        gpu_layers=_gpu_layers(entry, where),
        extra_args=extra_args,
    )
    if model.default_context not in model.context_sizes:
        raise SystemExit(f"{where}.default_context {model.default_context} is not in context_sizes")
    if model.default_reasoning not in model.reasoning_levels:
        raise SystemExit(f"{where}.default_reasoning {model.default_reasoning!r} is not allowed")
    return model


def load_worker_config(path: Path | None = None) -> WorkerConfig:
    """Load and validate ``config.toml``, failing loudly on any problem."""
    config_path = path or CONFIG_PATH
    if not config_path.is_file() or config_path.is_symlink():
        raise SystemExit(f"worker config not found: {config_path}")
    try:
        with config_path.open("rb") as handle:
            document = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SystemExit(f"worker config is not valid TOML: {config_path} ({exc})") from exc

    _keys(document, _TOP_KEYS, "config")
    _required(document, _TOP_KEYS, "config")

    raw_subagents = document["subagents"]
    raw_models = document["models"]
    if not isinstance(raw_subagents, list) or not raw_subagents:
        raise SystemExit("config.subagents must be a non-empty array of tables")
    if not isinstance(raw_models, list) or not raw_models:
        raise SystemExit("config.models must be a non-empty array of tables")

    subagents = tuple(_parse_subagent(entry, index) for index, entry in enumerate(raw_subagents))
    models = tuple(_parse_model(entry, index) for index, entry in enumerate(raw_models))

    _unique([spec.name for spec in subagents], "config.subagents names")
    _unique([spec.file for spec in subagents], "config.subagents files")
    _unique([model.key for model in models], "config.models keys")
    _unique([model.slug for model in models], "config.models slugs")
    _unique([str(model.port_base) for model in models], "config.models port_base values")

    return WorkerConfig(connection=_parse_connection(document), subagents=subagents, models=models)
