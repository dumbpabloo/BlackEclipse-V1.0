"""ModelRuntime — the single interface the rest of Eclipse talks to (spec §11).

    Agent → ModelRuntime → ModelRouter → ProviderAdapter → Provider

The runtime owns provider-agnostic model selection, invocation with retry and
fallback, health/rate-limit accounting, usage/cost/latency recording, structured
output and decision tracing. It never lets the agent see a provider SDK.

Migration note: `build()` delegates to the existing `harness.models` factory
through an adapter, so the one construction path is preserved and existing
callers/tests keep working while gaining the runtime's routing and accounting.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import random
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Deque, Optional

from . import capability as _capability
from .catalog import ALIASES
from .errors import ErrorClass, ModelError, classify_exception
from .fallback import fallback_quality_allowed
from .health import CircuitBreaker, CircuitState, HealthManager, RateLimitManager
from .providers import adapter_for
from .providers.base import ProviderAdapter
from .registry import (
    CapabilityRegistry,
    ModelRegistry,
    PricingRegistry,
    ProviderRegistry,
    build_default_registries,
)
from .routing import ModelRouter, RoutingDecision, RoutingPolicy, TaskProfiles
from .structured import StructuredOutputManager
from .types import (
    Attempt,
    Candidate,
    ChatRequest,
    CostEstimate,
    GatewayResult,
    ModelInfo,
    ModelResponse,
    StreamEvent,
    ToolCall,
    ToolSchema,
    Usage,
)
from .usage import (
    BudgetManager,
    extract_usage,
    CostTracker,
    LatencyTracker,
    UsageRecord,
    UsageStore,
)

log = logging.getLogger("harness.model_runtime")

# ── Retry policy (spec §18) ─────────────────────────────────────────────────

# Retryable on the SAME model: transient faults only. A context overflow is NOT
# retried in place (the caller must shrink context or change model); auth/bad
# request are not retried at all.
_RETRYABLE = frozenset({
    ErrorClass.RATE_LIMIT,
    ErrorClass.TIMEOUT,
    ErrorClass.NETWORK_ERROR,
    ErrorClass.SERVER_ERROR,
    ErrorClass.UNKNOWN_ERROR,
})

# Errors that justify moving to a fallback candidate.
_FALLBACK_CLASSES = frozenset({
    ErrorClass.RATE_LIMIT,
    ErrorClass.TIMEOUT,
    ErrorClass.NETWORK_ERROR,
    ErrorClass.SERVER_ERROR,
    ErrorClass.MODEL_UNAVAILABLE,
    ErrorClass.PROVIDER_UNAVAILABLE,
    ErrorClass.CAPABILITY_ERROR,
    ErrorClass.CONTEXT_OVERFLOW,
    ErrorClass.UNKNOWN_ERROR,
})


@dataclass
class Resolution:
    settings: Any  # schemas.ModelSettings (not imported here to avoid a cycle)
    model: Optional[ModelInfo]
    decision: RoutingDecision
    adapter: ProviderAdapter
    constrained: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": getattr(self.settings, "provider", None),
            "model": getattr(self.settings, "model", None),
            "constrained": self.constrained,
            "decision": self.decision.to_dict(),
        }


def _backoff_seconds(attempt: int, base: float = 0.5, cap: float = 8.0) -> float:
    """Exponential backoff with full jitter (spec §18)."""
    window = min(cap, base * (2 ** max(0, attempt)))
    return random.uniform(0.0, window)


class ModelRuntime:
    """The provider-agnostic model runtime."""

    def __init__(
        self,
        *,
        providers: Optional[ProviderRegistry] = None,
        models: Optional[ModelRegistry] = None,
        capabilities: Optional[CapabilityRegistry] = None,
        pricing: Optional[PricingRegistry] = None,
        health: Optional[HealthManager] = None,
        circuit: Optional[CircuitBreaker] = None,
        rate_limits: Optional[RateLimitManager] = None,
        policy: Optional[RoutingPolicy] = None,
        task_profiles: Optional[TaskProfiles] = None,
        budgets: Optional[BudgetManager] = None,
        usage_store: Optional[UsageStore] = None,
        structured: Optional[StructuredOutputManager] = None,
        max_attempts: Optional[int] = None,
    ) -> None:
        if providers is None or models is None or capabilities is None or pricing is None:
            providers, models, capabilities, pricing = build_default_registries()
        self.providers = providers
        self.models = models
        self.capabilities = capabilities
        self.pricing = pricing
        self.health = health or HealthManager()
        self.circuit = circuit or CircuitBreaker()
        if rate_limits is None:
            # One conservative ceiling per provider/model key. Parallel chat
            # sessions queue here instead of all hitting the provider at once.
            # `HARNESS_MODEL_CONCURRENCY=0` restores the old unbounded mode.
            try:
                raw_concurrency = (os.environ.get("HARNESS_MODEL_CONCURRENCY") or "2").strip()
                concurrency = max(0, min(64, int(raw_concurrency or "2")))
            except (TypeError, ValueError):
                concurrency = 2
            rate_limits = RateLimitManager(
                default_concurrency=concurrency or None,
            )
        self.rate_limits = rate_limits
        self.policy = policy or RoutingPolicy()
        self.task_profiles = task_profiles or TaskProfiles()
        self.budgets = budgets or BudgetManager()
        self.usage = usage_store or UsageStore()
        self.structured = structured or StructuredOutputManager()
        self.costs = CostTracker(pricing)
        self.latency = LatencyTracker()
        if max_attempts is not None:
            self.max_attempts = int(max_attempts)
        else:
            # W1: one retry knob. HARNESS_MODEL_RETRIES is a legacy alias; the
            # step policy and the runtime now agree on HARNESS_MODEL_ATTEMPTS.
            raw_attempts = (os.environ.get("HARNESS_MODEL_ATTEMPTS") or "").strip()
            if not raw_attempts and (os.environ.get("HARNESS_MODEL_RETRIES") or "").strip():
                log.warning(
                    "HARNESS_MODEL_RETRIES is deprecated; use HARNESS_MODEL_ATTEMPTS"
                )
                raw_attempts = os.environ.get("HARNESS_MODEL_RETRIES", "").strip()
            try:
                # Preserve the historical direct-call default (one attempt);
                # the graph's gateway explicitly passes the operator policy.
                self.max_attempts = max(1, min(10, int(raw_attempts or "1")))
            except (TypeError, ValueError):
                self.max_attempts = 1
        # Cross-provider fallback is default OFF: a run holds one resolved
        # credential set from the vault, so silently routing to a paid provider
        # the operator never configured would violate spec §31/§45. When enabled,
        # only same-provider or keyless-local fallbacks can actually build.
        self.fallback_enabled = os.environ.get("HARNESS_MODEL_FALLBACK", "").lower() in (
            "1", "true", "yes", "on",
        )
        self._router = ModelRouter(
            models=self.models,
            providers=self.providers,
            capabilities=self.capabilities,
            pricing=self.pricing,
            health=self.health,
            circuit=self.circuit,
            rate_limits=self.rate_limits,
            policy=self.policy,
            task_profiles=self.task_profiles,
            budget_manager=self.budgets,
        )
        self._router.set_latency_tracker(self.latency)
        self._decisions: Deque[RoutingDecision] = deque(maxlen=100)
        # Optional observer for observability events (spec §53). The service
        # installs a callback that forwards to the harness EventBus; the runtime
        # stays decoupled from the harness event model.
        self._observer: Optional[Callable[[str, dict[str, Any]], None]] = None

    def set_observer(self, observer: Optional[Callable[[str, dict[str, Any]], None]]) -> None:
        self._observer = observer

    def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        from .run_events import emit_model_event  # noqa: PLC0415 - avoid an import cycle

        emit_model_event(self._observer, event_type, data)

    # ── registries / discovery (spec §9–10) ─────────────────────────────────
    def register_provider(self, provider: Any) -> None:
        self.providers.register(provider)

    def register_model(self, model: ModelInfo) -> None:
        self.models.register_model(model)

    async def discover_served(self, settings: Any, *, provider_id: Optional[str] = None) -> list[str]:
        """The RAW model ids a live provider actually serves (best-effort).

        This is the provider's own list, BEFORE it is merged into the registry.
        It is the authoritative "what is installed/served" signal: the static
        catalog's declared models (Ollama's `llama3.1`, LM Studio's
        `local-model`) may not be installed at all, so the registry merge cannot
        distinguish a served declared model from an absent placeholder. Callers
        that need to pick a default that will not 404 use this.
        """
        provider_id = provider_id or getattr(settings, "provider", None)
        if not provider_id:
            return []
        provider = self.providers.get_provider(provider_id)
        if provider is None:
            return []
        adapter = adapter_for(kind=provider.kind, provider_id=provider_id)
        return await adapter.discover_models(settings)

    async def discover(self, settings: Any, *, provider_id: Optional[str] = None) -> list[ModelInfo]:
        """Discover a provider's models and register them (best-effort)."""
        provider_id = provider_id or getattr(settings, "provider", None)
        if not provider_id:
            return []
        model_ids = await self.discover_served(settings, provider_id=provider_id)
        return self.models.discover_models(provider_id, model_ids)

    async def health_check_provider(self, settings: Any) -> dict[str, Any]:
        provider_id = getattr(settings, "provider", None)
        provider = self.providers.get_provider(provider_id) if provider_id else None
        if provider is None:
            return {"ok": None, "detail": "unknown provider"}
        adapter = adapter_for(kind=provider.kind, provider_id=provider_id)
        return await adapter.health_check(settings)

    # ── routing (spec §11–13) ───────────────────────────────────────────────
    def route(
        self,
        *,
        model: Optional[str] = None,
        task: Optional[str] = None,
        requirements: Optional[dict[str, Any]] = None,
        policy_overrides: Optional[dict[str, Any]] = None,
        messages: Optional[list[Any]] = None,
    ) -> RoutingDecision:
        request = ChatRequest(
            messages=messages or [],
            model=model,
            task=task,
            requirements=requirements or {},
            policy_overrides=policy_overrides or {},
        )
        decision = self._router.route(request)
        self._decisions.append(decision)
        self._emit("model.routing.completed", {
            "requestId": decision.request_id,
            "task": decision.task,
            "alias": decision.alias,
            "selected": decision.selected.qualified_id if decision.selected else None,
            "fallbacks": [c.qualified_id for c in decision.fallbacks],
            "reason": decision.reason,
            "candidates": [
                {"model": c.qualified_id, "score": c.score, "excluded": c.excluded_reason}
                for c in decision.candidates
            ],
        })
        return decision

    def _explicit_ref(self, settings: Any) -> Optional[str]:
        """The router-facing explicit model reference for a run.

        The settings front door stores a BARE model id (e.g. `deepseek-chat`, or
        an operator-typed id the catalog has never heard of) alongside a provider
        id. The router resolves a bare id only when it is GLOBALLY unambiguous, so
        an uncatalogued custom/local model looked "unknown" and the router fell
        through to its own best pick — silently substituting a DIFFERENT model
        (the "my model doesn't work" bug). Qualifying the bare id with the RUN's
        provider lets the router recognise ANY model id the operator configured
        and honour it as the explicit choice. An already-qualified id or a known
        alias is passed through untouched.
        """
        explicit = getattr(settings, "model", None)
        provider_id = getattr(settings, "provider", None)
        if not explicit or not provider_id:
            return explicit
        if self.models.alias_intent(explicit) or self.models.get_model(explicit):
            return explicit
        head = explicit.split("/", 1)[0]
        if self.providers.get_provider(head) is not None:
            return explicit
        return f"{provider_id}/{explicit}"

    def resolve_for_run(
        self,
        settings: Any,
        *,
        task: Optional[str] = None,
        requirements: Optional[dict[str, Any]] = None,
        policy_overrides: Optional[dict[str, Any]] = None,
    ) -> Resolution:
        """Resolve the run's settings through the router.

        A concrete provider/model is respected as an explicit choice; the router
        only changes it when it can do so with the run's credentials (same
        provider) or a keyless local provider. It never silently routes to a
        provider the run has no key for (spec §31, §45).
        """
        decision = self.route(
            model=self._explicit_ref(settings),
            task=task,
            requirements=requirements,
            policy_overrides=policy_overrides,
        )
        resolved, constrained = self._constrain(settings, decision)
        model = self.models.get_model(f"{resolved.provider}/{resolved.model}")
        # A synthesized explicit model (an operator-typed id the catalog has never
        # seen) is not in the registry, but the router already built a `ModelInfo`
        # for it. Adopt that one so capabilities/health/usage are accounted for
        # instead of being silently unknown.
        if (
            model is None
            and decision.selected is not None
            and decision.selected.model.provider_id == resolved.provider
            and decision.selected.model.id == resolved.model
        ):
            model = decision.selected.model
        adapter = adapter_for(kind=self._kind_of(resolved.provider), provider_id=resolved.provider)
        return Resolution(settings=resolved, model=model, decision=decision, adapter=adapter, constrained=constrained)

    def _kind_of(self, provider_id: str) -> str:
        provider = self.providers.get_provider(provider_id)
        return provider.kind if provider else "openai_compatible"

    def _constrain(self, settings: Any, decision: RoutingDecision) -> tuple[Any, bool]:
        selected = decision.selected
        if selected is None:
            return settings, False
        provider_id = getattr(settings, "provider", None)
        if selected.model.provider_id == provider_id:
            return settings.model_copy(update={"model": selected.model.id}), False
        provider = self.providers.get_provider(selected.model.provider_id)
        if provider is not None and provider.keyless:
            return settings.model_copy(update={
                "provider": provider.id,
                "model": selected.model.id,
                "base_url": provider.default_base_url,
                "api_key": None,
            }), True
        # No credentials for the routed provider: keep the explicit choice.
        return settings, True

    # ── construction (spec §7) ──────────────────────────────────────────────
    def build(
        self,
        settings: Any,
        *,
        builder: Optional[Callable[[Any], Any]] = None,
        adapter: Optional[ProviderAdapter] = None,
    ) -> Any:
        """Build the LangChain model for `settings` via the provider adapter.

        `builder` lets a caller inject the existing module-level factory (used
        by the graph so its test seam is preserved) — the runtime still owns the
        adapter decision.
        """
        provider_id = getattr(settings, "provider", None)
        chosen = adapter or adapter_for(kind=self._kind_of(provider_id or ""), provider_id=provider_id)
        if builder is not None:
            return builder(settings)
        return chosen.build(settings)

    def prepare_model(self, model: Any, resolution: Resolution) -> Any:
        """Apply adapter-level request translation (e.g. reasoning).

        Reasoning effort is shaped ONLY when the model genuinely exposes a
        provider-native reasoning control: a first-party API (Anthropic
        `thinking`, Gemini `thinking_budget`) or a native OpenAI reasoning model
        (`reasoning_effort`). An OpenAI-COMPATIBLE endpoint (DeepSeek, Groq, a
        custom router, a local server) does NOT accept `reasoning_effort`, and
        injecting it is a hard provider 400 — the exact "my model stopped working"
        failure. The runtime never force-feeds a knob a model did not declare.
        """
        # An explicit effort the operator chose for the run (carried as the
        # `reasoning` routing requirement) OUTRANKS the task profile's default —
        # the setting is the whole point of exposing it. Absent, the profile's
        # own reasoning level applies.
        requirements = getattr(resolution.decision, "requirements", None)
        effort = getattr(requirements, "reasoning_effort", None)
        if not effort:
            profile = self.task_profiles.get(resolution.decision.task)
            effort = profile.get("reasoning")
        if not effort or effort in ("none", "low"):
            return model
        if not self._shapes_reasoning(resolution):
            return model
        from .types import ReasoningRequest  # local import

        # The UI's top stop (`very_high`, "Ultra") means the strongest thinking a
        # model offers, so normalize it to the runtime's `max` tier. Each adapter
        # then maps that to its provider's real ceiling: Anthropic/Google get a
        # deeper `thinking`/`thinking_budget`; native OpenAI has no tier above
        # `high`, so its adapter caps `max` at `high` (honest — the knob tops out
        # there). Any other out-of-vocabulary value falls back to `high` rather
        # than being sent raw.
        normalized = "max" if effort == "very_high" else effort
        if normalized not in ("low", "medium", "high", "max"):
            normalized = "high"
        request_reasoning = ReasoningRequest(enabled=True, effort=normalized)
        return resolution.adapter.apply_reasoning(model, request_reasoning)

    def _shapes_reasoning(self, resolution: Resolution) -> bool:
        """Whether this model has a provider-native reasoning control we may set.

        A bare `kind == "openai"` is not enough: every OpenAI-compatible endpoint
        shares that transport kind in some catalogs. Only the NATIVE OpenAI
        provider id is trusted with `reasoning_effort`; Anthropic and Google each
        have their own adapter and their own declared knob. In every case the
        model must also DECLARE reasoning support — an undeclared capability is
        never assumed.
        """
        model_info = resolution.model
        if model_info is None or not model_info.capabilities.is_supported("reasoning"):
            return False
        kind = getattr(resolution.adapter, "kind", "")
        if kind in ("anthropic", "google"):
            return True
        provider_id = getattr(resolution.settings, "provider", None)
        return kind == "openai" and provider_id == "openai"

    # ── invocation (retry + fallback + accounting) (spec §16–18) ────────────
    async def invoke(
        self,
        *,
        invoke_fn: Callable[[Any], Any],
        primary_model: Any,
        resolution: Resolution,
        max_attempts: Optional[int] = None,
        fallback_build: Optional[Callable[[Any], Any]] = None,
        task: Optional[str] = None,
        request_id: Optional[str] = None,
        raise_original: bool = True,
        # W1: trace + credential rotation + caller retry policy
        trace: Optional[list[Attempt]] = None,
        run_id: str = "",
        credential_pool: Any = None,
        retryable: Optional[Callable[[BaseException], bool]] = None,
        on_attempt: Optional[Callable[[Attempt], Any]] = None,
        max_run_tokens: Optional[int] = None,
        return_result: bool = False,
        # Multi-session admission: wait for quota instead of stampeding/failing.
        max_wait_s: Optional[float] = None,
        request_tokens: int = 0,
    ) -> Any:
        """Invoke the primary model, with same-provider fallback and retry.

        `invoke_fn(model)` performs the actual call (the graph passes its
        streaming helper). On failure the ORIGINAL exception is re-raised when
        `raise_original` is True, so existing caller error handling is intact;
        health/usage are recorded regardless.

        W1 additions are additive: `trace` receives one `Attempt` per physical
        request, `credential_pool` can rotate a provider key, and `retryable`
        lets the caller refuse a retry after a token has streamed. The legacy
        return value (the response) is unchanged unless `return_result=True`.
        """
        from ..retry_policy import retry_after_seconds as _retry_after_seconds  # noqa: PLC0415

        attempts = max_attempts if max_attempts is not None else self.max_attempts
        request_id = request_id or uuid.uuid4().hex
        decision = resolution.decision
        run_provider = getattr(resolution.settings, "provider", None)
        if max_wait_s is None:
            try:
                max_wait_s = float(os.environ.get("HARNESS_MODEL_COOLDOWN_WAIT_S") or "60")
            except (TypeError, ValueError):
                max_wait_s = 60.0
        max_wait_s = max(0.0, float(max_wait_s))
        # A cooling key is a shared fact. Wait for the earliest reset here so a
        # concurrent run rotates/waits instead of firing into a known 429.
        if credential_pool is not None and run_provider:
            wait_s = credential_pool.seconds_until_available(run_provider)
            if wait_s is not None and wait_s > 0:
                if wait_s <= max_wait_s:
                    self._emit("model.queued", {
                        "provider": run_provider,
                        "model": getattr(resolution.settings, "model", None),
                        "reason": "credential_cooldown",
                        "retryAfterMs": int(wait_s * 1000),
                    })
                    await asyncio.sleep(wait_s)
                else:
                    self._emit("model.rate_limited", {
                        "provider": run_provider,
                        "model": getattr(resolution.settings, "model", None),
                        "reason": "credential_cooldown",
                        "retryAfterMs": int(wait_s * 1000),
                    })
        candidates: list[tuple[Any, Optional[ModelInfo], Any]] = [
            (primary_model, resolution.model, None)
        ]
        # W1: the run's own key is one credential. If the pool has a different
        # first key for the primary provider, rebuild the primary through
        # `fallback_build` so the pool is authoritative.
        primary_cred = None
        if credential_pool is not None and run_provider:
            primary_cred = credential_pool.acquire(run_provider)
            run_key = getattr(resolution.settings, "api_key", None)
            if primary_cred is None and run_key:
                # Every key for this provider is cooling. Never fire the known
                # cooling run key: skip the primary and let fallbacks try.
                candidates = []
            elif primary_cred is not None and (not run_key or run_key != primary_cred.api_key):
                candidates[0] = (None, resolution.model, primary_cred)
            else:
                candidates[0] = (primary_model, resolution.model, primary_cred)
            for extra in credential_pool.credentials(run_provider):
                if extra.cooling:
                    continue
                if primary_cred is not None and extra.id == primary_cred.id:
                    continue
                candidates.append((None, resolution.model, extra))
        for fb in decision.fallbacks:
            cred = None
            if credential_pool is not None:
                cred = credential_pool.acquire(fb.model.provider_id)
            provider = self.providers.get_provider(fb.model.provider_id)
            same_provider = fb.model.provider_id == run_provider
            keyless = provider is not None and provider.keyless
            # Same-provider and keyless fallbacks are always considered: they
            # reuse the run's credentials and are the safe reliability win.
            # Cross-provider needs BOTH the operator opt-in and a credential,
            # and must pass the declared-quality guard.
            if same_provider or keyless:
                candidates.append((None, fb.model, cred))
            elif self.fallback_enabled and cred is not None:
                if fallback_quality_allowed(resolution.model, fb.model):
                    candidates.append((None, fb.model, cred))
                else:
                    log.warning(
                        "model fallback skipped: %s quality below %s (set "
                        "HARNESS_MODEL_FALLBACK_ALLOW_DEGRADED=1 to allow)",
                        fb.model.qualified_id,
                        getattr(resolution.model, "qualified_id", "primary"),
                    )
            # A remote provider with no credential is NOT reachable and is
            # skipped, never faked (spec A31/A45).

        if not candidates:
            raise ModelError(
                error_class=ErrorClass.RATE_LIMIT,
                message="all credentials for this provider are cooling",
                provider_id=run_provider,
                model_id=getattr(resolution.settings, "model", None),
            )

        last_error: Optional[ModelError] = None
        used_attempts = trace if trace is not None else []
        for index, (model_obj, model_info, credential) in enumerate(candidates):
            provider_id = model_info.provider_id if model_info else getattr(resolution.settings, "provider", None)
            model_id = model_info.id if model_info else getattr(resolution.settings, "model", None)
            credential_id = getattr(credential, "id", None)
            # Quota is per credential key: two keys each get a concurrency slot
            # and an independent 429 cooldown.
            key = f"{provider_id}/{model_id}"
            if credential_id:
                key = f"{key}#{credential_id}"
            if not self.circuit.allow(key) or not self.circuit.allow(provider_id or ""):
                last_error = ModelError(
                    error_class=ErrorClass.PROVIDER_UNAVAILABLE,
                    message="circuit open",
                    provider_id=provider_id,
                    model_id=model_id,
                )
                continue
            wait_s = self.rate_limits.seconds_until_available(key)
            if wait_s > 0:
                if wait_s > max_wait_s:
                    last_error = ModelError(
                        error_class=ErrorClass.RATE_LIMIT,
                        message="rate limited",
                        provider_id=provider_id,
                        model_id=model_id,
                    )
                    continue
                self._emit("model.queued", {
                    "provider": provider_id,
                    "model": model_id,
                    "reason": "rate_limit",
                    "retryAfterMs": int(wait_s * 1000),
                })
            elif self.rate_limits.is_limited(key):
                self._emit("model.queued", {
                    "provider": provider_id,
                    "model": model_id,
                    "reason": "concurrency",
                })

            candidate_model = model_obj
            if candidate_model is None:
                if fallback_build is None:
                    continue
                try:
                    settings = fb_settings(self, resolution, model_info)
                    if credential is not None and model_info is not None:
                        provider = self.providers.get_provider(model_info.provider_id)
                        settings = settings.model_copy(update={
                            "provider": model_info.provider_id,
                            "model": model_info.id,
                            "api_key": credential.api_key,
                            "base_url": (
                                credential.base_url
                                or getattr(provider, "default_base_url", None)
                                or getattr(settings, "base_url", None)
                            ),
                        })
                    candidate_model = fallback_build(settings)
                except Exception as exc:  # noqa: BLE001
                    last_error = classify_exception(exc, provider_id=provider_id, model_id=model_id)
                    continue

            try:
                await self.rate_limits.acquire_slot(key, timeout_s=max_wait_s)
            except asyncio.TimeoutError:
                last_error = ModelError(
                    error_class=ErrorClass.RATE_LIMIT,
                    message="rate limited: admission wait exceeded",
                    provider_id=provider_id,
                    model_id=model_id,
                )
                continue
            self.rate_limits.record_request(key, tokens=max(0, int(request_tokens or 0)))
            try:
                for attempt in range(max(1, attempts)):
                    if max_run_tokens is not None and sum(
                        a.total_tokens for a in used_attempts
                    ) >= int(max_run_tokens):
                        raise ModelError(
                            error_class=ErrorClass.UNKNOWN_ERROR,
                            message="MODEL_BUDGET_EXCEEDED",
                            provider_id=provider_id,
                            model_id=model_id,
                            retryable=False,
                        )
                    started = time.monotonic()
                    try:
                        response = await invoke_fn(candidate_model)
                    except BaseException as exc:  # noqa: BLE001 - classify + re-raise
                        # A non-Exception BaseException is a process/interpreter
                        # kill, not a provider failure. Let it propagate so the
                        # run is left recoverable (resume tests rely on this).
                        if not isinstance(exc, Exception):
                            raise
                        latency_ms = (time.monotonic() - started) * 1000.0
                        error = classify_exception(exc, provider_id=provider_id, model_id=model_id)
                        retryable_flag = (
                            bool(retryable(exc)) if retryable is not None
                            else error.error_class in _RETRYABLE
                        )
                        will_retry = retryable_flag and attempt < attempts - 1
                        will_fallback = (
                            error.error_class in _FALLBACK_CLASSES
                            and error.error_class is not ErrorClass.CONTEXT_OVERFLOW
                            and index < len(candidates) - 1
                        )
                        self._record_failure(
                            key, model_info, error, latency_ms, request_id, task,
                            retries=attempt, fallback=index > 0, run_id=run_id,
                        )
                        if credential_pool is not None:
                            credential_pool.record_failure(
                                credential,
                                status=error.status_code,
                                retry_after_s=_retry_after_seconds(exc),
                            )
                        last_error = error
                        if error.error_class is ErrorClass.RATE_LIMIT:
                            self.rate_limits.record_429(
                                key, retry_after_s=_retry_after_seconds(exc)
                            )
                        if trace is not None:
                            record = Attempt(
                                provider=str(provider_id or ""),
                                model=str(model_id or ""),
                                index=index,
                                attempt=attempt + 1,
                                outcome="failed",
                                error_class=error.error_class.value,
                                status_code=error.status_code,
                                latency_ms=int(round(latency_ms)),
                                retryable=bool(error.retryable),
                                will_retry=will_retry,
                                will_fallback=will_fallback,
                                credential_id=credential_id,
                            )
                            trace.append(record)
                            if on_attempt is not None:
                                notified = on_attempt(record)
                                if inspect.isawaitable(notified):
                                    await notified
                        self._emit("model.attempt.failed", {
                            "provider": provider_id, "model": model_id,
                            "index": index, "attempt": attempt + 1,
                            "fallback": index > 0,
                            "errorClass": error.error_class.value,
                            "statusCode": error.status_code,
                            "retryable": bool(error.retryable),
                            "willRetry": will_retry,
                            "willFallback": will_fallback,
                            "credentialId": credential_id,
                        })
                        if will_retry:
                            await asyncio.sleep(_backoff_seconds(attempt))
                            continue
                        break
                    latency_ms = (time.monotonic() - started) * 1000.0
                    self._record_success(
                        key, model_info, response, latency_ms, request_id, task,
                        retries=attempt, fallback=index > 0, run_id=run_id,
                    )
                    if credential_pool is not None:
                        credential_pool.record_success(credential)
                    usage = extract_usage(response)
                    cost = self.costs.estimate(model_info, usage) if model_info else None
                    if trace is not None:
                        record = Attempt(
                            provider=str(provider_id or ""),
                            model=str(model_id or ""),
                            index=index,
                            attempt=attempt + 1,
                            outcome="completed",
                            latency_ms=int(round(latency_ms)),
                            prompt_tokens=usage.input_tokens,
                            completion_tokens=usage.output_tokens,
                            total_tokens=usage.total_tokens or (usage.input_tokens + usage.output_tokens),
                            cost_usd=(cost.total_cost if cost is not None else None),
                            priced=(cost is not None and cost.total_cost is not None),
                            credential_id=credential_id,
                        )
                        trace.append(record)
                        if on_attempt is not None:
                            notified = on_attempt(record)
                            if inspect.isawaitable(notified):
                                await notified
                    self._emit("model.attempt.completed", {
                        "provider": provider_id, "model": model_id,
                        "index": index, "attempt": attempt + 1,
                        "fallback": index > 0, "latencyMs": round(latency_ms, 2),
                        "tokens": usage.total_tokens or (usage.input_tokens + usage.output_tokens),
                        "costUsd": (cost.total_cost if cost is not None else None),
                        "priced": bool(cost is not None and cost.total_cost is not None),
                        "credentialId": credential_id,
                    })
                    if return_result:
                        totals = sum(a.total_tokens for a in (trace or []))
                        costed = [
                            a for a in (trace or [])
                            if a.total_tokens > 0 or a.cost_usd is not None
                        ]
                        priced_attempts = [a for a in costed if a.priced]
                        cost_total = (
                            sum(a.cost_usd or 0.0 for a in priced_attempts)
                            if costed and len(priced_attempts) == len(costed)
                            else None
                        )
                        return GatewayResult(
                            response=response,
                            attempts=tuple(trace or ()),
                            fallback_used=index > 0,
                            total_tokens=totals,
                            cost_usd=cost_total,
                            priced=cost_total is not None,
                        )
                    return response
            finally:
                self.rate_limits.release_slot(key)

            if last_error is not None and last_error.error_class is ErrorClass.CONTEXT_OVERFLOW:
                # Context overflow is not a provider outage: the graph's bounded
                # context recovery owns it. Never try a different model here.
                break
            if (
                last_error is not None
                and last_error.error_class in _FALLBACK_CLASSES
            ):
                continue
            if last_error is not None and not last_error.retryable:
                break

        if last_error is not None:
            if trace is not None:
                last_error.detail = str([a.to_dict() for a in trace])
            if raise_original and last_error.cause is not None:
                raise last_error.cause
            raise last_error
        raise ModelError(error_class=ErrorClass.PROVIDER_UNAVAILABLE, message="no model available")

    async def invoke_traced(self, **kwargs: Any) -> GatewayResult:
        """Compatibility alias: invoke with a trace and return GatewayResult."""
        kwargs["return_result"] = True
        kwargs.setdefault("trace", [])
        result = await self.invoke(**kwargs)
        if isinstance(result, GatewayResult):
            return result
        raise ModelError(
            error_class=ErrorClass.UNKNOWN_ERROR,
            message="model gateway returned no result",
        )

    async def generate(
        self,
        *,
        settings: Any,
        messages: list[Any],
        tools: Optional[list[ToolSchema]] = None,
        task: Optional[str] = None,
        requirements: Optional[dict[str, Any]] = None,
        policy_overrides: Optional[dict[str, Any]] = None,
        response_schema: Optional[dict[str, Any]] = None,
        timeout_s: float = 120.0,
        temperature: Optional[float] = None,
        builder: Optional[Callable[[Any], Any]] = None,
    ) -> ModelResponse:
        """Provider-neutral one-shot generation with routing + fallback."""
        resolution = self.resolve_for_run(
            settings, task=task, requirements=requirements, policy_overrides=policy_overrides
        )
        request_id = uuid.uuid4().hex
        lc_tools = _to_langchain_tools(tools or [])

        async def _invoke(model: Any) -> Any:
            bound = model.bind_tools(lc_tools) if lc_tools and hasattr(model, "bind_tools") else model
            return await asyncio.wait_for(bound.ainvoke(messages), timeout=timeout_s)

        response = await self.invoke(
            invoke_fn=_invoke,
            primary_model=self._build_bound(resolution, builder, lc_tools),
            resolution=resolution,
            fallback_build=(lambda s: self._build_bound_for(s, builder, lc_tools)),
            task=task,
            request_id=request_id,
            raise_original=False,
        )
        normalized = self._normalize_response(response, resolution)
        if response_schema is not None and normalized.text:
            structured = self.structured.parse_and_validate(normalized.text, response_schema)
            normalized.decision = {**(normalized.decision or {}), "structured": structured.to_dict()}
        return normalized

    def _build_bound(self, resolution: Resolution, builder: Optional[Callable[[Any], Any]], lc_tools: list[Any]) -> Any:
        model = self.build(resolution.settings, builder=builder, adapter=resolution.adapter)
        model = self.prepare_model(model, resolution)
        return model.bind_tools(lc_tools) if lc_tools and hasattr(model, "bind_tools") else model

    def _build_bound_for(self, settings: Any, builder: Optional[Callable[[Any], Any]], lc_tools: list[Any]) -> Any:
        model = self.build(settings, builder=builder)
        return model.bind_tools(lc_tools) if lc_tools and hasattr(model, "bind_tools") else model

    async def stream(
        self,
        *,
        settings: Any,
        messages: list[Any],
        tools: Optional[list[ToolSchema]] = None,
        task: Optional[str] = None,
        requirements: Optional[dict[str, Any]] = None,
        policy_overrides: Optional[dict[str, Any]] = None,
        timeout_s: float = 120.0,
        builder: Optional[Callable[[Any], Any]] = None,
    ) -> AsyncIterator[StreamEvent]:
        """Normalized streaming (spec §29). Providers that cannot stream are
        served by a single non-streaming call, wrapped as one `text_delta`."""
        resolution = self.resolve_for_run(
            settings, task=task, requirements=requirements, policy_overrides=policy_overrides
        )
        model = self._build_bound(resolution, builder, _to_langchain_tools(tools or []))
        if not hasattr(model, "astream"):
            response = await asyncio.wait_for(model.ainvoke(messages), timeout=timeout_s)
            async for event in _events_from_message(response):
                yield event
            yield StreamEvent(type="done")
            return
        async for event in _stream_normalized(model, messages, timeout_s):
            yield event

    async def stream_for_graph(
        self,
        *,
        model: Any,
        messages: list[Any],
        timeout_s: float,
        finalizing: bool,
        emitter: Any,
        resolution: Optional[Resolution] = None,
        request_id: Optional[str] = None,
    ) -> Any:
        """Invoke the already-built graph model, streaming + accounting.

        The graph owns its streaming helper semantics; this wraps the runtime's
        retry/fallback around it without changing the returned message type.
        """
        if resolution is None:
            resolution = Resolution(
                settings=None, model=None,
                decision=RoutingDecision(request_id="", task="default", requirements=None, selected=None),  # type: ignore[arg-type]
                adapter=adapter_for(kind="openai_compatible"),
            )

        async def _invoke(m: Any) -> Any:
            from ..model_stream import ainvoke_streaming  # local import: avoid cycle

            return await ainvoke_streaming(m, messages, timeout_s, emitter, finalizing)

        return await self.invoke(
            invoke_fn=_invoke,
            primary_model=model,
            resolution=resolution,
            task=resolution.decision.task if resolution.decision else None,
            request_id=request_id,
            raise_original=True,
        )

    # ── accounting ──────────────────────────────────────────────────────────
    def record_step(
        self,
        resolution: Resolution,
        *,
        response: Any = None,
        latency_ms: float = 0.0,
        ok: bool = True,
        error: Optional[ModelError] = None,
        request_id: Optional[str] = None,
        task: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> None:
        """Record one graph model step for health/usage/latency.

        The execution graph builds its model through the runtime but invokes it
        with its own streaming helper; this is the seam that still lets the
        runtime learn from real traffic. A failure is recorded but never alters
        which model the caller already chose.
        """
        model_info = resolution.model
        provider_id = model_info.provider_id if model_info else getattr(resolution.settings, "provider", None)
        model_id = model_info.id if model_info else getattr(resolution.settings, "model", None)
        key = f"{provider_id}/{model_id}"
        task = task or (resolution.decision.task if resolution.decision else None)
        request_id = request_id or uuid.uuid4().hex
        if ok:
            self._record_success(key, model_info, response, latency_ms, request_id, task, run_id=run_id)
            self._emit("model.request.completed", {
                "requestId": request_id, "runId": run_id, "provider": provider_id,
                "model": model_id, "task": task, "latencyMs": round(latency_ms, 2),
            })
        else:
            self._record_failure(
                key, model_info,
                error or ModelError(error_class=ErrorClass.UNKNOWN_ERROR, message="model step failed"),
                latency_ms, request_id, task, run_id=run_id,
            )
            self._emit("model.request.failed", {
                "requestId": request_id, "runId": run_id, "provider": provider_id,
                "model": model_id, "task": task,
                "errorClass": (error.error_class.value if error else ErrorClass.UNKNOWN_ERROR.value),
            })

    def _record_success(
        self, key: str, model_info: Optional[ModelInfo], response: Any,
        latency_ms: float, request_id: str, task: Optional[str],
        *, retries: int = 0, fallback: bool = False, run_id: Optional[str] = None,
    ) -> None:
        self.health.record_success(key, latency_ms=latency_ms)
        if model_info:
            self.health.record_success(model_info.provider_id, latency_ms=latency_ms)
            self.circuit.record_success(model_info.provider_id)
        self.circuit.record_success(key)
        self.latency.record(key, total_ms=latency_ms)
        usage = extract_usage(response)
        cost: Optional[CostEstimate] = None
        if model_info is not None:
            cost = self.costs.estimate(model_info, usage)
            self.costs.record(model_info, cost)
        self.usage.record(UsageRecord(
            request_id=request_id,
            provider_id=model_info.provider_id if model_info else key.split("/")[0],
            model_id=model_info.id if model_info else key.split("/")[-1],
            run_id=run_id,
            task=task,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_tokens=usage.cached_tokens,
            total_tokens=usage.total_tokens or (usage.input_tokens + usage.output_tokens),
            tokens_estimated=usage.estimated,
            latency_ms=latency_ms,
            success=True,
            retries=retries,
            fallback=fallback,
            cost=cost,
        ))

    def _record_failure(
        self, key: str, model_info: Optional[ModelInfo], error: ModelError,
        latency_ms: float, request_id: str, task: Optional[str],
        *, retries: int = 0, fallback: bool = False, run_id: Optional[str] = None,
    ) -> None:
        self.health.record_failure(key, error_class=error.error_class, latency_ms=latency_ms)
        self.circuit.record_failure(key, provider_fault=error.is_provider_fault)
        if model_info is not None:
            self.circuit.record_failure(model_info.provider_id, provider_fault=error.is_provider_fault)
        self.usage.record(UsageRecord(
            request_id=request_id,
            provider_id=model_info.provider_id if model_info else key.split("/")[0],
            model_id=model_info.id if model_info else key.split("/")[-1],
            run_id=run_id,
            task=task,
            latency_ms=latency_ms,
            success=False,
            error_class=error.error_class.value,
            retries=retries,
            fallback=fallback,
        ))

    def _normalize_response(self, response: Any, resolution: Resolution) -> ModelResponse:
        usage = extract_usage(response)
        model_info = resolution.model
        cost = self.costs.estimate(model_info, usage) if model_info else None
        tool_calls = []
        for call in (getattr(response, "tool_calls", None) or []):
            if isinstance(call, dict):
                tool_calls.append(ToolCall(id=str(call.get("id") or ""), name=str(call.get("name") or ""), arguments=call.get("args") or {}))
        return ModelResponse(
            text=_text(response),
            tool_calls=tool_calls,
            usage=usage,
            cost=cost,
            model_id=model_info.id if model_info else "",
            provider_id=model_info.provider_id if model_info else "",
            raw=response,
            decision=resolution.decision.to_dict(),
        )

    # ── observability (spec §33, §41–43) ────────────────────────────────────
    def install_persistence(self, store: Any) -> None:
        """Route usage records into the harness persistence store.

        The sink persists one bounded `ModelRequest` row plus a rolling
        `ModelHealth` upsert per request. It never persists prompts, tool output
        or credentials — only normalized counts, cost and the routing trace.
        A sink failure is swallowed by `UsageStore` so accounting can never
        break a run.
        """
        if store is None:
            return

        def _sink(record: UsageRecord) -> None:
            payload = {
                "requestId": record.request_id,
                "runId": record.run_id,
                "provider": record.provider_id,
                "model": record.model_id,
                "task": record.task,
                "fallback": record.fallback,
                "success": record.success,
                "errorClass": record.error_class,
                "retries": record.retries,
                "inputTokens": record.input_tokens,
                "outputTokens": record.output_tokens,
                "cachedTokens": record.cached_tokens,
                "totalTokens": record.total_tokens,
                "tokensEstimated": record.tokens_estimated,
                "latencyMs": record.latency_ms,
                "ttftMs": record.time_to_first_token_ms,
                "cost": record.cost.to_dict() if record.cost else None,
                "decision": None,
            }
            record_model = getattr(store, "record_model_request", None)
            if callable(record_model):
                record_model(payload)
            upsert = getattr(store, "upsert_model_health", None)
            if callable(upsert):
                upsert(payload)

        self.usage.set_sink(_sink)

    def recent_decisions(self, limit: int = 20) -> list[dict[str, Any]]:
        return [d.to_dict() for d in list(self._decisions)[-limit:]]

    def health_report(self) -> dict[str, Any]:
        return {
            "providers": [
                {
                    "id": p.id,
                    "label": p.label,
                    "enabled": p.enabled,
                    "circuit": self.circuit.state(p.id),
                    **self.health.snapshot(p.id),
                    "rateLimit": self.rate_limits.snapshot(p.id),
                }
                for p in self.providers.list_providers()
            ]
        }

    def usage_summary(self) -> dict[str, Any]:
        return {**self.usage.summary(), "cost": self.costs.total(), "budgets": self.budgets.snapshot()}

    # ── structured output (spec §28) ────────────────────────────────────────
    def parse_structured(self, text: str, schema: Optional[dict[str, Any]]) -> Any:
        return self.structured.parse_and_validate(text, schema)


# ── module helpers ──────────────────────────────────────────────────────────


def fb_settings(runtime: ModelRuntime, resolution: Resolution, model_info: Optional[ModelInfo]) -> Any:
    """Settings for a fallback candidate, reusing the run's credentials when the
    provider matches, otherwise a keyless provider's defaults."""
    settings = resolution.settings
    if settings is None or model_info is None:
        return settings
    if model_info.provider_id == getattr(settings, "provider", None):
        return settings.model_copy(update={"model": model_info.id})
    provider = runtime.providers.get_provider(model_info.provider_id)
    if provider is not None and provider.keyless:
        return settings.model_copy(update={
            "provider": provider.id,
            "model": model_info.id,
            "base_url": provider.default_base_url,
            "api_key": None,
        })
    return settings.model_copy(update={"model": model_info.id})


def _to_langchain_tools(tools: list[ToolSchema]) -> list[Any]:
    """Convert normalized tool schemas into LangChain StructuredTools.

    The graph already builds LangChain tools from its own registry; this path is
    for callers of the runtime that pass normalized schemas directly.
    """
    if not tools:
        return []
    try:
        from langchain_core.tools import StructuredTool  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return []

    lc_tools = []
    for tool in tools:
        async def _noop(**_kwargs: Any) -> str:  # pragma: no cover - schema carrier only
            return ""

        try:
            lc_tools.append(StructuredTool.from_function(
                func=lambda **_kw: "",  # noqa: E731
                coroutine=_noop,
                name=tool.name,
                description=tool.description,
                args_schema=tool.input_schema,
            ))
        except Exception:  # noqa: BLE001
            continue
    return lc_tools


def _text(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return str(content)


async def _events_from_message(response: Any) -> AsyncIterator[StreamEvent]:
    text = _text(response)
    if text:
        yield StreamEvent(type="text_delta", text=text)
    for call in (getattr(response, "tool_calls", None) or []):
        if isinstance(call, dict):
            yield StreamEvent(
                type="tool_call_end",
                tool_call=ToolCall(id=str(call.get("id") or ""), name=str(call.get("name") or ""), arguments=call.get("args") or {}),
            )
    usage = extract_usage(response)
    if usage.total_tokens:
        yield StreamEvent(type="usage", usage=usage)


async def _stream_normalized(model: Any, messages: list[Any], timeout_s: float) -> AsyncIterator[StreamEvent]:
    """Normalize a provider stream into the common event vocabulary (spec §29)."""
    from ..model_stream import merge_chunks  # local import: avoid cycle

    aggregated: Any = None
    try:
        async for chunk in model.astream(messages):
            aggregated = chunk if aggregated is None else merge_chunks(aggregated, chunk)
            text = _text(chunk) if getattr(chunk, "content", "") else ""
            if text:
                yield StreamEvent(type="text_delta", text=text)
        final = aggregated
        for call in (getattr(final, "tool_calls", None) or []):
            if isinstance(call, dict):
                yield StreamEvent(
                    type="tool_call_end",
                    tool_call=ToolCall(id=str(call.get("id") or ""), name=str(call.get("name") or ""), arguments=call.get("args") or {}),
                )
        usage = extract_usage(final)
        if usage.total_tokens:
            yield StreamEvent(type="usage", usage=usage)
    except Exception as exc:  # noqa: BLE001
        yield StreamEvent(type="error", error=classify_exception(exc).to_dict())
    finally:
        yield StreamEvent(type="done")


# ── process-wide default runtime ────────────────────────────────────────────

_DEFAULT: Optional[ModelRuntime] = None


def get_runtime() -> ModelRuntime:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = ModelRuntime()
    return _DEFAULT


def set_runtime(runtime: Optional[ModelRuntime]) -> None:
    global _DEFAULT
    _DEFAULT = runtime


__all__ = [
    "ALIASES",
    "ModelRuntime",
    "Resolution",
    "extract_usage",
    "get_runtime",
    "set_runtime",
]
