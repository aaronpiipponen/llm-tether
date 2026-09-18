"""Generic global subagent templates and stamping.

The templates under ``templates/`` are project-agnostic versions of read-only reviewers and a
scout. The exposed set is the ``[[subagents]]`` manifest in ``config.toml``; each entry names a
template file relative to ``templates/``. ``create subagent`` stamps one with a live instance's
provider id and writes it to the global OpenCode agents directory under
``<name>-<model slug>-<instance>.md``. The name embeds the instance, so the same template can be
stamped for several live models and one live model can hold several stamps without collision.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

from .catalog import MODELS, SUBAGENTS, ModelSpec, SubagentSpec, provider_for
from .opencode import OPENCODE_AGENTS_DIR

TEMPLATES_DIR = Path(__file__).with_name("templates")

# The token replaced with the stamped instance's `<provider>/<alias>` model id.
MODEL_PLACEHOLDER = "{{LLM_TETHER_MODEL}}"


def subagent_specs() -> tuple[SubagentSpec, ...]:
    """Return the subagent templates exposed by the config manifest."""
    return SUBAGENTS


def template_path(spec: SubagentSpec) -> Path:
    """Return the manifest entry's template file path."""
    return TEMPLATES_DIR / spec.file


def template_description(spec: SubagentSpec) -> str:
    """Return the template's frontmatter description, or fail loudly when missing."""
    text = template_path(spec).read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise SystemExit(f"template is missing frontmatter: {spec.file}")
    for line in text.split("---", 2)[1].splitlines():
        if line.startswith("description:"):
            return line.split(":", 1)[1].strip()
    raise SystemExit(f"template is missing a description: {spec.file}")


def agent_stem(spec: SubagentSpec, model: ModelSpec, instance: int) -> str:
    """Return the non-colliding global agent name for one stamped template and instance."""
    return f"{spec.name}-{model.slug}-{instance}"


def agent_path(spec: SubagentSpec, model: ModelSpec, instance: int) -> Path:
    """Return the global OpenCode agent file path for one stamped template and instance."""
    return OPENCODE_AGENTS_DIR / f"{agent_stem(spec, model, instance)}.md"


def resolve_agent_path(value: str) -> Path:
    """Resolve a user-supplied stamped agent path, treating a relative value as an agents-dir name."""
    raw = Path(value).expanduser()
    if raw.is_absolute():
        return raw.resolve()
    return (OPENCODE_AGENTS_DIR / raw).resolve()


def stamp_subagent(spec: SubagentSpec, model: ModelSpec, instance: int) -> Path:
    """Render one template with a live instance's model id and write the global agent file."""
    text = template_path(spec).read_text(encoding="utf-8")
    if MODEL_PLACEHOLDER not in text:
        raise SystemExit(f"template {spec.file} has no {MODEL_PLACEHOLDER} placeholder")
    model_id = f"{provider_for(model, instance)}/{model.alias}"
    rendered = text.replace(MODEL_PLACEHOLDER, model_id)
    destination = agent_path(spec, model, instance)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(rendered, encoding="utf-8")
    os.chmod(temporary, 0o644)
    os.replace(temporary, destination)
    return destination


def remove_subagents(paths: Sequence[object]) -> list[Path]:
    """Remove the named global agent files that still exist and return what was removed."""
    removed: list[Path] = []
    for raw in paths:
        path = Path(str(raw))
        if path.exists():
            path.unlink()
            removed.append(path)
    return removed


def match_stamp(stem: str) -> tuple[SubagentSpec, ModelSpec, int] | None:
    """Parse a stamped agent file stem as ``<template name>-<model slug>-<instance>``.

    Returns the matching manifest entry, catalog model, and 1-based instance, or ``None`` when
    the stem does not belong to any configured template/model pair. Both names may contain
    hyphens, so the match is made against the configured prefixes rather than by splitting.
    """
    for spec in SUBAGENTS:
        for model in MODELS:
            prefix = f"{spec.name}-{model.slug}-"
            if not stem.startswith(prefix):
                continue
            suffix = stem[len(prefix) :]
            if suffix.isdigit() and int(suffix) >= 1:
                return spec, model, int(suffix)
    return None


def remove_stale_temps() -> list[Path]:
    """Remove this tool's own interrupted ``*.md.tmp`` stamp files and return them.

    Only names that parse as one of our configured stamps are touched, so an unrelated temp file
    in the shared agents directory is never deleted.
    """
    if not OPENCODE_AGENTS_DIR.is_dir():
        return []
    removed: list[Path] = []
    for path in sorted(OPENCODE_AGENTS_DIR.glob("*.md.tmp")):
        if match_stamp(path.name[: -len(".md.tmp")]) is None:
            continue
        path.unlink()
        removed.append(path)
    return removed


def manifest_findings() -> list[str]:
    """Return one message per malformed template, or an empty list when the manifest is sound."""
    findings: list[str] = []
    for spec in SUBAGENTS:
        path = template_path(spec)
        if not path.is_file():
            findings.append(f"subagent {spec.name!r} names a missing template: {path}")
            continue
        if MODEL_PLACEHOLDER not in path.read_text(encoding="utf-8"):
            findings.append(f"template {spec.file} has no {MODEL_PLACEHOLDER} placeholder")
            continue
        try:
            template_description(spec)
        except SystemExit as exc:
            findings.append(str(exc))
    return findings


def _validate_manifest() -> None:
    """Fail loudly at import when the config names a missing or malformed template."""
    findings = manifest_findings()
    if findings:
        raise SystemExit("\n".join(findings))


_validate_manifest()
