"""The Native Automation Engine (§20-§25, §41).

One durable engine task beside the TS refresh loops. It owns:

  * due-time scheduling (cron/interval/one-time) with timezone-aware next-run,
  * event routing (indexed by `AutomationEventSubscription`, never a full scan),
  * condition evaluation with persisted edge state,
  * a durable queue for concurrency-limited work,
  * lease-based locking so two workers never process the same execution,
  * retry with backoff + jitter + non-retryable classification,
  * restart recovery (expired leases reclaimed, interrupted executions reconciled,
    missed fires applied per misfire policy),
  * delivery through the existing notification/artifact/messaging surfaces.

A run is dispatched through the EXISTING runtime (`Dispatcher`); the engine never
runs an agent loop. Time is injected so tests advance a fake clock.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import json
import logging
import os
import random
import uuid
from typing import Any, Callable, Optional

from ..mcp.circuit import CLOSED, COOLDOWN_S, FAILURE_THRESHOLD, HALF_OPEN, OPEN
from ..persistence import now_iso
from .conditions import ConditionContext, decide_edge, evaluate, extract_variables
from .delivery import deliver_automation
from .dispatcher import Dispatcher, DispatchResult, plan_actions
from .facts import FactProvider, default_fact_provider
from .legacy import FILE_CHANGED_EVENT
from .model import (
    DEFAULT_RETRYABLE_ERROR_CLASSES,
    NON_RETRYABLE_ERROR_CLASSES,
    AutomationDefinition,
)
from .store import AutomationStore
from .timeutil import (
    next_cron_fire,
    next_interval_fire,
    next_one_time_fire,
    now_utc,
    parse_iso,
    to_utc_iso,
)

log = logging.getLogger("harness.automation.engine")

TICK_SECONDS = 30.0
DEFAULT_MAX_IN_FLIGHT = 4
LEASE_SLACK_S = 60.0
# A fire that came due within this many seconds of startup is treated as ON TIME
# (run it), not a missed window — the process restarted near the fire time, it
# did not miss an outage. Mirrors Quartz's 60s misfire threshold.
MISFIRE_GRACE_S = 60.0
# A stored run output is the answer a person reads back; bounded so a runaway
# answer cannot bloat the execution ledger.
_OUTPUT_MAX_CHARS = 20_000
# Terminal failure statuses that count toward the breaker.
_FAILURE_STATES = frozenset({"failed", "timed_out"})
# Trigger types whose launch advances the persisted schedule. A manual fire (or a
# recovery re-run) MUST NOT move `nextRunAt` — see `fire()`'s contract.
_SCHEDULING_TRIGGER_TYPES = frozenset(
    {"schedule", "cron", "interval", "one_time", "misfire", "catch_up"}
)
_DEDUP_RETENTION_DAYS = 30


def _default_clock() -> _dt.datetime:
    return now_utc()


class AutomationEngine:
    """Durable automation scheduler + trigger router over the ONE store."""

    def __init__(
        self,
        service: Any,
        store: AutomationStore,
        *,
        clock: Optional[Callable[[], _dt.datetime]] = None,
        dispatcher: Optional[Dispatcher] = None,
        fact_provider: Optional[FactProvider] = None,
        max_in_flight: int = DEFAULT_MAX_IN_FLIGHT,
        tick_seconds: float = TICK_SECONDS,
        worker_id: Optional[str] = None,
    ) -> None:
        self._service = service
        self._store = store
        self._clock = clock or _default_clock
        self._dispatcher = dispatcher or Dispatcher(service)
        self._facts: FactProvider = fact_provider or default_fact_provider(service)
        self._max_in_flight = max(1, int(max_in_flight))
        self._tick_seconds = tick_seconds
        self._worker_id = worker_id or f"worker_{uuid.uuid4().hex[:8]}"
        self._bus = getattr(service, "bus", None)
        self._task: Optional[asyncio.Task[Any]] = None
        # W4: heartbeat hooks ride THIS tick. A hook is a coroutine taking the
        # current UTC datetime; a failure is logged and never breaks scheduling.
        self._tick_hooks: list[Any] = []
        self._active: dict[str, asyncio.Task[Any]] = {}
        self._loop_ref: Optional[asyncio.AbstractEventLoop] = None
        self._unsubscribe: Optional[Callable[[], None]] = None
        self._watchers: dict[str, str] = {}
        self._last_tick_ms: float = 0.0

    # ── Lifecycle ────────────────────────────────────────────────────────────
    async def start(self) -> None:
        """Recover state, subscribe to events, and start the tick loop (idempotent)."""
        if self._task is not None and not self._task.done():
            return
        self._loop_ref = asyncio.get_running_loop()
        try:
            await self._recover()
        except Exception:  # noqa: BLE001 - recovery must never block boot
            log.warning("automation recovery failed", exc_info=True)
        self._subscribe_events()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._unsubscribe is not None:
            try:
                self._unsubscribe()
            except Exception:  # noqa: BLE001
                pass
            self._unsubscribe = None
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        for run in list(self._active.values()):
            run.cancel()
        self._active.clear()

    async def wait_idle(self) -> None:
        # `_active` is popped by a done callback, which runs on the next loop
        # turn. Gathering only already-done tasks returns without yielding, so
        # yield once or this becomes a busy loop that never exits.
        while self._active:
            await asyncio.gather(*list(self._active.values()), return_exceptions=True)
            await asyncio.sleep(0)

    def _now(self) -> _dt.datetime:
        moment = self._clock()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=_dt.timezone.utc)
        return moment

    async def _loop(self) -> None:
        while True:
            started = self._now()
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a tick failure must not kill the loop
                log.warning("automation tick failed", exc_info=True)
            self._last_tick_ms = (self._now() - started).total_seconds() * 1000.0
            await asyncio.sleep(self._tick_seconds)

    # ── Event routing (§41) ──────────────────────────────────────────────────
    def _subscribe_events(self) -> None:
        if self._bus is None:
            return
        self._unsubscribe = self._bus.subscribe(self._on_bus_event)

    def _on_bus_event(self, event: dict[str, Any]) -> None:
        loop = self._loop_ref
        if loop is None or not loop.is_running():
            return
        loop.create_task(self._handle_event(event))

    async def _handle_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if not event_type:
            return
        subscribers = self._store.event_subscribers(event_type)
        if not subscribers:
            return
        event_id = self._event_id(event)
        facts = self._event_facts(event)
        for definition in subscribers:
            # Honor the trigger's event_filter BEFORE consuming the dedup slot, so
            # a non-matching event is simply not this automation's concern.
            if not self._event_matches_filter(event, definition.trigger.event_filter):
                continue
            if self._store.already_seen_event(definition.id, event_id):
                continue
            self._store.mark_event_seen(definition.id, event_id)
            gate = definition.condition
            if gate is not None:
                result = evaluate(gate.expression, ConditionContext(values=facts, history={}))
                if not result.truthy:
                    continue
            await self._launch(
                definition, trigger_type="event", trigger_ref=event_id,
                scheduled_for=to_utc_iso(self._now()),
            )

    def _event_matches_filter(self, event: dict[str, Any], event_filter: dict[str, Any]) -> bool:
        """Whether an event satisfies a trigger's `eventFilter`.

        Each filter key must equal the matching value; a key may be a bare data
        field (`ticker`), a dotted `data.` path, or an `event.` field. An empty
        filter matches everything.
        """
        if not event_filter:
            return True
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        for key, expected in event_filter.items():
            if key.startswith("event."):
                actual = event.get(key.split(".", 1)[1])
            elif key.startswith("data."):
                actual = data.get(key.split(".", 1)[1])
            elif key in data:
                actual = data.get(key)
            else:
                actual = event.get(key)
            if actual != expected:
                return False
        return True

    def _event_id(self, event: dict[str, Any]) -> str:
        if event.get("id"):
            return str(event["id"])
        run_id = event.get("runId")
        seq = event.get("seq")
        if run_id and seq is not None:
            return f"{run_id}:{seq}"
        digest = hashlib.sha256(
            json.dumps({"t": event.get("type"), "d": event.get("data")}, sort_keys=True, default=str).encode()
        ).hexdigest()[:24]
        return f"evt_{digest}"

    def _event_facts(self, event: dict[str, Any]) -> dict[str, Any]:
        """Flatten an event into condition facts (bare, `data.`-prefixed, `event.`)."""
        facts: dict[str, Any] = {"event.type": event.get("type")}
        data = event.get("data")
        if isinstance(data, dict):
            for key, value in data.items():
                facts[key] = value
                facts[f"data.{key}"] = value
        return facts

    async def emit_event(self, event_type: str, data: Optional[dict[str, Any]] = None) -> None:
        """Publish an internal event so event-triggered automations can match it."""
        event = {
            "id": f"evt_{uuid.uuid4().hex[:20]}",
            "runId": None,
            "type": event_type,
            "seq": None,
            "at": now_iso(),
            "data": data or {},
        }
        if self._bus is not None:
            self._bus.publish(event)
        else:
            await self._handle_event(event)

    # ── Tick ─────────────────────────────────────────────────────────────────
    async def _tick(self) -> None:
        await self._due_tick()
        await self._file_watch_tick()
        await self._condition_tick()
        await self._drain_queue()

    async def _due_tick(self) -> None:
        now = self._now()
        for definition in self._store.due_automations(to_utc_iso(now)):
            await self._launch(
                definition, trigger_type=definition.trigger.type,
                trigger_ref=None, scheduled_for=to_utc_iso(now),
            )
        for hook in list(self._tick_hooks):
            try:
                await hook(now)
            except Exception:  # noqa: BLE001 - a heartbeat must never break automation
                log.warning("automation tick hook failed", exc_info=True)

    def add_tick_hook(self, hook: Any) -> None:
        """Register a coroutine hook called once per scheduling pass (W4)."""
        if hook not in self._tick_hooks:
            self._tick_hooks.append(hook)

    async def _file_watch_tick(self) -> None:
        for definition in self._store.event_subscribers(FILE_CHANGED_EVENT):
            path = definition.trigger.event_filter.get("path")
            if not path:
                continue
            try:
                import os as _os  # noqa: PLC0415

                stat = _os.stat(path)
                signature = f"{int(stat.st_size)}:{int(stat.st_mtime_ns)}"
            except OSError:
                continue
            previous = self._watchers.get(definition.id)
            self._watchers[definition.id] = signature
            if previous is not None and previous != signature:
                await self._launch(
                    definition, trigger_type="event", trigger_ref=f"file:{path}",
                    scheduled_for=to_utc_iso(self._now()),
                )

    async def _condition_tick(self) -> None:
        for definition in self._store.condition_automations():
            trigger = definition.trigger
            spec = trigger.condition
            if spec is None:
                continue
            variables = spec.variables or extract_variables(spec.expression)
            facts = await self._facts.snapshot(variables)
            state = self._store.get_condition_state(definition.id) or {}
            previous_result = state.get("lastResult")
            history = state.get("values") or {}
            result = evaluate(spec.expression, ConditionContext(values=facts, history=history))
            should_fire = decide_edge(result.state, previous_result) if spec.edge else result.truthy
            # Persist: new facts become history for the next evaluation. UNAVAILABLE
            # never overwrites the last decided result (§9).
            new_last = result.state if result.state != "unavailable" else previous_result
            self._store.set_condition_state(
                definition.id, last_result=new_last, previous_result=previous_result,
                values=facts, history=history,
            )
            if should_fire:
                await self._launch(
                    definition, trigger_type="condition", trigger_ref=None,
                    scheduled_for=to_utc_iso(self._now()),
                )

    async def _drain_queue(self) -> None:
        for execution in self._store.active_executions():
            if execution["state"] != "queued":
                continue
            definition = self._store.get_automation(execution["automationId"])
            if definition is None or definition.state != "active":
                self._store.finish_execution(execution["id"], state="skipped",
                                             outcome_reason="the automation is no longer active")
                continue
            if len(self._active) >= self._max_in_flight:
                return
            # In-flight only: the queued execution we are considering must not count
            # against the cap, or the queue can never drain.
            if self._store.in_flight_count(definition.id) >= definition.concurrency.max_concurrent_runs:
                continue
            self._store.update_execution(execution["id"], state="triggered")
            self._spawn(definition, execution)

    # ── Launch / concurrency ─────────────────────────────────────────────────
    async def _launch(
        self, definition: AutomationDefinition, *, trigger_type: str, trigger_ref: Optional[str],
        scheduled_for: str,
    ) -> Optional[str]:
        # `active` is the only state a scheduled/event/fabric launch may use. A
        # MANUAL launch may also run a paused automation (see `fire`).
        manual_paused = definition.state == "paused" and trigger_type == "manual"
        if definition.state != "active" and not manual_paused:
            return None
        # Scheduler → `schedule.triggered` → Event Bus, so a timed fire is an
        # observable event rather than a tightly-coupled Scheduler→Agent jump.
        if trigger_type in ("schedule", "cron", "interval", "one_time", "misfire", "recovery", "catch_up"):
            try:
                await self.emit_event("schedule.triggered", {
                    "automationId": definition.id,
                    "triggerType": trigger_type,
                    "triggerRef": trigger_ref,
                    "scheduledFor": scheduled_for,
                })
            except Exception:  # noqa: BLE001 - an advisory event never blocks a fire
                log.debug("schedule.triggered emit failed", exc_info=True)
        # Advance the due anchor BEFORE launching so a re-tick cannot double-fire.
        # Only a SCHEDULING launch moves the schedule: a manual "Run now" or a
        # recovery re-run must leave `nextRunAt` untouched (fire()'s contract).
        if trigger_type in _SCHEDULING_TRIGGER_TYPES:
            self._advance_next_run(definition)
        allowed, reason = self._breaker_allows(definition)
        if not allowed:
            execution = self._store.start_execution(
                definition, trigger_type=trigger_type, trigger_ref=trigger_ref,
                scheduled_for=scheduled_for, snapshot=self._snapshot(definition), state="blocked",
            )
            self._store.append_execution_event(execution["id"], "blocked", {"reason": reason})
            self._store.finish_execution(execution["id"], state="blocked", outcome_reason=reason)
            return execution["id"]

        running = self._store.running_count(definition.id)
        policy = definition.concurrency
        if running >= policy.max_concurrent_runs:
            if policy.on_conflict == "skip_if_running":
                execution = self._store.start_execution(
                    definition, trigger_type=trigger_type, trigger_ref=trigger_ref,
                    scheduled_for=scheduled_for, snapshot=self._snapshot(definition), state="skipped",
                )
                self._store.finish_execution(
                    execution["id"], state="skipped",
                    outcome_reason="This automation was still running, so this round was skipped.",
                )
                return execution["id"]
            if policy.on_conflict == "cancel_previous":
                await self._cancel_active(definition.id)
            # queue_if_running (or after cancelling): fall through to queue.
            execution = self._store.start_execution(
                definition, trigger_type=trigger_type, trigger_ref=trigger_ref,
                scheduled_for=scheduled_for, snapshot=self._snapshot(definition), state="queued",
            )
            self._store.append_execution_event(execution["id"], "queued", {"reason": "concurrency limit"})
            return execution["id"]

        if len(self._active) >= self._max_in_flight:
            execution = self._store.start_execution(
                definition, trigger_type=trigger_type, trigger_ref=trigger_ref,
                scheduled_for=scheduled_for, snapshot=self._snapshot(definition), state="queued",
            )
            return execution["id"]

        execution = self._store.start_execution(
            definition, trigger_type=trigger_type, trigger_ref=trigger_ref,
            scheduled_for=scheduled_for, snapshot=self._snapshot(definition), state="triggered",
        )
        self._spawn(definition, execution)
        return execution["id"]

    def _spawn(self, definition: AutomationDefinition, execution: dict[str, Any]) -> None:
        task = asyncio.create_task(self._execute(definition, execution))
        self._active[execution["id"]] = task

        def _done(_t: asyncio.Task[Any], execution_id: str = execution["id"]) -> None:
            self._active.pop(execution_id, None)

        task.add_done_callback(_done)

    async def _cancel_active(self, automation_id: str) -> None:
        for execution in self._store.active_executions():
            if execution["automationId"] != automation_id:
                continue
            task = self._active.get(execution["id"])
            if task is not None:
                task.cancel()
            self._store.finish_execution(
                execution["id"], state="cancelled",
                outcome_reason="A newer run replaced this one.",
            )

    async def fire(
        self, automation_id: str, *, trigger_type: str = "manual", trigger_ref: Optional[str] = None,
        allow_paused: bool = False,
    ) -> Optional[str]:
        """Fire now (manual/webhook/API). Never alters the schedule.

        The operator's "Run now" may fire a PAUSED automation (`allow_paused`,
        set only by the automation run route): pausing stops the scheduler, not
        an explicit request to test the bot. Callers that do not opt in — the
        jobs webhook, agent tools, the fabric — still require `active`, so a
        paused bot cannot be woken by anything except the operator.
        """
        definition = self._store.get_automation(automation_id)
        allowed = ("active", "paused") if (allow_paused and trigger_type == "manual") else ("active",)
        if definition is None or definition.state not in allowed:
            return None
        return await self._launch(
            definition, trigger_type=trigger_type, trigger_ref=trigger_ref,
            scheduled_for=to_utc_iso(self._now()),
        )

    async def resume_execution(self, execution_id: str) -> bool:
        """Resume a WAITING execution after its approval was granted (§28).

        Re-enters `_execute`, which re-checks the (now-approved) single-use
        approval and proceeds — the execution keeps its id, version and snapshot,
        so no context is lost. Returns False when the execution is not waiting.
        """
        execution = self._store.get_execution(execution_id)
        if execution is None or execution["state"] != "waiting":
            return False
        definition = self._store.get_automation(execution["automationId"])
        if definition is None:
            return False
        self._store.update_execution(execution_id, state="triggered")
        self._store.append_execution_event(execution_id, "resumed", {"reason": "approval granted"})
        self._spawn(definition, execution)
        return True

    async def dry_run(self, automation_id: str) -> Optional[dict[str, Any]]:
        definition = self._store.get_automation(automation_id)
        if definition is None:
            return None
        return {
            "automationId": automation_id,
            "automationVersion": definition.version,
            "plannedActions": plan_actions(definition),
            "condition": definition.condition.expression if definition.condition else None,
            "permissions": definition.permissions.model_dump(by_alias=True),
            "delivery": [t.model_dump(by_alias=True) for t in definition.delivery.effective_targets()],
        }

    # ── Execute ──────────────────────────────────────────────────────────────
    async def _execute(self, definition: AutomationDefinition, execution: dict[str, Any]) -> None:
        execution_id = execution["id"]
        lease = f"exec:{execution_id}"
        ttl = definition.execution.max_runtime_s + LEASE_SLACK_S
        if not self._store.acquire_lock(lease, self._worker_id, ttl_s=ttl):
            self._store.finish_execution(execution_id, state="skipped",
                                         outcome_reason="another worker holds this execution")
            return
        # Keep the lease alive for the whole run: several attempts plus backoff
        # sleeps can exceed `max_runtime_s + slack`, and a stale lease would let
        # another worker reclaim and re-run the identical execution.
        renew_task = asyncio.create_task(self._renew_lease(lease, ttl))
        run_id = execution["runId"] or f"autorun_{uuid.uuid4().hex[:16]}"
        self._store.update_execution(execution_id, state="running", runId=run_id, workerId=self._worker_id)
        self._store.append_execution_event(execution_id, "running", {"runId": run_id})
        state = "failed"
        reason: Optional[str] = None
        error_class: Optional[str] = None
        result: Optional[DispatchResult] = None
        attempt = int(execution.get("attempt") or 1)
        max_attempts = int(execution.get("maxAttempts") or 1)
        limit_retries = definition.limits.max_retries
        if limit_retries is not None:
            max_attempts = min(max_attempts, 1 + limit_retries)
        try:
            while True:
                approved, approval_id = await self._dispatcher.check_approval(definition)
                if not approved:
                    state = "waiting"
                    reason = "This automation needs an operator's approval before it can run."
                    self._store.update_execution(execution_id, state="waiting", attempt=attempt)
                    self._store.append_execution_event(execution_id, "waiting", {"approvalId": approval_id})
                    await self._deliver(definition, execution_id, run_id, "waiting", None)
                    self._store.update_execution(execution_id, state="waiting", outcomeReason=reason)
                    renew_task.cancel()
                    self._store.release_lock(lease, self._worker_id)
                    return
                result = await asyncio.wait_for(
                    self._dispatcher.dispatch(definition, execution_id=execution_id, run_id=run_id),
                    # `timeoutS`, when set, is a tighter per-attempt cap than the
                    # run's max runtime; otherwise the max runtime is the bound.
                    timeout=definition.execution.timeout_s or definition.execution.max_runtime_s,
                )
                if result.state in ("completed", "skipped", "blocked"):
                    # A decision that did not trade is still a FINISHED run. Treating
                    # "skipped"/"blocked" as a failure would retry a refusal and mark
                    # a normal no-trade bar as a broken bot.
                    state = result.state
                    reason = result.error or result.output
                    break
                error_class = result.error_class or "INTERNAL_ERROR"
                reason = result.error
                if not self._should_retry(definition, error_class, attempt, max_attempts):
                    state = "failed"
                    break
                attempt += 1
                delay = self._backoff(definition, attempt)
                self._store.update_execution(execution_id, state="retrying", attempt=attempt)
                self._store.append_execution_event(
                    execution_id, "retrying", {"attempt": attempt, "errorClass": error_class, "delayS": delay}
                )
                await asyncio.sleep(delay)
        except asyncio.TimeoutError:
            state, reason, error_class = "timed_out", "This automation took too long and was stopped.", "PROVIDER_TIMEOUT"
        except asyncio.CancelledError:
            state, reason, error_class = "cancelled", "This automation was stopped.", "CANCELLED"
        except Exception as exc:  # noqa: BLE001 - a failure is recorded, never raised
            state, reason, error_class = "failed", str(exc)[:2000], "INTERNAL_ERROR"
        renew_task.cancel()

        # Resource limits: token/cost are detected after the run (providers bill per
        # call), and exceeding them fails the execution honestly.
        if state == "completed" and result is not None:
            exceeded = self._limit_exceeded(definition, result)
            if exceeded:
                state, reason, error_class = "failed", exceeded, "RESOURCE_LIMIT_EXCEEDED"

        run_id_out = result.run_id if result and result.run_id else run_id
        cost = result.cost if result else None
        tokens = result.tokens if result else None
        decision = (getattr(result, "detail", None) or {}).get("decision") if result else None
        self._apply_breaker(definition, state)
        self._store.finish_execution(
            execution_id, state=state, outcome_reason=reason, error_class=error_class,
            run_id=run_id_out, cost=cost, tokens=tokens,
            output=self._bounded_output(result.output if result else None),
            decision=decision,
        )
        self._store.append_execution_event(execution_id, state, {"reason": reason, "errorClass": error_class})
        # One completion contract for chains: a monitor bot finishing can wake a
        # desk (event trigger with `eventFilter: { automationId }`). The OUTPUT is
        # deliberately NOT on the event — it stays on the execution, so a bus event
        # never carries a memo (size and provenance).
        await self.emit_event(
            "automation.failed" if state in ("failed", "timed_out") else "automation.completed",
            {
                "automationId": definition.id,
                "executionId": execution_id,
                "runId": run_id_out,
                "status": state,
                "triggerType": definition.trigger.type,
                "teamRunId": (getattr(result, "detail", None) or {}).get("teamRunId") if result else None,
            },
        )
        detail = getattr(result, "detail", None) or {}
        pending = detail.get("pendingApprovals") if result else None
        # A quiet run ("nothing changed") stays in run history but does not
        # notify, deliver a report, or post to the bot's chat. A pending order
        # always breaks quiet, because the operator must decide it.
        if not detail.get("quiet"):
            await self._deliver(
                definition, execution_id, run_id_out, state,
                result.output if result else None,
                pending_approvals=pending,
            )
        self._store.release_lock(lease, self._worker_id)
        # A one-time automation that has fired has no next run; it stays (audit).
        if definition.trigger.type == "one_time":
            self._store.set_next_run(definition.id, None)

    async def _renew_lease(self, name: str, ttl_s: float) -> None:
        """Renew a run's lease at a third of its TTL until cancelled."""
        interval = max(5.0, float(ttl_s) / 3.0)
        try:
            while True:
                await asyncio.sleep(interval)
                self._store.renew_lock(name, self._worker_id, ttl_s=ttl_s)
        except asyncio.CancelledError:
            return

    def _should_retry(self, definition: AutomationDefinition, error_class: str, attempt: int, max_attempts: int) -> bool:
        if attempt >= max_attempts:
            return False
        retry = definition.retry
        non_retryable = set(retry.non_retryable_classes) | NON_RETRYABLE_ERROR_CLASSES
        if error_class in non_retryable:
            return False
        if retry.retryable_classes:
            return error_class in retry.retryable_classes
        return error_class in DEFAULT_RETRYABLE_ERROR_CLASSES

    def _backoff(self, definition: AutomationDefinition, attempt: int) -> float:
        retry = definition.retry
        delay = retry.initial_backoff_s * (retry.backoff_multiplier ** max(0, attempt - 2))
        delay = min(delay, retry.max_backoff_s)
        if retry.jitter:
            delay = delay * (0.5 + random.random() * 0.5)
        return max(0.0, delay)

    def _limit_exceeded(self, definition: AutomationDefinition, result: DispatchResult) -> Optional[str]:
        limits = definition.limits
        if limits.max_tokens is not None and result.tokens is not None and result.tokens > limits.max_tokens:
            return f"the run used {result.tokens} tokens, above the limit of {limits.max_tokens}"
        if limits.max_cost is not None and result.cost is not None and result.cost > limits.max_cost:
            return f"the run cost {result.cost}, above the limit of {limits.max_cost}"
        return None

    def _bounded_output(self, output: Optional[str]) -> Optional[str]:
        """The run's answer, bounded for storage. A completed automation's output
        is persisted so it can be READ later (run history + completion notice):
        the report/delivery surfaces used to drop it and the run looked empty."""
        if not output:
            return None
        return output if len(output) <= _OUTPUT_MAX_CHARS else output[:_OUTPUT_MAX_CHARS] + "…"

    # ── Breaker ──────────────────────────────────────────────────────────────
    def _breaker_state(self, definition: AutomationDefinition) -> str:
        if definition.consecutive_failures < FAILURE_THRESHOLD:
            return CLOSED
        opened = parse_iso(definition.breaker_opened_at)
        if opened is None:
            return OPEN
        elapsed = (self._now() - opened).total_seconds()
        return HALF_OPEN if elapsed >= COOLDOWN_S else OPEN

    def _breaker_allows(self, definition: AutomationDefinition) -> tuple[bool, Optional[str]]:
        state = self._breaker_state(definition)
        if state == CLOSED:
            return True, None
        if state == OPEN:
            return False, (
                f"This automation has failed {definition.consecutive_failures} times and is paused. "
                "Reset the breaker from the automation's page to try again."
            )
        return True, None

    def _apply_breaker(self, definition: AutomationDefinition, state: str) -> AutomationDefinition:
        if state == "completed":
            failures, breaker, opened_at = 0, CLOSED, None
        elif state in _FAILURE_STATES:
            failures = definition.consecutive_failures + 1
            if failures >= FAILURE_THRESHOLD:
                breaker, opened_at = OPEN, definition.breaker_opened_at or now_iso()
            else:
                breaker, opened_at = CLOSED, None
        else:
            return definition
        self._store.record_outcome(
            definition.id, status=state, ran_at=self._now().isoformat(),
            consecutive_failures=failures, breaker=breaker, breaker_opened_at=opened_at,
        )
        return self._store.get_automation(definition.id) or definition

    def reset_breaker(self, automation_id: str, *, reset_by: str = "operator") -> Optional[AutomationDefinition]:
        return self._store.reset_breaker(automation_id, reset_by=reset_by)

    # ── Delivery ─────────────────────────────────────────────────────────────
    async def _deliver(
        self, definition: AutomationDefinition, execution_id: str, run_id: Optional[str],
        state: str, output: Optional[str],
        *, pending_approvals: Optional[list[dict[str, Any]]] = None,
    ) -> list[dict[str, Any]]:
        try:
            return await deliver_automation(
                definition, execution_id=execution_id, run_id=run_id, state=state,
                output=output, store=self._store, bus=self._bus,
                pending_approvals=pending_approvals,
                channel_service=getattr(self._service, "channels", None),
            )
        except Exception as exc:  # noqa: BLE001 - delivery never fails the run
            log.warning("automation delivery failed", exc_info=True)
            return [{"adapter": definition.delivery.adapter, "ok": False,
                     "reasonCode": "DELIVERY_FAILED", "reason": str(exc)[:500]}]

    # ── Next-run computation (§21) ───────────────────────────────────────────
    def _anchor(self, definition: AutomationDefinition) -> _dt.datetime:
        return parse_iso(definition.last_run_at) or parse_iso(definition.created_at) or self._now()

    def _compute_next_run(
        self,
        definition: AutomationDefinition,
        *,
        after: Optional[_dt.datetime] = None,
        anchor: Optional[_dt.datetime] = None,
    ) -> Optional[str]:
        trigger = definition.trigger
        moment = after or self._now()
        if trigger.type == "cron" and trigger.cron:
            nxt = next_cron_fire(
                trigger.cron, trigger.timezone, moment,
                start_at=trigger.start_at, end_at=trigger.end_at,
            )
            return to_utc_iso(nxt) if nxt else None
        if trigger.type == "interval" and trigger.interval_seconds:
            # The interval grid is anchored to a fixed instant, NOT to the run's
            # completion time: otherwise every run would push the next fire out by
            # its own duration and the cadence would drift (a "1 minute" bot would
            # silently become "1 minute + run time"). Callers pass the slot that
            # just fired so the grid never moves; a fresh schedule falls back to
            # the anchor (last run / creation).
            base = anchor or self._anchor(definition)
            nxt = next_interval_fire(
                trigger.interval_seconds, base, after_utc=moment,
                start_at=trigger.start_at, end_at=trigger.end_at,
            )
            return to_utc_iso(nxt) if nxt else None
        if trigger.type == "one_time":
            nxt = next_one_time_fire(trigger.run_at or "", after_utc=moment)
            return to_utc_iso(nxt) if nxt else None
        return None

    def _advance_next_run(self, definition: AutomationDefinition) -> None:
        if definition.trigger.type not in ("cron", "interval", "one_time"):
            return
        anchor = None
        if definition.trigger.type == "interval":
            # Anchor on the scheduled slot that is firing right now (its
            # `next_run_at`), never on `last_run_at` (recorded at completion), so
            # the interval grid is stable and run duration cannot drift it.
            anchor = parse_iso(definition.next_run_at) or self._anchor(definition)
        nxt = self._compute_next_run(definition, after=self._now(), anchor=anchor)
        self._store.set_next_run(definition.id, nxt)

    def refresh_schedule(self, automation_id: str) -> Optional[str]:
        """Recompute and persist next_run_at for an active automation."""
        definition = self._store.get_automation(automation_id)
        if definition is None:
            return None
        if definition.state != "active":
            self._store.set_next_run(automation_id, None)
            return None
        nxt = self._compute_next_run(definition)
        self._store.set_next_run(automation_id, nxt)
        return nxt

    # ── Snapshot (§31) ───────────────────────────────────────────────────────
    def _snapshot(self, definition: AutomationDefinition) -> dict[str, Any]:
        """The effective configuration at execution time (NEVER credentials)."""
        return {
            "automationId": definition.id,
            "automationVersion": definition.version,
            "trigger": definition.trigger.model_dump(by_alias=True),
            "condition": definition.condition.model_dump(by_alias=True) if definition.condition else None,
            "action": definition.action.model_dump(by_alias=True),
            "execution": definition.execution.model_dump(by_alias=True),
            "retry": definition.retry.model_dump(by_alias=True),
            "concurrency": definition.concurrency.model_dump(by_alias=True),
            "limits": definition.limits.model_dump(by_alias=True),
            "permissions": definition.permissions.model_dump(by_alias=True),
            "delivery": definition.delivery.model_dump(by_alias=True),
            "capturedAt": now_iso(),
        }

    # ── Recovery (§20, §47) ──────────────────────────────────────────────────
    async def _recover(self) -> None:
        reclaimed = self._store.reclaim_expired_locks()
        if reclaimed:
            log.info("automation: reclaimed %d expired leases", reclaimed)
        self._store.prune_event_dedup(
            older_than_iso=to_utc_iso(self._now() - _dt.timedelta(days=_DEDUP_RETENTION_DAYS))
        )
        # Interrupted executions (this process or a crashed worker): reconcile.
        for execution in self._store.active_executions():
            definition = self._store.get_automation(execution["automationId"])
            attempt = int(execution.get("attempt") or 1)
            max_attempts = int(execution.get("maxAttempts") or 1)
            self._store.finish_execution(
                execution["id"], state="failed",
                outcome_reason="Eclipse restarted while this was running.",
                error_class="INTERRUPTED",
            )
            self._store.append_execution_event(execution["id"], "failed", {"reason": "interrupted by restart"})
            if definition is not None and definition.state == "active" and attempt < max_attempts:
                await self._launch(
                    definition, trigger_type="recovery", trigger_ref=execution["id"],
                    scheduled_for=to_utc_iso(self._now()),
                )
        # Recompute next-run for every active timed automation, then apply misfire.
        now = self._now()
        for definition in self._store.list_automations(limit=2000, state="active"):
            if definition.trigger.type not in ("cron", "interval", "one_time"):
                continue
            # Detect missed fires relative to the ANCHOR (last run / creation), not
            # now — a fire that should have happened while the process was down.
            nxt = self._compute_next_run(definition, after=self._anchor(definition))
            if nxt is None:
                self._store.set_next_run(definition.id, None)
                continue
            due = parse_iso(nxt)
            if due is not None and due <= now:
                if (now - due).total_seconds() <= MISFIRE_GRACE_S:
                    # Only just due — a restart near the fire time, not an outage.
                    # Run it now instead of recording a spurious miss (a scheduled
                    # launch so the anchor still advances).
                    await self._launch(
                        definition, trigger_type="schedule", trigger_ref="misfire",
                        scheduled_for=nxt,
                    )
                else:
                    await self._apply_misfire(definition, due)
            else:
                self._store.set_next_run(definition.id, nxt)

    async def _apply_misfire(self, definition: AutomationDefinition, due: _dt.datetime) -> None:
        policy = definition.execution.misfire
        scheduled_for = to_utc_iso(due)
        # Idempotency: this exact slot may already have been handled by an earlier
        # recovery pass (the app can restart several times). Never record the same
        # missed slot twice — just make sure the schedule has moved past it.
        if self._store.execution_exists_for_slot(definition.id, scheduled_for):
            self._store.set_next_run(definition.id, self._compute_next_run(definition, after=self._now()))
            return
        if policy == "run_once":
            await self._launch(
                definition, trigger_type="schedule", trigger_ref="misfire", scheduled_for=scheduled_for,
            )
        elif policy == "catch_up":
            # Launch up to `catch_up_max` times to cover the missed window. Each
            # scheduling launch advances the anchor, so this is bounded by the
            # operator's chosen cap (default 1) — never an unbounded storm.
            for _ in range(max(1, int(definition.execution.catch_up_max))):
                await self._launch(
                    definition, trigger_type="schedule", trigger_ref="catch_up",
                    scheduled_for=scheduled_for,
                )
        else:  # skip
            execution = self._store.start_execution(
                definition, trigger_type="schedule", trigger_ref="misfire",
                scheduled_for=scheduled_for, snapshot=self._snapshot(definition), state="skipped",
            )
            self._store.append_execution_event(
                execution["id"], "skipped", {"reason": "missed while Eclipse was closed"}
            )
            self._store.finish_execution(
                execution["id"], state="skipped",
                outcome_reason="Eclipse was closed when this was due, so it was skipped.",
            )
            self._store.set_next_run(definition.id, self._compute_next_run(definition, after=self._now()))

    # ── Observability (§43) ──────────────────────────────────────────────────
    def metrics(self) -> dict[str, Any]:
        data = self._store.metrics()
        data["activeExecutionCount"] = len(self._active)
        data["inFlight"] = len(self._active)
        data["maxInFlight"] = self._max_in_flight
        data["workerId"] = self._worker_id
        data["tickMs"] = round(self._last_tick_ms, 2)
        data["tickSeconds"] = self._tick_seconds
        return data


__all__ = ["AutomationEngine", "DEFAULT_MAX_IN_FLIGHT", "TICK_SECONDS"]
