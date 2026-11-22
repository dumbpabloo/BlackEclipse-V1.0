"""Typed runtime feature flags (W0).

There are already hundreds of ad-hoc `HARNESS_*` environment reads across the
harness. This module is the ONE place for **new** cross-cutting flags introduced
by the Hermes-inspired programme. It deliberately does not migrate existing
flags; a migration would be a separate, reviewable change.

Rules:

* stdlib only — importable from boot, scripts and tests without the runtime;
* every flag has a declared kind, default, owner and (usually) a removal date;
* an invalid value resolves to the default and is reported by `validate()`;
* `snapshot()` is stable, sorted and secret-free, so it can be persisted on a
  run for reproduction;
* `HARNESS_FLAG_STRICT=1` turns a known-invalid value into a boot error.

A flag is not a feature. A flag without a kill-switch test is not merged.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Literal, Mapping, Optional

Kind = Literal["bool", "int", "enum", "str"]

_TRUTHY = frozenset({"1", "true", "yes", "on", "enable", "enabled"})
_FALSY = frozenset({"0", "false", "no", "off", "disable", "disabled"})


@dataclass(frozen=True)
class FlagSpec:
    """One declared flag. `name` is always an environment-variable name."""

    name: str
    kind: Kind
    default: str
    values: tuple[str, ...] = ()
    minimum: Optional[int] = None
    maximum: Optional[int] = None
    owner: str = ""
    description: str = ""
    since: str = "w0"
    remove_by: Optional[str] = None

    def resolve(self, environ: Mapping[str, str]) -> str:
        """The effective raw string, falling back to the default on invalid input."""
        raw = environ.get(self.name)
        if raw is None:
            return self.default
        value = str(raw).strip()
        if self.kind == "bool":
            lowered = value.lower()
            if lowered in _TRUTHY:
                return "on"
            if lowered in _FALSY:
                return "off"
            return self.default
        if self.kind == "int":
            try:
                number = int(value)
            except (TypeError, ValueError):
                return self.default
            if self.minimum is not None:
                number = max(self.minimum, number)
            if self.maximum is not None:
                number = min(self.maximum, number)
            return str(number)
        if self.kind == "enum":
            if self.values and value not in self.values:
                return self.default
            return value
        return value

    def error(self, environ: Mapping[str, str]) -> Optional[str]:
        """A human-readable problem with the raw value, or None."""
        raw = environ.get(self.name)
        if raw is None:
            return None
        value = str(raw).strip()
        if self.kind == "bool":
            if value.lower() not in _TRUTHY | _FALSY:
                return f"{self.name}={value!r} is not a boolean; using {self.default!r}"
            return None
        if self.kind == "int":
            try:
                int(value)
            except (TypeError, ValueError):
                return f"{self.name}={value!r} is not an integer; using {self.default!r}"
            return None
        if self.kind == "enum" and self.values and value not in self.values:
            return (
                f"{self.name}={value!r} is not one of {self.values}; "
                f"using {self.default!r}"
            )
        return None


def _spec(
    name: str,
    kind: Kind,
    default: str,
    *,
    values: tuple[str, ...] = (),
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
    owner: str,
    description: str,
    remove_by: Optional[str] = None,
) -> FlagSpec:
    return FlagSpec(
        name=name,
        kind=kind,
        default=default,
        values=values,
        minimum=minimum,
        maximum=maximum,
        owner=owner,
        description=description,
        remove_by=remove_by,
    )


# The W1–W7 flag matrix from docs/harness/roadmap/hermes-inspired-improvement-plan.md.
# Defaults preserve TODAY'S behaviour: W0 must not change runtime semantics.
FLAG_SPECS: dict[str, FlagSpec] = {
    # W1 — model gateway
    "HARNESS_MODEL_GATEWAY": _spec(
        "HARNESS_MODEL_GATEWAY", "enum", "on",
        values=("shadow", "on", "off"), owner="W1",
        description="Route the graph through ModelGateway (shadow/on/off)",
        remove_by="after W1 is green for two weeks",
    ),
    "HARNESS_MODEL_ATTEMPTS": _spec(
        "HARNESS_MODEL_ATTEMPTS", "int", "3", minimum=1, maximum=10, owner="W1",
        description="Physical attempts per model candidate",
        remove_by="replaced by provider policy after W1",
    ),
    "HARNESS_MODEL_FIRST_TOKEN_S": _spec(
        "HARNESS_MODEL_FIRST_TOKEN_S", "int", "45", minimum=5, maximum=300, owner="W1",
        description="Seconds allowed before the first streamed token",
    ),
    "HARNESS_CREDENTIAL_POOL": _spec(
        "HARNESS_CREDENTIAL_POOL", "bool", "on", owner="W1",
        description="Rotate provider credentials on failure",
    ),
    "HARNESS_MODEL_FALLBACK_ALLOW_DEGRADED": _spec(
        "HARNESS_MODEL_FALLBACK_ALLOW_DEGRADED", "bool", "off", owner="W1",
        description="Allow a cross-provider fallback with materially lower declared quality",
    ),
    # W2 — context
    "HARNESS_USAGE_ANCHOR": _spec(
        "HARNESS_USAGE_ANCHOR", "bool", "on", owner="W2",
        description="Gate compression on provider-reported usage",
    ),
    "HARNESS_COMPRESSION_LADDER": _spec(
        "HARNESS_COMPRESSION_LADDER", "enum", "on",
        values=("shadow", "on", "off"), owner="W2",
        description="Prune -> summarise -> deterministic fallback ladder",
    ),
    "HARNESS_CONTEXT_GATE_PCT": _spec(
        "HARNESS_CONTEXT_GATE_PCT", "int", "50", minimum=10, maximum=90,
        owner="W2",
        description="Soft context-pressure gate; hard/emergency follow at +20/+35",
    ),
    # W3 — tools
    "HARNESS_PARALLEL_TOOLS": _spec(
        "HARNESS_PARALLEL_TOOLS", "enum", "on",
        values=("shadow", "on", "off"), owner="W3",
        description="Run contiguous parallel-safe tool calls concurrently (shadow = plan only); "
                    "default on because only explicitly read-only tools opt in",
    ),
    "HARNESS_TOOL_CONCURRENCY": _spec(
        "HARNESS_TOOL_CONCURRENCY", "int", "6", minimum=1, maximum=12, owner="W3",
        description="Maximum concurrent read-only tool calls",
    ),
    "HARNESS_TOOL_BATCH_TIMEOUT_S": _spec(
        "HARNESS_TOOL_BATCH_TIMEOUT_S", "int", "120", minimum=10, maximum=600,
        owner="W3",
        description="Wall-clock ceiling for one read-only tool batch",
    ),
    "HARNESS_TOOL_GUARDRAILS": _spec(
        "HARNESS_TOOL_GUARDRAILS", "enum", "warn",
        values=("warn", "halt", "off"), owner="W3",
        description="Identical-call/cycle/stall guardrails",
    ),
    "HARNESS_TOOL_DISCLOSURE": _spec(
        "HARNESS_TOOL_DISCLOSURE", "enum", "auto",
        values=("auto", "on", "off"), owner="W3",
        description="Tiered progressive tool disclosure",
    ),
    "HARNESS_TOOL_DISCLOSURE_THRESHOLD_PCT": _spec(
        "HARNESS_TOOL_DISCLOSURE_THRESHOLD_PCT", "int", "8",
        minimum=1, maximum=50, owner="W3",
        description="Advertised-schema budget before disclosure activates",
    ),
    # W4 — autonomy. `HARNESS_AUTONOMY` predates W4 and is the GLOBAL
    # unattended-action kill switch (read by recovery/envelope.py; absent = on).
    # W4's engine has its own mode flag so enabling the goal loop cannot silently
    # change the recovery envelope's default.
    "HARNESS_AUTONOMY": _spec(
        "HARNESS_AUTONOMY", "bool", "off", owner="W4",
        description="Global kill switch for unattended actions (off vetoes W4)",
    ),
    "HARNESS_GOAL_LOOP": _spec(
        "HARNESS_GOAL_LOOP", "enum", "on",
        values=("off", "shadow", "on"), owner="W4",
        description="Goal gates/judge/continuation engine (on|shadow|off)",
    ),
    "HARNESS_GOAL_MAX_TURNS": _spec(
        "HARNESS_GOAL_MAX_TURNS", "int", "20", minimum=1, maximum=100,
        owner="W4", description="Hard ceiling for a goal's turns",
    ),
    "HARNESS_GOAL_GATE_TIMEOUT_S": _spec(
        "HARNESS_GOAL_GATE_TIMEOUT_S", "int", "600", minimum=30, maximum=3600,
        owner="W4", description="Ceiling per deterministic gate execution",
    ),
    "HARNESS_GOAL_JUDGE_PROVIDER": _spec(
        "HARNESS_GOAL_JUDGE_PROVIDER", "str", "", owner="W4",
        description="Optional provider for the advisory goal judge",
    ),
    "HARNESS_GOAL_JUDGE_MODEL": _spec(
        "HARNESS_GOAL_JUDGE_MODEL", "str", "", owner="W4",
        description="Optional model for the advisory goal judge",
    ),
    "HARNESS_GOAL_MAX_TOKENS": _spec(
        "HARNESS_GOAL_MAX_TOKENS", "int", "0", minimum=0, maximum=10_000_000,
        owner="W4", description="Optional per-turn token cap (0 = no extra cap)",
    ),
    "HARNESS_LOOP_MIN_INTERVAL_S": _spec(
        "HARNESS_LOOP_MIN_INTERVAL_S", "int", "60", minimum=30, maximum=86_400,
        owner="W4", description="Minimum loop/heartbeat interval",
    ),
    "HARNESS_LOOP_MAX_TICKS": _spec(
        "HARNESS_LOOP_MAX_TICKS", "int", "50", minimum=1, maximum=1_000,
        owner="W4", description="Default maximum ticks for a loop",
    ),
    "HARNESS_LOOP_MAX_NO_CHANGE": _spec(
        "HARNESS_LOOP_MAX_NO_CHANGE", "int", "3", minimum=1, maximum=20,
        owner="W4", description="Auto-pause after this many unchanged replies",
    ),
    # W5 — learning
    "HARNESS_SKILL_EXEC_EVAL": _spec(
        "HARNESS_SKILL_EXEC_EVAL", "bool", "on", owner="W5",
        description="Require behavioural evaluation before a skill can enable",
    ),
    "HARNESS_SKILL_CURATOR": _spec(
        "HARNESS_SKILL_CURATOR", "enum", "dry_run",
        values=("dry_run", "propose", "apply", "off"), owner="W5",
        description="Background skill consolidation mode",
    ),
    "HARNESS_MEMORY_HYBRID": _spec(
        "HARNESS_MEMORY_HYBRID", "bool", "off", owner="W5",
        description="BM25/lexical hybrid memory retrieval",
    ),
    "HARNESS_SKILL_EVAL_RETRY_HOURS": _spec(
        "HARNESS_SKILL_EVAL_RETRY_HOURS", "int", "12",
        minimum=0, maximum=720, owner="W5",
        description="Backoff before a failed/needs-review execution eval is retried",
    ),
    # W6 — run kernel / safety
    "HARNESS_FENCED_PROGRESS": _spec(
        "HARNESS_FENCED_PROGRESS", "bool", "on", owner="W6",
        description="Fence progress counter writes to the live lease",
    ),
    "HARNESS_CHECKPOINT_ATOMIC": _spec(
        "HARNESS_CHECKPOINT_ATOMIC", "bool", "on", owner="W6",
        description="Commit checkpoint + message cursor in one boundary",
    ),
    "HARNESS_EVENT_SEQ": _spec(
        "HARNESS_EVENT_SEQ", "bool", "on", owner="W6",
        description="Allocate durable per-run event sequence in the store",
    ),
    "HARNESS_EFFECT_TOKENS": _spec(
        "HARNESS_EFFECT_TOKENS", "enum", "shadow",
        values=("shadow", "enforce", "off"), owner="W6",
        description="Effect-token enforcement mode (default preserves today)",
    ),
    # W7 — delegation / files
    "HARNESS_FILE_CHECKPOINTS": _spec(
        "HARNESS_FILE_CHECKPOINTS", "enum", "warn",
        values=("off", "warn", "enforce", "on", "true", "false", "1", "0"),
        owner="W7",
        description="Shadow-git file checkpoints (off/warn/enforce; on aliases enforce); "
                    "default warn = snapshots are taken but a missing undo point never blocks a write",
    ),
    "HARNESS_FILE_CHECKPOINT_MAX_MB": _spec(
        "HARNESS_FILE_CHECKPOINT_MAX_MB", "int", "500", minimum=50, maximum=5000,
        owner="W7", description="Maximum checkpoint-store size per project (MB)",
    ),
    "HARNESS_FILE_CHECKPOINT_RETENTION": _spec(
        "HARNESS_FILE_CHECKPOINT_RETENTION", "int", "50", minimum=1, maximum=500,
        owner="W7", description="Snapshots kept per project",
    ),
    "HARNESS_ASYNC_DELEGATION": _spec(
        "HARNESS_ASYNC_DELEGATION", "bool", "off", owner="W7",
        description="Background delegation with status/steer/cancel",
    ),
    "HARNESS_DELEGATION_SCHEMA_REPAIR": _spec(
        "HARNESS_DELEGATION_SCHEMA_REPAIR", "bool", "on", owner="W7",
        description="Allow one bounded repair turn on a schema miss",
    ),
    "HARNESS_DELEGATION_STEER_MAX": _spec(
        "HARNESS_DELEGATION_STEER_MAX", "int", "4", minimum=0, maximum=8,
        owner="W7", description="Pending steer messages allowed per child",
    ),
    "HARNESS_DELEGATION_COMPLETION": _spec(
        "HARNESS_DELEGATION_COMPLETION", "enum", "card",
        values=("card", "continue", "off"), owner="W7",
        description="How an async delegation completion is surfaced",
    ),
    # Existing evidence seam (declared here so the snapshot is complete).
    "HARNESS_EVIDENCE_ENFORCE": _spec(
        "HARNESS_EVIDENCE_ENFORCE", "bool", "off", owner="W5",
        description="Disclose unsupported evidence claims in the answer",
    ),
    "HARNESS_FLAG_STRICT": _spec(
        "HARNESS_FLAG_STRICT", "bool", "off", owner="W0",
        description="Fail boot on a known-invalid flag value",
    ),
}


def _env(environ: Optional[Mapping[str, str]] = None) -> Mapping[str, str]:
    return environ if environ is not None else os.environ


def get_flag(name: str, *, environ: Optional[Mapping[str, str]] = None) -> str:
    """Resolve a declared flag to its effective string value."""
    spec = FLAG_SPECS.get(name)
    if spec is None:
        # Unknown names are not silently invented. Callers that need a new flag
        # must declare it in FLAG_SPECS (and add a kill-switch test).
        raw = _env(environ).get(name)
        return "" if raw is None else str(raw).strip()
    return spec.resolve(_env(environ))


def get_bool(name: str, *, environ: Optional[Mapping[str, str]] = None) -> bool:
    return get_flag(name, environ=environ) == "on"


def get_int(name: str, *, environ: Optional[Mapping[str, str]] = None) -> int:
    raw = get_flag(name, environ=environ)
    try:
        return int(raw)
    except (TypeError, ValueError):
        spec = FLAG_SPECS.get(name)
        return int(spec.default) if spec is not None else 0


def snapshot(*, environ: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """Every declared flag's effective value, sorted by name.

    This is safe to persist: no flag in this registry is a secret. If a future
    flag is secret, it must not be added here; use the secrets vault instead.
    """
    env = _env(environ)
    return {name: FLAG_SPECS[name].resolve(env) for name in sorted(FLAG_SPECS)}


def snapshot_hash(values: Optional[Mapping[str, str]] = None) -> str:
    """A stable digest of a flag snapshot (order-independent)."""
    payload = dict(values or snapshot())
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate(*, environ: Optional[Mapping[str, str]] = None) -> list[str]:
    """Return one message per known-invalid flag value. Never raises."""
    env = _env(environ)
    return [
        problem
        for name in sorted(FLAG_SPECS)
        if (problem := FLAG_SPECS[name].error(env)) is not None
    ]


def strict_enabled(*, environ: Optional[Mapping[str, str]] = None) -> bool:
    return get_bool("HARNESS_FLAG_STRICT", environ=environ)


def assert_valid(*, environ: Optional[Mapping[str, str]] = None) -> None:
    """Raise when strict mode is on and a known flag value is invalid.

    Call this from boot after configuration is loaded. It is intentionally not
    called at import time: tests and scripts may set flags after import.
    """
    if not strict_enabled(environ=environ):
        return
    problems = validate(environ=environ)
    if problems:
        raise ValueError("invalid harness flag value(s): " + "; ".join(problems))


__all__ = [
    "FLAG_SPECS",
    "FlagSpec",
    "assert_valid",
    "get_bool",
    "get_flag",
    "get_int",
    "snapshot",
    "snapshot_hash",
    "strict_enabled",
    "validate",
]
