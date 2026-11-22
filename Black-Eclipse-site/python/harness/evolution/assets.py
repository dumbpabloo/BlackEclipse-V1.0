"""The shared evolution asset contract.

One vocabulary for every evolvable thing: prompt sections, skills, tool policy,
hooks, agent presets, composition rows, plugin bundles, memory policy and model
routes. A mutation is a closed operation (add/patch/retire/attach/detach/tune)
over a closed asset kind; the content is validated per kind before admission.

This module is deliberately storage-free. It defines identity, validation and
the candidate state machine; the durable store and the evaluator are wired in
later phases over the existing ``EvolutionCandidate``/``EvaluationRun`` records.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

ASSET_KINDS = (
    "prompt_section",
    "skill",
    "tool_policy",
    "hook",
    "agent_preset",
    "composition_row",
    "plugin_bundle",
    "memory_policy",
    "model_route",
)
MUTATION_OPS = ("add", "patch", "retire", "attach", "detach", "tune")
STATUSES = (
    "inert",
    "proposed",
    "admitted",
    "evaluating",
    "measured_ok",
    "measured_bad",
    "unmeasured",
    "refused",
    "canaried",
    "applied",
    "rolled_back",
    "retired",
)


class AssetError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class AssetKind(str, Enum):
    PROMPT_SECTION = "prompt_section"
    SKILL = "skill"
    TOOL_POLICY = "tool_policy"
    HOOK = "hook"
    AGENT_PRESET = "agent_preset"
    COMPOSITION_ROW = "composition_row"
    PLUGIN_BUNDLE = "plugin_bundle"
    MEMORY_POLICY = "memory_policy"
    MODEL_ROUTE = "model_route"


class MutationOp(str, Enum):
    ADD = "add"
    PATCH = "patch"
    RETIRE = "retire"
    ATTACH = "attach"
    DETACH = "detach"
    TUNE = "tune"


class CandidateStatus(str, Enum):
    INERT = "inert"
    PROPOSED = "proposed"
    ADMITTED = "admitted"
    EVALUATING = "evaluating"
    MEASURED_OK = "measured_ok"
    MEASURED_BAD = "measured_bad"
    UNMEASURED = "unmeasured"
    REFUSED = "refused"
    CANARIED = "canaried"
    APPLIED = "applied"
    ROLLED_BACK = "rolled_back"
    RETIRED = "retired"


_REQUIRED: dict[str, tuple[str, ...]] = {
    "prompt_section": ("text",),
    "skill": ("name", "instructions"),
    "tool_policy": (),
    "hook": ("name",),
    "agent_preset": (),
    "composition_row": ("id", "name"),
    "plugin_bundle": ("name",),
    "memory_policy": (),
    "model_route": ("provider", "model"),
}


@dataclass(frozen=True)
class Mutation:
    op: MutationOp
    kind: AssetKind
    target: str
    content: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""
    expected_effect: str = ""

    def validate(self) -> None:
        if not self.target.strip():
            raise AssetError("EVOLUTION_TARGET_REQUIRED", "a mutation needs a target id")
        if not self.rationale.strip():
            raise AssetError("EVOLUTION_RATIONALE_REQUIRED", "a mutation needs a rationale")
        if self.op is MutationOp.RETIRE:
            if self.content:
                raise AssetError("EVOLUTION_CONTENT_INVALID", "retire takes no content")
            return
        for key in _REQUIRED[self.kind.value]:
            if not str(self.content.get(key) or "").strip():
                raise AssetError(
                    "EVOLUTION_CONTENT_INVALID",
                    f"{self.kind.value} mutation needs a non-empty {key!r}",
                )
        text = self.content.get("text")
        if self.kind is AssetKind.PROMPT_SECTION and len(str(text)) > 20_000:
            raise AssetError("EVOLUTION_CONTENT_INVALID", "prompt section exceeds 20000 characters")

    def to_dict(self) -> dict[str, Any]:
        return {
            "op": self.op.value,
            "kind": self.kind.value,
            "target": self.target,
            "content": self.content,
            "rationale": self.rationale,
            "expectedEffect": self.expected_effect,
        }


@dataclass
class Candidate:
    id: str
    kind: AssetKind
    target: str
    mutations: list[Mutation]
    status: CandidateStatus = CandidateStatus.PROPOSED
    parent_id: Optional[str] = None
    evidence_refs: list[str] = field(default_factory=list)
    created_at: str = ""
    applied_version: Optional[int] = None
    refusal: str = ""
    expires_at: Optional[str] = None
    watch_keys: list[str] = field(default_factory=list)
    decision: dict[str, Any] = field(default_factory=dict)
    recovered: bool = False

    @property
    def digest(self) -> str:
        return mutation_digest(self.mutations)

    def validate(self) -> None:
        if not self.mutations:
            raise AssetError("EVOLUTION_MUTATION_REQUIRED", "a candidate needs at least one mutation")
        for mutation in self.mutations:
            if mutation.kind is not self.kind:
                raise AssetError(
                    "EVOLUTION_KIND_MISMATCH",
                    f"candidate kind {self.kind.value!r} does not match mutation {mutation.kind.value!r}",
                )
            if mutation.target != self.target:
                raise AssetError(
                    "EVOLUTION_TARGET_MISMATCH",
                    f"candidate target {self.target!r} does not match mutation {mutation.target!r}",
                )
            mutation.validate()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "target": self.target,
            "mutations": [mutation.to_dict() for mutation in self.mutations],
            "status": self.status.value,
            "parentId": self.parent_id,
            "evidenceRefs": self.evidence_refs,
            "digest": self.digest,
            "createdAt": self.created_at,
            "appliedVersion": self.applied_version,
            "refusal": self.refusal,
            "expiresAt": self.expires_at,
            "watchKeys": self.watch_keys,
            "decision": self.decision,
            "recovered": self.recovered,
        }


def new_candidate(
    *,
    kind: AssetKind | str,
    target: str,
    mutations: list[Mutation] | list[dict[str, Any]],
    parent_id: Optional[str] = None,
    evidence_refs: Optional[list[str]] = None,
) -> Candidate:
    resolved_kind = kind if isinstance(kind, AssetKind) else AssetKind(str(kind))
    parsed = [
        item if isinstance(item, Mutation) else _parse_mutation(item)
        for item in mutations
    ]
    candidate = Candidate(
        id=f"evo_{uuid.uuid4().hex[:12]}",
        kind=resolved_kind,
        target=str(target),
        mutations=parsed,
        parent_id=parent_id,
        evidence_refs=list(evidence_refs or []),
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    candidate.validate()
    return candidate


def _parse_mutation(raw: dict[str, Any]) -> Mutation:
    try:
        op = raw["op"]
        kind = raw["kind"]
        return Mutation(
            op=op if isinstance(op, MutationOp) else MutationOp(str(op)),
            kind=kind if isinstance(kind, AssetKind) else AssetKind(str(kind)),
            target=str(raw["target"]),
            content=dict(raw.get("content") or {}),
            rationale=str(raw.get("rationale") or ""),
            expected_effect=str(raw.get("expectedEffect") or raw.get("expected_effect") or ""),
        )
    except (KeyError, ValueError) as exc:
        raise AssetError("EVOLUTION_MUTATION_INVALID", f"invalid mutation: {exc}") from exc


def mutation_digest(mutations: list[Mutation]) -> str:
    payload = [mutation.to_dict() for mutation in mutations]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return "emd_" + hashlib.sha256(encoded).hexdigest()[:24]


def candidate_from_dict(raw: dict[str, Any]) -> Candidate:
    candidate = Candidate(
        id=str(raw["id"]),
        kind=AssetKind(str(raw["kind"])),
        target=str(raw["target"]),
        mutations=[_parse_mutation(item) for item in raw.get("mutations") or []],
        status=CandidateStatus(str(raw.get("status", "proposed"))),
        parent_id=raw.get("parentId"),
        evidence_refs=list(raw.get("evidenceRefs") or []),
        created_at=str(raw.get("createdAt") or ""),
        applied_version=raw.get("appliedVersion"),
        refusal=str(raw.get("refusal") or ""),
        expires_at=raw.get("expiresAt"),
        watch_keys=list(raw.get("watchKeys") or []),
        decision=dict(raw.get("decision") or {}),
        recovered=bool(raw.get("recovered", False)),
    )
    candidate.validate()
    return candidate
