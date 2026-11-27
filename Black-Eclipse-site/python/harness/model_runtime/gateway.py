"""Model gateway facade (W1).

The gateway is deliberately thin: `ModelRuntime.invoke_traced` owns the one
candidate/retry/fallback loop. This module adds the run-facing request shape,
candidate planning/trace, and the streaming-first-token policy.

The graph never talks to a provider SDK. `step.invoke_model_step` calls
`ModelGateway.stream`, which calls the runtime.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Callable, Optional

from . import errors
from .credential_pool import Credential, CredentialPool, get_credential_pool
from .fallback import fallback_quality_allowed
from .runtime import (
    ModelRuntime,
    Resolution,
    fb_settings,
    get_runtime,
)
from .types import Attempt, Candidate, GatewayResult, ModelInfo

# Same-candidate retry classes. Kept local so the gateway does not import the
# retry policy (which imports the model runtime package).
_RETRYABLE = frozenset({
    errors.ErrorClass.RATE_LIMIT,
    errors.ErrorClass.TIMEOUT,
    errors.ErrorClass.NETWORK_ERROR,
    errors.ErrorClass.SERVER_ERROR,
    errors.ErrorClass.UNKNOWN_ERROR,
})


@dataclass(frozen=True)
class GatewayRequest:
    model: Any
    messages: list[Any]
    timeout_s: float
    finalizing: bool
    emitter: Any
    resolution: Resolution
    run_id: str
    step: int
    task: str = "agent"
    first_token_timeout_s: Optional[float] = None
    fallback_build: Optional[Callable[[Any], Any]] = None
    max_attempts: Optional[int] = None
    max_run_tokens: Optional[int] = None
    credential_pool: Any = None
    usage_anchor_store: Any = None
    anchor_id: str = ""
    history_chars: int = 0


class _CountingEmitter:
    """Count text deltas so a retry never duplicates streamed text."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.deltas = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def emit(self, event_type: str, data: Optional[dict[str, Any]] = None) -> Any:
        return await self._inner.emit(event_type, data)

    def emit_sync(self, event_type: str, data: Optional[dict[str, Any]] = None) -> None:
        self._inner.emit_sync(event_type, data)

    def emit_transient(self, event_type: str, data: Optional[dict[str, Any]] = None) -> None:
        if event_type == "model.delta" and isinstance(data, dict) and str(data.get("text") or ""):
            self.deltas += 1
        self._inner.emit_transient(event_type, data)


def _estimate_request_tokens(messages: list[Any], tools_text: str = "") -> int:
    from ..eclipse_memory.profile import estimate_tokens  # noqa: PLC0415

    total = sum(estimate_tokens(str(getattr(m, "content", "") or "")) for m in messages)
    if tools_text:
        total += estimate_tokens(tools_text)
    return total + 1024  # conservative output reserve


def plan_candidates(
    runtime: ModelRuntime,
    resolution: Resolution,
    *,
    primary_model: Any,
    messages: list[Any],
    credential_pool: Optional[CredentialPool] = None,
    tools_text: str = "",
    fallback_depth: int = 3,
) -> tuple[list[Candidate], list[dict[str, Any]]]:
    """Return reachable candidates and explicit exclusion reasons.

    This plan is emitted for observability. The runtime independently enforces
    the same circuit/rate/credential rules, so a plan is never the only gate.
    """
    settings = resolution.settings
    run_provider = getattr(settings, "provider", None)
    run_model = getattr(settings, "model", None)
    primary_info = resolution.model
    estimate = _estimate_request_tokens(messages, tools_text)
    excluded: list[dict[str, Any]] = []
    candidates: list[Candidate] = []

    def add(provider: str, model: str, *, reason: str, info: Optional[ModelInfo],
            built: Any, credential: Any, score: Optional[float] = None) -> None:
        credential_id = getattr(credential, "id", None)
        key = f"{provider}/{model}"
        if credential_id:
            key = f"{key}#{credential_id}"
        if not runtime.circuit.allow(key) or not runtime.circuit.allow(provider):
            excluded.append({"model": key, "reason": "circuit_open"})
            return
        if runtime.rate_limits.is_limited(key):
            excluded.append({"model": key, "reason": "rate_limited"})
            return
        window = getattr(info, "context_window", None) if info is not None else None
        if window is not None and estimate > max(1, int(window) - 1024):
            excluded.append({"model": key, "reason": "context_too_small", "window": window})
            return
        candidates.append(Candidate(
            provider=provider, model=model, index=len(candidates),
            reason=reason, credential_id=getattr(credential, "id", None),
            context_window=window, score=score, model_info=info,
            built=built, credential=credential,
        ))

    credential = credential_pool.acquire(run_provider) if credential_pool and run_provider else None
    add(
        str(run_provider or "unknown"), str(run_model or "unknown"),
        reason="primary", info=primary_info, built=primary_model,
        credential=credential,
    )
    # Additional keys for the same provider become reachable candidates. The
    # runtime rebuilds them with the candidate's credential.
    if credential_pool is not None and run_provider:
        for extra in credential_pool.credentials(run_provider):
            if credential is not None and extra.id == credential.id:
                continue
            add(
                str(run_provider), str(run_model or "unknown"),
                reason="primary_key", info=primary_info, built=None,
                credential=extra,
            )

    for fb in (resolution.decision.fallbacks if resolution.decision else []):
        info = getattr(fb, "model", None)
        if info is None:
            continue
        provider_id = getattr(info, "provider_id", "")
        model_id = getattr(info, "id", "")
        provider = runtime.providers.get_provider(provider_id)
        cred = credential_pool.acquire(provider_id) if credential_pool is not None else None
        same_provider = provider_id == run_provider
        keyless = provider is not None and provider.keyless
        if not (same_provider or keyless):
            if not runtime.fallback_enabled:
                excluded.append({"model": f"{provider_id}/{model_id}", "reason": "cross_provider_opt_in_required"})
                continue
            if cred is None:
                excluded.append({"model": f"{provider_id}/{model_id}", "reason": "no_credential"})
                continue
            if not fallback_quality_allowed(resolution.model, info):
                excluded.append({"model": f"{provider_id}/{model_id}", "reason": "quality_too_low"})
                continue
        # Required capability must be explicitly supported by the fallback.
        required = list(getattr(resolution.decision.requirements, "required", []) or [])
        caps = runtime.capabilities.apply(info)
        missing = [key for key in required if not caps.is_supported(key)]
        if missing:
            excluded.append({
                "model": f"{provider_id}/{model_id}",
                "reason": "capability_missing", "missing": missing,
            })
            continue
        add(
            provider_id, model_id, reason="fallback", info=info,
            built=None, credential=cred, score=getattr(fb, "score", None),
        )
        if len(candidates) >= 1 + max(1, int(fallback_depth)):
            break
    return candidates, excluded


class ModelGateway:
    """The graph-facing gateway. One runtime call, one trace."""

    def __init__(self, runtime: Optional[ModelRuntime] = None) -> None:
        self.runtime = runtime or get_runtime()

    async def plan(self, req: GatewayRequest):
        """Build and emit the candidate plan; no model call."""
        pool = req.credential_pool
        if pool is None:
            pool = get_credential_pool(req.resolution.settings)
        candidates, excluded = plan_candidates(
            self.runtime, req.resolution,
            primary_model=req.model, messages=req.messages,
            credential_pool=pool, tools_text="",
        )
        if req.emitter is not None:
            try:
                await req.emitter.emit("model.candidate_plan", {
                    "step": req.step,
                    "candidates": [c.to_dict() for c in candidates],
                    "excluded": excluded,
                    "fallbackDepth": len(candidates) - 1,
                })
            except Exception:  # noqa: BLE001 - observability is fail-open
                pass
        return candidates, excluded

    async def stream(self, req: GatewayRequest) -> GatewayResult:
        pool = req.credential_pool
        if pool is None:
            pool = get_credential_pool(req.resolution.settings)
        await self.plan(req)
        counted = _CountingEmitter(req.emitter)

        async def _invoke(model: Any) -> Any:
            from ..model_stream import ainvoke_streaming  # noqa: PLC0415

            return await ainvoke_streaming(
                model, req.messages, req.timeout_s, counted, req.finalizing,
                first_token_timeout_s=req.first_token_timeout_s,
            )

        def _on_attempt(attempt: Attempt) -> None:
            if req.emitter is None:
                return
            data = attempt.to_dict()
            data["step"] = req.step
            try:
                req.emitter.emit_sync(
                    "model.attempt.completed" if attempt.outcome == "completed"
                    else "model.attempt.failed",
                    data,
                )
                if attempt.will_retry:
                    req.emitter.emit_sync("model.retry", {
                        "step": req.step,
                        "attempt": attempt.attempt,
                        "maxAttempts": req.max_attempts,
                        "errorClass": attempt.error_class,
                        "statusCode": attempt.status_code,
                    })
            except Exception:  # noqa: BLE001 - telemetry is fail-open
                pass

        def _retryable(exc: BaseException) -> bool:
            if counted.deltas > 0:
                return False
            try:
                return errors.classify_exception(
                    exc,
                    provider_id=getattr(req.resolution.settings, "provider", None),
                    model_id=getattr(req.resolution.settings, "model", None),
                ).error_class in _RETRYABLE
            except Exception:  # noqa: BLE001
                return False

        trace: list[Attempt] = []
        from .run_events import bind_run_emitter  # noqa: PLC0415 - keep the module light

        with bind_run_emitter(req.emitter):
            result = await self.runtime.invoke(
                invoke_fn=_invoke,
                primary_model=req.model,
                resolution=req.resolution,
                fallback_build=req.fallback_build,
                credential_pool=pool,
                max_attempts=req.max_attempts,
                task=req.task,
                run_id=req.run_id,
                retryable=_retryable,
                trace=trace,
                on_attempt=_on_attempt,
                max_run_tokens=req.max_run_tokens,
                return_result=True,
                raise_original=True,
                max_wait_s=min(120.0, float(req.timeout_s or 60.0)),
                request_tokens=_estimate_request_tokens(req.messages),
            )
        if not isinstance(result, GatewayResult):
            from .errors import ErrorClass, ModelError  # noqa: PLC0415
            raise ModelError(
                error_class=ErrorClass.UNKNOWN_ERROR,
                message="model gateway returned no result",
                detail=str([a.to_dict() for a in trace]),
            )
        if req.usage_anchor_store is not None and req.anchor_id:
            last = result.attempts[-1] if result.attempts else None
            if last is not None and last.outcome == "completed":
                try:
                    context_length = (
                        getattr(req.resolution.model, "context_window", None)
                        if req.resolution.model is not None else None
                    )
                    req.usage_anchor_store.record(
                        req.anchor_id,
                        model_key=f"{last.provider}/{last.model}",
                        prompt_tokens=last.prompt_tokens,
                        completion_tokens=last.completion_tokens,
                        messages=req.messages,
                        history_chars=req.history_chars,
                        context_length=context_length,
                    )
                except Exception:  # noqa: BLE001 - the anchor must not break a run
                    pass

        if req.emitter is not None and result.fallback_used:
            try:
                await req.emitter.emit("model.fallback", {
                    "step": req.step,
                    "attempts": len(result.attempts),
                    "fallbackUsed": True,
                })
            except Exception:  # noqa: BLE001
                pass
        return result


__all__ = [
    "GatewayRequest",
    "ModelGateway",
    "plan_candidates",
]
