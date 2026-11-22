"""Composition: rows, ordered patch layers and safe declarative expressions.

A composition is a list of addressable rows. A patch can insert rows, replace a
row's whole config, disable it, or remove it. Layers apply in order, so the same
mechanism serves installation bundles, a profile patch and a user overlay.

There is no code evaluation. Expressions are a closed vocabulary:

    {"$env": "NAME", "$default": value}
    {"$ref": "settings.path", "$default": value}
    {"$eq": [a, b]} / {"$ne": [a, b]}
    {"$and": [a, b, ...]} / {"$or": [...]} / {"$not": a}
    {"$literal": value}

Anything else is rejected at composition time, before a plugin is imported.
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

from .errors import CompositionError

ROW_KEYS = {"id", "name", "config", "disabled", "group", "isolate", "when"}
PATCH_KEYS = {"insert", "remove", "id", "name", "config", "disabled", "group", "isolate", "when"}
EXPR_KEYS = {"$env", "$ref", "$default", "$eq", "$ne", "$and", "$or", "$not", "$literal"}


@dataclass
class Row:
    id: str
    name: str
    config: Any = None
    disabled: bool = False
    group: bool = False
    isolate: tuple[str, ...] = ()
    when: Any = None
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"id": self.id, "name": self.name}
        if self.config is not None:
            data["config"] = self.config
        if self.disabled:
            data["disabled"] = True
        if self.group:
            data["group"] = True
        if self.isolate:
            data["isolate"] = list(self.isolate)
        if self.source:
            data["source"] = self.source
        return data


@dataclass
class Composition:
    rows: list[Row] = field(default_factory=list)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)

    def enabled(self) -> list[Row]:
        return [row for row in self.rows if not row.disabled]

    def by_id(self, row_id: str) -> Optional[Row]:
        for row in self.rows:
            if row.id == row_id:
                return row
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows": [row.to_dict() for row in self.rows],
            "diagnostics": self.diagnostics,
        }


@dataclass
class Layer:
    source: str
    patch: list[dict[str, Any]]


def compose(layers: Iterable[Layer], *, env: Optional[Mapping[str, str]] = None,
            refs: Optional[Mapping[str, Any]] = None) -> Composition:
    composition = Composition()
    for layer in layers:
        _apply_patch(composition, layer, env=env or os.environ, refs=refs or {})
    return composition


def _apply_patch(composition: Composition, layer: Layer, *, env: Mapping[str, str], refs: Mapping[str, Any]) -> None:
    if not isinstance(layer.patch, list):
        raise CompositionError(
            "KERNEL_COMPOSITION_SHAPE", "a patch must be a list of operations",
            source=layer.source,
        )
    for index, operation in enumerate(layer.patch):
        if not isinstance(operation, dict):
            raise CompositionError("KERNEL_COMPOSITION_SHAPE", "patch item must be a mapping", source=layer.source)
        unknown = set(operation) - PATCH_KEYS
        if unknown:
            raise CompositionError(
                "KERNEL_COMPOSITION_KEY", f"unknown patch key(s): {', '.join(sorted(unknown))}",
                source=layer.source,
            )
        if "insert" in operation:
            inserted = operation["insert"]
            if not isinstance(inserted, list):
                raise CompositionError("KERNEL_COMPOSITION_SHAPE", "insert must be a list", source=layer.source)
            for raw in inserted:
                row = _parse_row(raw, layer.source, env=env, refs=refs)
                if composition.by_id(row.id) is not None:
                    raise CompositionError(
                        "KERNEL_COMPOSITION_DUPLICATE", f"duplicate row id {row.id!r}",
                        source=layer.source, row=row.id,
                    )
                composition.rows.append(row)
            continue
        if "remove" in operation:
            row_id = str(operation["remove"] or "")
            existing = composition.by_id(row_id)
            if existing is None:
                composition.diagnostics.append({
                    "code": "KERNEL_COMPOSITION_UNKNOWN_ROW",
                    "row": row_id,
                    "source": layer.source,
                    "message": f"remove targets unknown row {row_id!r}",
                })
                continue
            composition.rows.remove(existing)
            continue
        row_id = str(operation.get("id") or "")
        existing = composition.by_id(row_id)
        if existing is None:
            composition.diagnostics.append({
                "code": "KERNEL_COMPOSITION_UNKNOWN_ROW",
                "row": row_id,
                "source": layer.source,
                "message": f"patch targets unknown row {row_id!r}",
            })
            continue
        if "name" in operation:
            existing.name = str(operation["name"])
        if "config" in operation:
            existing.config = _resolve(operation["config"], env, refs)
        if "disabled" in operation:
            existing.disabled = bool(_resolve(operation["disabled"], env, refs))
        if "isolate" in operation:
            existing.isolate = tuple(str(item) for item in operation["isolate"] or ())
        if "when" in operation:
            existing.when = _resolve(operation["when"], env, refs)
        existing.source = layer.source
        if "name" in operation or "config" in operation:
            # A config replacement is whole-row: the last writer wins.
            pass


def _parse_row(raw: Any, source: str, *, env: Mapping[str, str], refs: Mapping[str, Any]) -> Row:
    if not isinstance(raw, dict):
        raise CompositionError("KERNEL_COMPOSITION_SHAPE", "insert row must be a mapping", source=source)
    unknown = set(raw) - ROW_KEYS
    if unknown:
        raise CompositionError(
            "KERNEL_COMPOSITION_KEY", f"unknown row key(s): {', '.join(sorted(unknown))}", source=source,
        )
    row_id = str(raw.get("id") or "").strip()
    name = str(raw.get("name") or "").strip()
    if not row_id:
        raise CompositionError("KERNEL_COMPOSITION_ID", "insert row needs an id", source=source)
    if not name:
        raise CompositionError("KERNEL_COMPOSITION_NAME", f"row {row_id!r} needs a name", source=source, row=row_id)
    isolate = raw.get("isolate") or ()
    if isinstance(isolate, str):
        isolate = (isolate,)
    return Row(
        id=row_id,
        name=name,
        config=_resolve(raw.get("config"), env, refs),
        disabled=bool(_resolve(raw.get("disabled", False), env, refs)),
        group=bool(raw.get("group", False)),
        isolate=tuple(str(item) for item in isolate),
        when=_resolve(raw.get("when"), env, refs),
        source=source,
    )


def _resolve(value: Any, env: Mapping[str, str], refs: Mapping[str, Any]) -> Any:
    if isinstance(value, list):
        return [_resolve(item, env, refs) for item in value]
    if not isinstance(value, dict):
        return value
    dollar = {key for key in value if isinstance(key, str) and key.startswith("$")}
    if not dollar:
        return {key: _resolve(item, env, refs) for key, item in value.items()}
    unknown = dollar - EXPR_KEYS
    if unknown:
        raise CompositionError(
            "KERNEL_COMPOSITION_EXPR", f"unknown expression key(s): {', '.join(sorted(unknown))}",
        )
    if "$literal" in value:
        return copy.deepcopy(value["$literal"])
    if "$env" in value:
        name = str(value["$env"])
        fallback = _resolve(value.get("$default"), env, refs)
        raw = env.get(name)
        return fallback if raw is None else _coerce(raw)
    if "$ref" in value:
        fallback = _resolve(value.get("$default"), env, refs)
        return _lookup(refs, str(value["$ref"]), fallback)
    if "$eq" in value or "$ne" in value:
        left, right = _pair(value.get("$eq") or value.get("$ne"), env, refs)
        return (left == right) if "$eq" in value else (left != right)
    if "$and" in value:
        return all(bool(_resolve(item, env, refs)) for item in _list(value["$and"]))
    if "$or" in value:
        return any(bool(_resolve(item, env, refs)) for item in _list(value["$or"]))
    if "$not" in value:
        return not bool(_resolve(value["$not"], env, refs))
    raise CompositionError("KERNEL_COMPOSITION_EXPR", "empty expression")


def _pair(value: Any, env: Mapping[str, str], refs: Mapping[str, Any]) -> tuple[Any, Any]:
    items = _list(value)
    if len(items) != 2:
        raise CompositionError("KERNEL_COMPOSITION_EXPR", "$eq/$ne need exactly two values")
    return _resolve(items[0], env, refs), _resolve(items[1], env, refs)


def _list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return [value]


def _lookup(refs: Mapping[str, Any], path: str, fallback: Any) -> Any:
    current: Any = refs
    for part in path.split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        else:
            return fallback
    return current


def _coerce(raw: str) -> Any:
    lowered = raw.strip().lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return raw
