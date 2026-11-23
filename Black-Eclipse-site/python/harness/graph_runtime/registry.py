"""Node registry — the runtime side of the node SDK.

Every node type the runtime can compile/execute declares itself ONCE here: its
typed ports, its config fields (validated at the trust boundary), the
capabilities it needs, its side-effect class, the modes it may run in, and the
executor that implements it.

The registry is intentionally data + one pure callable per node. It is the
runtime counterpart of the TypeScript registry in
``src/lib/graph-studio/registry.ts`` — the TS side drives the editor, this side
gates execution. A node type with no executor is still REGISTERED (so the
compiler can name it precisely and say which phase brings it) but cannot run.
"""

from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Optional

from .model import (
    ExecutionMode,
    PortDef,
    PortKind,
    PortType,
    SideEffect,
)

log = logging.getLogger("harness.graph_runtime.registry")

ConfigKind = Literal["any", "string", "number", "integer", "boolean", "enum", "object", "array"]


@dataclass(frozen=True)
class ConfigField:
    """One declared config field. This IS the schema: unknown fields are kept
    (forward-compat) but declared fields are type/range-checked."""

    name: str
    kind: ConfigKind
    default: Any = None
    enum: tuple[str, ...] = ()
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    max_length: Optional[int] = None


@dataclass
class NodeExecContext:
    """What an executor receives. Pure and self-contained: no store, no network,
    no runtime handles — an executor computes from its inputs and config only.
    """

    node_id: str
    run_id: str
    mode: ExecutionMode
    inputs: dict[str, Any]
    config: dict[str, Any]
    # The persisted workflow identity for this run. A live mandate pins it, so an
    # edited graph cannot reuse an old mandate.
    workflow_id: str = ""
    emit: Callable[[str], None] = lambda _msg: None  # noqa: E731 - trivial default
    # The caller-supplied input for this run. A subworkflow receives its
    # parent node's input here; a top-level run normally leaves it None.
    run_input: Any = None
    # The node contract version being executed. Used only to keep an older
    # version's output shape stable while a newer version adds outputs.
    node_version: str = ""
    # Bounded context for an inbound trigger (a verified webhook: body,
    # headers, query, method, receivedAt, webhookId, deliveryId). Authenticated
    # by the front door; never contains a secret or a signature.
    trigger_context: Optional[dict[str, Any]] = None
    # Model credentials are resolved by the front door and held IN MEMORY for
    # this run only. They are deliberately excluded from the durable Fabric job
    # payload, so a restart makes agent nodes fail honestly rather than persist a
    # key to disk.
    model_settings: Optional[dict[str, Any]] = None
    callback_base_url: str = ""
    callback_token: Optional[str] = None
    user_id: str = "local"
    # True only for a durable Fabric job: a wait node may then defer the job
    # instead of blocking the worker thread.
    allow_defer: bool = False
    # The run's effect envelope. Agent nodes narrow their tool surface when a
    # run has no network grant (a test/dry run), so the model cannot reach web,
    # browser, MCP or broker tools while it can still reason and read locally.
    allow_network: bool = False
    test_run: bool = False
    # True for an operator run (the reduced test envelope is False). The skill
    # runtime's actor carries this so an operator-authored permission set is
    # honoured — and a test run never widens it.
    operator: bool = False
    # Which of THIS node's wired control inputs fired (keyed by input port id).
    # A gate can then express an AND ("every wired branch arrived") without the
    # scheduler special-casing node types. Empty when nothing is wired.
    control_fired: dict[str, bool] = field(default_factory=dict)
    # Polled by long-running but pure nodes (`logic.wait`) so cancellation does
    # not have to wait for the node to return. Never a resource handle.
    should_cancel: Callable[[], bool] = lambda: False  # noqa: E731


@dataclass
class NodeDef:
    type: str
    version: str
    label: str
    category: str
    inputs: tuple[PortDef, ...] = ()
    outputs: tuple[PortDef, ...] = ()
    config_fields: tuple[ConfigField, ...] = ()
    capabilities: tuple[str, ...] = ()
    side_effects: SideEffect = SideEffect.NONE
    allowed_modes: tuple[ExecutionMode, ...] = tuple(ExecutionMode)
    terminal: bool = False
    timeout_ms: int = 30_000
    max_attempts: int = 1
    executor: Optional[Callable[[NodeExecContext], dict[str, Any]]] = None
    # The executor handles this node itself (e.g. a subworkflow runs a nested
    # plan). It is executable without a plain `executor` function.
    orchestrated: bool = False
    # A node that, when it succeeds, ends the run early (its downstream nodes
    # become `skipped`). Used by `logic.stop`.
    stops_run: bool = False
    # Config fields a TEST run tightens: field name → the largest value a test
    # run may use. Keeps a bounded `wait` from stalling a test (Phase 4).
    test_run_limits: dict[str, float] = field(default_factory=dict)
    # Set when the node is declared but not yet executable — names the phase
    # that brings its executor so diagnostics are actionable, never vague.
    since_phase: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        return self.executor is not None or self.orchestrated

    def input_port(self, handle: str) -> Optional[PortDef]:
        return next((p for p in self.inputs if p.id == handle), None)

    def output_port(self, handle: str) -> Optional[PortDef]:
        return next((p for p in self.outputs if p.id == handle), None)


class NodeRegistry:
    """A registry of node definitions keyed by type@version."""

    def __init__(self) -> None:
        self._defs: dict[tuple[str, str], NodeDef] = {}

    def register(self, definition: NodeDef) -> NodeDef:
        self._defs[(definition.type, definition.version)] = definition
        return definition

    def get(self, node_type: str, version: Optional[str] = None) -> Optional[NodeDef]:
        if version:
            return self._defs.get((node_type, version))
        matches = [d for (t, _v), d in self._defs.items() if t == node_type]
        if not matches:
            return None
        return sorted(matches, key=lambda d: _version_key(d.version))[-1]

    def indexed(self) -> dict[str, NodeDef]:
        """Every definition keyed by ``type@version`` (a stable lookup for tests)."""
        return {f"{d.type}@{d.version}": d for d in self._defs.values()}

    def definitions(self) -> list[NodeDef]:
        return list(self._defs.values())


def _version_key(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in version.split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    return tuple(parts)


# ── Config ──────────────────────────────────────────────────────────────────


def config_defaults(definition: NodeDef) -> dict[str, Any]:
    return {f.name: f.default for f in definition.config_fields}


def coerce_config(definition: NodeDef, config: dict[str, Any]) -> dict[str, Any]:
    """Fill declared defaults and coerce numeric fields. Never guesses types for
    fields it does not know — unknown keys pass through untouched."""
    merged: dict[str, Any] = {f.name: f.default for f in definition.config_fields}
    for key, value in (config or {}).items():
        merged[key] = value
    for f in definition.config_fields:
        value = merged.get(f.name)
        if f.kind in ("number", "integer") and isinstance(value, str):
            try:
                merged[f.name] = float(value) if f.kind == "number" else int(float(value))
            except ValueError:
                pass
    return merged


def validate_config(definition: NodeDef, config: dict[str, Any]) -> list[str]:
    """Return human-readable problems with a config, or [] when valid."""
    problems: list[str] = []
    merged = coerce_config(definition, config)
    for f in definition.config_fields:
        value = merged.get(f.name)
        if value is None:
            if f.default is None:
                continue
            problems.append(f"{f.name}: missing value")
            continue
        if f.kind == "string":
            if not isinstance(value, str):
                problems.append(f"{f.name}: expected a string")
            elif f.max_length is not None and len(value) > f.max_length:
                problems.append(f"{f.name}: exceeds {f.max_length} characters")
        elif f.kind == "boolean":
            if not isinstance(value, bool):
                problems.append(f"{f.name}: expected true or false")
        elif f.kind in ("number", "integer"):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                problems.append(f"{f.name}: expected a number")
            else:
                if f.kind == "integer" and float(value) != int(value):
                    problems.append(f"{f.name}: expected a whole number")
                if f.minimum is not None and value < f.minimum:
                    problems.append(f"{f.name}: must be at least {f.minimum}")
                if f.maximum is not None and value > f.maximum:
                    problems.append(f"{f.name}: must be at most {f.maximum}")
        elif f.kind == "enum":
            if value not in f.enum:
                problems.append(f"{f.name}: expected one of {', '.join(f.enum)}")
        elif f.kind == "any":
            # Deliberately unconstrained: a JSON value (object, array, string,
            # number, boolean). The editor's zod schema mirrors this.
            pass
        elif f.kind == "object" and not isinstance(value, dict):
            problems.append(f"{f.name}: expected an object")
        elif f.kind == "array" and not isinstance(value, list):
            problems.append(f"{f.name}: expected an array")
    return problems


# ── Ports ───────────────────────────────────────────────────────────────────


def ports_compatible(source: PortDef, target: PortDef) -> bool:
    """Control ports only connect to control ports; `any` matches anything;
    a `ticket` accepts an id as a number or a string (MT5 vs REST); otherwise
    the declared types must match exactly (no silent coercion)."""
    source_control = source.kind == PortKind.CONTROL
    target_control = target.kind == PortKind.CONTROL
    if source_control or target_control:
        return source_control and target_control
    if source.type == PortType.ANY or target.type == PortType.ANY:
        return True
    if PortType.TICKET in (source.type, target.type):
        return source.type == target.type or {
            source.type, target.type,
        } <= {PortType.TICKET, PortType.NUMBER, PortType.STRING}
    return source.type == target.type


def _is_iso_datetime(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def validate_port_value(port_type: PortType, value: Any) -> Optional[str]:
    """Validate an executor's output against its declared port type.

    Primitive types are checked exactly. Domain types (ohlcv, indicator, …) are
    checked STRUCTURALLY — enough to catch a clearly-wrong shape (a scalar where
    a series is declared) without pretending to be a full market-data schema.
    Phase 5 tightens these with the finance engines' own contracts.
    """
    if value is None:
        return None  # `null` is always allowed; `nullable` is documentation today
    if port_type == PortType.ANY:
        return None
    if port_type == PortType.NUMBER:
        return None if isinstance(value, (int, float)) and not isinstance(value, bool) else "expected a number"
    if port_type == PortType.TICKET:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            return "expected an order/position id (a number or a string)"
        return None if str(value).strip() else "expected a non-empty order/position id"
    if port_type == PortType.BOOLEAN:
        return None if isinstance(value, bool) else "expected a boolean"
    if port_type == PortType.STRING:
        return None if isinstance(value, str) else "expected a string"
    if port_type == PortType.OBJECT:
        return None if isinstance(value, dict) else "expected an object"
    if port_type == PortType.ARRAY:
        return None if isinstance(value, list) else "expected an array"
    if port_type == PortType.DATETIME:
        return None if _is_iso_datetime(value) else "expected an ISO-8601 datetime string"
    if port_type == PortType.OHLCV:
        if isinstance(value, dict) and isinstance(value.get("rows"), list):
            return None
        if isinstance(value, list) and all(isinstance(r, dict) for r in value):
            return None
        return "expected an OHLCV series (a list of rows or { rows: [...] })"
    if port_type == PortType.INDICATOR:
        if isinstance(value, dict) and isinstance(value.get("values"), list):
            return None
        if isinstance(value, list) and all(isinstance(v, (int, float)) for v in value):
            return None
        return "expected an indicator series (a list of numbers or { values: [...] })"
    if port_type == PortType.INSTRUMENT:
        return None if isinstance(value, dict) and isinstance(value.get("symbol"), str) else "expected an instrument ({ symbol, ... })"
    if port_type == PortType.SIGNAL:
        return None if isinstance(value, dict) else "expected a signal object"
    if port_type == PortType.PORTFOLIO:
        return None if isinstance(value, dict) and isinstance(value.get("positions", []), list) else "expected a portfolio object"
    if port_type == PortType.ORDER_INTENT:
        if isinstance(value, dict) and "symbol" in value and "side" in value:
            return None
        return "expected an order intent ({ symbol, side, quantity, ... })"
    if port_type in (
        PortType.RESEARCH_FINDING,
        PortType.EVIDENCE,
        PortType.AGENT_RESULT,
        PortType.EXECUTION_RESULT,
    ):
        return None if isinstance(value, dict) else f"expected a {port_type.value} object"
    if port_type in (PortType.FILE, PortType.IMAGE):
        if isinstance(value, dict) and any(
            isinstance(value.get(key), str) and value.get(key)
            for key in ("assetId", "asset_id", "ref")
        ):
            return None
        return f"expected a {port_type.value} reference ({{ assetId, name, ... }})"
    return f"unknown port type {port_type.value}"


__all__ = [
    "ConfigField",
    "NodeExecContext",
    "NodeDef",
    "NodeRegistry",
    "config_defaults",
    "coerce_config",
    "validate_config",
    "ports_compatible",
    "validate_port_value",
]
