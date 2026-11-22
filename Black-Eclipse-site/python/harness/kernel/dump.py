"""Dump, diff and diagnose a composition before anything mounts."""

from __future__ import annotations

import json
from typing import Any

from .composition import Composition


def render(composition: Composition) -> str:
    """Deterministic YAML for the effective rows, with source comments."""
    import yaml  # noqa: PLC0415

    lines: list[str] = ["# Eclipse kernel composition", ""]
    last_source = None
    for row in composition.rows:
        if row.source != last_source:
            lines.append(f"# source: {row.source or 'unknown'}")
            last_source = row.source
        lines.append(yaml.safe_dump([row.to_dict()], sort_keys=False, default_flow_style=False).rstrip())
        lines.append("")
    if composition.diagnostics:
        lines.append("# diagnostics")
        for item in composition.diagnostics:
            lines.append(f"# {item.get('code')}: {item.get('message')} [{item.get('source', '')}]")
    return "\n".join(lines).rstrip() + "\n"


def render_json(composition: Composition) -> str:
    return json.dumps(composition.to_dict(), indent=2, sort_keys=True) + "\n"


def diff(before: Composition, after: Composition) -> dict[str, list[dict[str, Any]]]:
    """Added / removed / changed rows, compared by id and whole-row identity."""
    old = {row.id: row for row in before.rows}
    new = {row.id: row for row in after.rows}
    added = [new[key].to_dict() for key in sorted(set(new) - set(old))]
    removed = [old[key].to_dict() for key in sorted(set(old) - set(new))]
    changed = []
    for key in sorted(set(old) & set(new)):
        if _identity(old[key]) != _identity(new[key]):
            changed.append({
                "id": key,
                "before": old[key].to_dict(),
                "after": new[key].to_dict(),
            })
    return {"added": added, "removed": removed, "changed": changed}


def diagnose(composition: Composition) -> list[dict[str, Any]]:
    """Actionable warnings an operator can read before boot."""
    problems = list(composition.diagnostics)
    seen: set[str] = set()
    for row in composition.rows:
        if row.id in seen:
            problems.append({
                "code": "KERNEL_COMPOSITION_DUPLICATE",
                "row": row.id,
                "source": row.source,
                "message": f"row {row.id!r} appears more than once",
            })
        seen.add(row.id)
        if row.disabled and row.name and not row.config:
            problems.append({
                "code": "KERNEL_ROW_DISABLED",
                "row": row.id,
                "source": row.source,
                "message": f"row {row.id!r} is disabled and contributes nothing",
            })
    return problems


def _identity(row: Any) -> tuple[Any, Any, Any, Any]:
    return (
        row.name,
        json.dumps(row.config, sort_keys=True, default=str),
        row.disabled,
        tuple(row.isolate),
    )
