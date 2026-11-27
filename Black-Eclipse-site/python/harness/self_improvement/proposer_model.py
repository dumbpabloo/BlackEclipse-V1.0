"""Model-assisted candidate proposals for RSL, behind a flag.

The model never owns a decision. It returns strict JSON, which is parsed and
validated; every op then goes through the SAME `propose_ops` guard and frozen
target denylist the deterministic proposer uses. A missing provider, malformed
JSON, an invented path or a frozen target is a refusal, never a silent fallback.

The module adds no second runtime or provider path: it uses `model_runtime` and
the existing `build_chat_model` factory. A background proposer has no request to
carry `ModelSettings`, so it resolves the runtime-wide job/RSL settings from the
environment and fails honestly when none is configured.
"""

from __future__ import annotations

import inspect
import json
import os
from typing import Any, Callable, Mapping, Optional

from ..models import build_chat_model
from ..model_runtime import get_runtime
from ..model_runtime.structured import StructuredOutputManager
from ..schemas import ModelSettings
from ..evaluation.verdict_guard import assert_text_has_no_verdict_leaks
from .proposer import CandidateProposal, ProposalRefused, propose_many

MAX_VARIANTS = 4
MAX_OPS = 10
MAX_FILE_EXCERPT = 8_000
MAX_EVIDENCE = 12

MODEL_PROPOSER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["variants"],
    "properties": {
        "variants": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_VARIANTS,
            "items": {
                "type": "object",
                "required": ["kind", "targetPath", "ops", "rationale"],
                "properties": {
                    "kind": {"type": "string", "enum": ["invariant", "domain"]},
                    "targetPath": {"type": "string", "minLength": 1, "maxLength": 512},
                    "rationale": {"type": "string", "maxLength": 1000},
                    "ops": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": MAX_OPS,
                        "items": {
                            "type": "object",
                            "required": ["op", "path", "reason"],
                            "properties": {
                                "op": {
                                    "type": "string",
                                    "enum": ["create", "replace", "delete", "move", "overwrite"],
                                },
                                "path": {"type": "string", "minLength": 1, "maxLength": 512},
                                "reason": {"type": "string", "maxLength": 500},
                                "content": {"type": "string"},
                                "old": {"type": "string"},
                                "new": {"type": "string"},
                                "fromPath": {"type": "string"},
                            },
                        },
                    },
                },
            },
        }
    },
}

InvokeFn = Callable[[str], Any]


def model_settings_from_env(environ: Optional[Mapping[str, str]] = None) -> ModelSettings:
    """The runtime-wide proposer model, with RSL-specific overrides.

    This mirrors the existing background-job convention (`HARNESS_JOB_*`) so a
    zero-config install still points at the local Ollama endpoint. It never
    invents a key and never logs one.
    """
    env = os.environ if environ is None else environ
    return ModelSettings(
        provider=str(
            env.get("HARNESS_RSL_PROVIDER")
            or env.get("HARNESS_JOB_PROVIDER")
            or "ollama"
        ),
        model=str(
            env.get("HARNESS_RSL_MODEL")
            or env.get("HARNESS_JOB_MODEL")
            or "llama3.1"
        ),
        base_url=(
            env.get("HARNESS_RSL_BASE_URL")
            or env.get("HARNESS_JOB_BASE_URL")
            or None
        ),
        api_key=(
            env.get("HARNESS_RSL_API_KEY")
            or env.get("HARNESS_JOB_API_KEY")
            or None
        ),
    )


def _memory_text(memory: Any) -> str:
    """The rendered SIM block, or "".

    A `str` is accepted as-is (a caller may already have rendered it); anything
    with `render()` is rendered at its own budget. A render failure degrades to
    no memory: the proposal must still be possible without it. The block passes
    the same verdict tripwire as a lesson, so a grader verdict captured in an
    attempt rationale can never enter the proposal prompt.
    """
    if not memory:
        return ""
    if isinstance(memory, str):
        text = memory.strip()
    else:
        render = getattr(memory, "render", None)
        if not callable(render):
            return ""
        try:
            text = str(render() or "").strip()
        except Exception:  # noqa: BLE001 - memory is advice, never a blocker
            return ""
    if not text:
        return ""
    try:
        assert_text_has_no_verdict_leaks(text, label="self-improvement memory")
    except Exception:  # noqa: BLE001 - a leak is dropped, never widened around
        return ""
    return text


def _prompt(
    candidate: Mapping[str, Any],
    *,
    k: int,
    context: Optional[Mapping[str, Any]] = None,
    memory: Any = None,
) -> str:
    payload = {
        "problem": str(candidate.get("problemStatement") or "")[:1500],
        "category": str(candidate.get("sourceCategory") or "")[:120],
        "targetPath": candidate.get("targetPath"),
        "existingOps": candidate.get("proposedOps") or [],
        "fileExcerpt": str((context or {}).get("fileExcerpt") or "")[:MAX_FILE_EXCERPT],
        "evidence": list((context or {}).get("evidence") or [])[:MAX_EVIDENCE],
    }
    experience = _memory_text(memory)
    memory_section = (
        f"\n\n{experience}\n\n"
        "Before proposing, check whether an existing problem already explains "
        "this failure; cite its id in `rationale` (for example \"P3: ...\") or "
        "name a new root cause. Do not repeat an attempt that the record shows "
        "already failed in this setting without new evidence.\n"
        if experience
        else ""
    )
    return (
        "You propose a bounded, reviewable change to the Eclipse harness.\n"
        "Return STRICT JSON only, no prose and no code fences, with this shape:\n"
        '{"variants":[{"kind":"invariant"|"domain","targetPath":"relative/path",'
        '"ops":[{"op":"create"|"replace"|"delete"|"move"|"overwrite",'
        '"path":"relative/path","reason":"...","content?":"...","old?":"...",'
        '"new?":"...","fromPath?":"..."}],"rationale":"..."}]}\n'
        f"Produce between 1 and {max(1, min(int(k), MAX_VARIANTS))} variants. "
        "Every path must be relative and inside the workspace. Never target "
        "tests, the evaluator, policy, approvals, security, datasets, .env or "
        "jdds.db. Do not claim a test result, a score or an approval. If the "
        "evidence is insufficient, return a single `domain` variant with the "
        "smallest defensible change."
        f"{memory_section}\n\n"
        f"Observation:\n{json.dumps(payload, indent=2, sort_keys=True, default=str)}"
    )


def _content_text(result: Any) -> str:
    text = getattr(result, "content", None)
    if text is None:
        text = result
    if isinstance(text, list):
        return " ".join(str(part) for part in text)
    return str(text or "")


async def _invoke_with_model(prompt: str, settings: ModelSettings) -> str:
    try:
        runtime = get_runtime()
        resolution = runtime.resolve_for_run(settings, task="rsl_proposer")
        model = runtime.build(
            resolution.settings, builder=build_chat_model, adapter=resolution.adapter,
        )
        invoke = getattr(model, "ainvoke", None)
        if callable(invoke):
            result = await invoke(prompt)
        else:
            result = model.invoke(prompt)
            if inspect.isawaitable(result):
                result = await result
        return _content_text(result)
    except Exception as exc:  # noqa: BLE001 - a provider failure is a refusal
        raise ProposalRefused(
            "MODEL_PROPOSER_UNAVAILABLE",
            f"{type(exc).__name__}: {exc}"[:500],
        ) from exc


async def propose_variants(
    candidate: Mapping[str, Any],
    *,
    k: int = 2,
    context: Optional[Mapping[str, Any]] = None,
    actor: Any = None,
    settings: Optional[ModelSettings] = None,
    invoke: Optional[InvokeFn] = None,
    memory: Any = None,
) -> list[CandidateProposal]:
    """Ask the model for typed variants, then validate them like any proposal.

    `invoke` is the test seam: it receives the prompt and returns text (or an
    awaitable). In production the runtime-wide model is used. Any malformed
    variant refuses the whole set: a partial, silently dropped proposal would be
    a fabricated comparison. `memory` is an optional `ImprovementMemory` (or its
    rendered text): prior attempts with their rationale and outcomes.
    """
    want = max(1, min(int(k), MAX_VARIANTS))
    prompt = _prompt(candidate, k=want, context=context, memory=memory)
    try:
        if invoke is not None:
            raw = invoke(prompt)
            if inspect.isawaitable(raw):
                raw = await raw
            text = _content_text(raw)
        else:
            text = await _invoke_with_model(
                prompt, settings or model_settings_from_env(),
            )
    except ProposalRefused:
        raise
    except Exception as exc:  # noqa: BLE001 - provider failure is a refusal
        raise ProposalRefused(
            "MODEL_PROPOSER_UNAVAILABLE",
            f"{type(exc).__name__}: {exc}"[:500],
        ) from exc

    parsed = StructuredOutputManager().parse_and_validate(
        text, MODEL_PROPOSER_SCHEMA,
    )
    if not parsed.ok:
        raise ProposalRefused(
            "MODEL_PROPOSER_INVALID",
            parsed.validation_error or "the model did not return the required JSON",
        )
    value = parsed.value if isinstance(parsed.value, Mapping) else {}
    variants = value.get("variants")
    if not isinstance(variants, list) or not variants:
        raise ProposalRefused(
            "MODEL_PROPOSER_INVALID", "the model returned no variants",
        )
    if len(variants) > want:
        variants = variants[:want]
    return propose_many(candidate, want, variants=variants, actor=actor)


__all__ = [
    "MAX_OPS",
    "MAX_VARIANTS",
    "MODEL_PROPOSER_SCHEMA",
    "model_settings_from_env",
    "propose_variants",
]
