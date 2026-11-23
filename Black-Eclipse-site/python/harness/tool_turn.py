"""W3 — the ONE tool turn: preflight, optional parallel dispatch, postflight.

`graph.tools_node` builds a `ToolTurn` and calls `run`. Keeping the body here
does two things the size ratchet and the no-second-loop rule both ask for:

  * `graph.py` keeps only wiring;
  * budget, duplicate suppression, resume idempotency, effects, plugin hooks,
    guardrails, events, pruning and the checkpoint stay in ONE flow for both
    sequential and parallel execution.

Parallelism is deliberately narrow: only a CONTIGUOUS run of explicitly
`parallel_safe` calls is batched. `[read A, write B, read C]` is three serial
slots, because B may depend on A and C may depend on B. Effectful and
interactive calls never overlap anything.

`ToolRegistry.execute` (through `run_tool_with_recovery`) remains the only
dispatch seam. No handler is called directly and the result order is the order
the model issued the calls.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from langchain_core.messages import BaseMessage, ToolMessage

from .schemas import HarnessError
from .tool_guardrails import GuardrailDecision, ToolGuardrail
from .tools import ToolResult

#: The event name the UI/telemetry already understands for a stop.
GUARDRAIL_EVENT = "tool.guardrail"


def stable_key(name: str, args: dict[str, Any]) -> str:
    return f"{name}:{json.dumps(args, sort_keys=True, default=str)}"


def _has_image(message: BaseMessage) -> bool:
    content = getattr(message, "content", None)
    if not isinstance(content, list):
        return False
    if getattr(message, "additional_kwargs", {}).get("eclipse_media") != "desktop_screenshot":
        return False
    return any(
        isinstance(part, dict) and part.get("type") in ("image_url", "image")
        for part in content
    )


def _prune_media_messages(messages: list[BaseMessage], keep: int = 8) -> list[BaseMessage]:
    """Keep only the newest `keep` synthetic media messages.

    Screenshots are the bulk of a long desktop trajectory. The text history and
    every ToolMessage stay intact; only the extra HumanMessage that carries an
    image is dropped, oldest first. This is what stops a 60-step task from
    re-sending 60 full screenshots on every model call.
    """
    indices = [i for i, message in enumerate(messages) if _has_image(message)]
    if len(indices) <= keep:
        return messages
    drop = set(indices[: len(indices) - keep])
    return [message for i, message in enumerate(messages) if i not in drop]


def resolve_parallel_safe(registry: Any, name: str) -> bool:
    """Whether this call may share a batch. Explicit opt-in only."""
    tool = registry.get(name) if hasattr(registry, "get") else None
    if tool is None:
        return False
    if bool(getattr(tool, "interactive", False)):
        return False
    if getattr(tool, "effect", None):
        return False
    if str(getattr(tool, "permission", "read")) != "read":
        return False
    return getattr(tool, "parallel_safe", None) is True


@dataclass
class _Slot:
    index: int
    call_id: str
    name: str
    args: dict[str, Any]
    key: str = ""
    mode: str = "pending"  # pending|resume|duplicate|budget|vetoed|unknown|halted|skipped
    result: Optional[ToolResult] = None
    result_text: str = ""
    effectful: bool = False
    effect_begun: bool = False
    effect_finalized: bool = False
    started_recorded: bool = False
    step: int = 0
    warned: Optional[GuardrailDecision] = None
    dispatched: bool = False


@dataclass
class ToolTurnDeps:
    request: Any
    store: Any
    emitter: Any
    registry: Any
    ctx: Any
    effects: Any
    lineage: Any
    plan: Any
    gaps: Any
    gap_need: str
    deadline: Any
    run_timeout_s: float
    max_tool_calls: Optional[int]
    tool_timeout_ms: int
    allowed: Optional[set[str]]
    guard: ToolGuardrail
    hook: Callable[..., Any]
    write_checkpoint: Callable[..., Any]
    emit_step_completed: Callable[..., Any]
    parallel_mode: str = "off"  # off|shadow|on
    concurrency: int = 6
    batch_timeout_s: int = 120
    # W6: fenced progress write. Optional so existing tests/embedded callers
    # keep their constructor; the graph always supplies it.
    write_progress: Optional[Callable[..., Any]] = None


class ToolTurn:
    def __init__(self, deps: ToolTurnDeps) -> None:
        self.d = deps
        # Per-run recovery counters are shared BY REFERENCE with each batch
        # call's shallow context clone. A clone without these would reset the
        # retry/stuck accounting for every concurrent call.
        self._share_run_scoped_state()

    def _share_run_scoped_state(self) -> None:
        ctx = self.d.ctx
        try:
            from .recovery.progress import ProgressTracker  # noqa: PLC0415

            if not hasattr(ctx, "_recovery_progress"):
                ctx._recovery_progress = ProgressTracker()
            if not hasattr(ctx, "_recovery_hotpath"):
                import time as _time  # noqa: PLC0415

                ctx._recovery_hotpath = {
                    "attempts": 0, "signatures": {}, "started": _time.monotonic(),
                }
        except Exception:  # noqa: BLE001 - a frozen ctx must not break a turn
            pass

    def _clone_context(self) -> Any:
        """A shallow clone per batch call: per-call fields cannot race.

        Shared services/stores (and the run-scoped recovery counters above)
        stay shared by reference on purpose. `tool_timeout_ms`,
        `effect_authorized_digest` and `effect_token` are per call, so they
        must not be written by two workers onto one object.
        """
        try:
            from dataclasses import replace  # noqa: PLC0415

            return replace(self.d.ctx)
        except Exception:  # noqa: BLE001 - a foreign context is used as-is
            return self.d.ctx

    async def run(self, state: dict[str, Any]) -> dict[str, Any]:
        d = self.d
        if d.deadline.expired():
            message = f"Run exceeded its {int(d.run_timeout_s)}s deadline."
            await d.emitter.emit(
                "run.failed",
                {"error": {"code": "RUN_TIMEOUT", "message": message}},
            )
            return {**state, "error": {"code": "RUN_TIMEOUT", "message": message}}

        last = state["messages"][-1]
        calls = getattr(last, "tool_calls", None) or []
        tool_calls = int(state.get("tool_calls", 0) or 0)
        tool_errors = int(state.get("tool_errors", 0) or 0)
        step_index = int(state.get("step", 0) or 0)
        # Gap #10: a checkpoint is per TURN, not per run. Every file mutation in
        # this model step shares one key, so "undo this turn" restores the state
        # before the step's first write.
        try:
            d.ctx.checkpoint_turn = f"step{step_index or 1}"
            # The write service is cached per run; keep its turn key current.
            svc = getattr(d.ctx, "_fs_write_service", None)
            if svc is not None:
                svc._turn_key = d.ctx.checkpoint_turn
        except Exception:  # noqa: BLE001 - a frozen ctx must not break a turn
            pass
        prev_keys = list(state.get("last_turn_keys") or [])
        turn_keys: list[str] = []
        failure_details: list[dict[str, Any]] = []
        # Duplicate suppression is scoped to THIS agent step (one model turn).
        # A later turn may legitimately re-issue a call (next page, re-poll,
        # retry after new evidence). See the original loop's contract.
        seen_calls: dict[str, int] = {}

        slots: list[_Slot] = []
        halted: Optional[GuardrailDecision] = None
        for index, call in enumerate(calls):
            slot = _Slot(
                index=index,
                call_id=call.get("id") or f"call_{call.get('name', '')}",
                name=call.get("name", ""),
                args=call.get("args", {}) or {},
                step=step_index,
            )
            slots.append(slot)
            if halted is not None:
                slot.mode = "skipped"
                slot.result_text = (
                    "Not executed: the turn was stopped by a tool guardrail."
                )
                continue

            # Hard tool-call budget. A refused call is auditable and the model is
            # told plainly to answer from what it already gathered.
            if d.max_tool_calls is not None and tool_calls >= d.max_tool_calls:
                slot.mode = "budget"
                slot.result_text = (
                    f"Tool call refused: the maximum of {d.max_tool_calls} tool calls "
                    "for this run has been reached. Answer using the information "
                    "already gathered."
                )
                await d.emitter.emit(
                    "tool.failed",
                    {"name": slot.name, "toolCallId": slot.call_id,
                     "code": "TOOL_BUDGET_EXCEEDED"},
                )
                try:
                    from .event_sequence import append_durable  # noqa: PLC0415

                    append_durable(d.request.runId, "skill.tool_budget_exceeded", {
                        "maxToolCalls": d.max_tool_calls, "toolCalls": tool_calls,
                        "tool": slot.name,
                    })
                except Exception:  # noqa: BLE001 - audit must not break the run
                    pass
                tool_errors += 1
                failure_details.append({"name": slot.name, "code": "TOOL_BUDGET_EXCEEDED"})
                continue

            tool_calls += 1
            from .code.activity import tool_code_preview, tool_detail  # noqa: PLC0415

            safe_args = slot.args if isinstance(slot.args, dict) else {}
            detail = tool_detail(slot.name, safe_args)
            code_preview = tool_code_preview(slot.name, safe_args)
            await d.emitter.emit("tool.requested", {
                "name": slot.name, "toolCallId": slot.call_id,
                "step": state.get("step"),
                **({"detail": detail} if detail else {}),
                **({"codePreview": code_preview} if code_preview else {}),
            })

            slot.key = stable_key(slot.name, slot.args)
            turn_keys.append(slot.key)

            tool_def = d.registry.get(slot.name)
            effect_kind = getattr(tool_def, "effect", None) if tool_def is not None else None
            slot.effectful = bool(effect_kind) or (
                tool_def is not None
                and str(getattr(tool_def, "permission", "read")) != "read"
            )

            # Resume idempotency: reuse a recorded result instead of re-running.
            # W6: a `started`/`unknown` effectful call is never re-executed — the
            # side effect may already have happened. A read-only call is safe to
            # replay; its stale `started` row is overwritten by `done` below.
            already = (
                d.lineage.was_tool_executed(d.request.runId, step_index, slot.call_id)
                if d.lineage is not None else None
            )
            already_state = str((already or {}).get("state") or "done")
            if already is not None and already_state == "done":
                slot.mode = "resume"
                slot.result_text = already["text"]
                slot.result = ToolResult(
                    tool_call_id=slot.call_id, name=slot.name, ok=bool(already["ok"]),
                    text=already["text"],
                )
                continue
            if already is not None and already_state in ("started", "unknown"):
                if slot.effectful:
                    slot.mode = "unknown"
                    slot.result = ToolResult(
                        tool_call_id=slot.call_id, name=slot.name, ok=False,
                        text=(
                            f"{slot.name!r} was started by a previous attempt but its "
                            "outcome was never recorded. It was NOT run again. Reconcile "
                            "the external state (broker terminal / git status / file "
                            "hash) before retrying."
                        ),
                    )
                    slot.result_text = slot.result.text
                    if d.lineage is not None and already_state == "started":
                        d.lineage.mark_tool_unknown(
                            d.request.runId, step_index, slot.call_id,
                        )
                    continue
                # Read-only: replay is safe.

            if seen_calls.get(slot.key, 0) > 0:
                slot.mode = "duplicate"
                slot.result_text = (
                    f"Duplicate call to {slot.name!r} with identical arguments was not "
                    "executed again. Use the earlier result."
                )
                await d.emitter.emit(
                    "tool.failed",
                    {"name": slot.name, "toolCallId": slot.call_id,
                     "code": "TOOL_FORBIDDEN"},
                )
                tool_errors += 1
                failure_details.append({"name": slot.name, "code": "TOOL_FORBIDDEN"})
                continue
            seen_calls[slot.key] = seen_calls.get(slot.key, 0) + 1

            veto = await d.hook(
                "before_tool_call",
                {"tool": slot.name, "arguments": safe_args, "runId": d.request.runId},
            )
            if veto:
                slot.mode = "vetoed"
                slot.result = ToolResult(
                    tool_call_id=slot.call_id, name=slot.name, ok=False,
                    text=f"Tool {slot.name!r} was blocked by a plugin hook: {veto}",
                )
                slot.result_text = slot.result.text
                continue

            prior_effect = (
                d.effects.state(d.request.runId, step_index, slot.call_id)
                if slot.effectful and d.effects is not None else None
            )
            if prior_effect == "started":
                slot.mode = "unknown"
                slot.result = ToolResult(
                    tool_call_id=slot.call_id, name=slot.name, ok=False,
                    text=(
                        f"{slot.name!r} was started by a previous attempt but its "
                        "outcome was never recorded. It was NOT run again. Reconcile "
                        "the external state (broker terminal / git status / file "
                        "hash) before retrying."
                    ),
                )
                slot.result_text = slot.result.text
                if d.effects is not None:
                    d.effects.mark_unknown(d.request.runId, step_index, slot.call_id)
                if d.lineage is not None:
                    d.lineage.mark_tool_unknown(
                        d.request.runId, step_index, slot.call_id,
                    )
                continue

            # W3.2: the guardrail decision is made BEFORE effects begin or a
            # dispatch happens. A halt never starts a side effect.
            decision = d.guard.before(slot.name, slot.args, run_id=d.request.runId)
            if decision.action == "halt":
                halted = decision
                slot.mode = "halted"
                slot.result_text = (
                    f"Tool loop stopped by a guardrail ({decision.rule}): "
                    f"{decision.message}"
                )
                await d.emitter.emit(GUARDRAIL_EVENT, {
                    "name": slot.name, "toolCallId": slot.call_id,
                    "rule": decision.rule, "count": decision.count, "action": "halt",
                })
                continue
            if decision.action == "warn":
                slot.warned = decision
                await d.emitter.emit(GUARDRAIL_EVENT, {
                    "name": slot.name, "toolCallId": slot.call_id,
                    "rule": decision.rule, "count": decision.count, "action": "warn",
                })

            # W6: record intent BEFORE dispatch. A crash after this row is a
            # known `started` outcome, not a silent hole.
            if d.lineage is not None:
                d.lineage.mark_tool_started(
                    d.request.runId, step_index, slot.call_id, slot.name,
                )
                slot.started_recorded = True

            if slot.effectful and d.effects is not None:
                d.effects.begin(
                    d.request.runId, step_index, slot.call_id, slot.name,
                    str(effect_kind) if effect_kind else None,
                )
                slot.effect_begun = True

        # ── dispatch ─────────────────────────────────────────────────────────
        await self._dispatch(slots)

        # ── postflight ───────────────────────────────────────────────────────
        out_messages: list[BaseMessage] = []
        media_items: list[dict[str, Any]] = []
        for slot in slots:
            if slot.mode == "resume" and slot.result is not None:
                ok = bool(slot.result.ok)
                if not ok:
                    tool_errors += 1
                    failure_details.append({"name": slot.name, "code": "TOOL_ERROR"})
                await d.emitter.emit(
                    "tool.completed" if ok else "tool.failed",
                    {"name": slot.name, "toolCallId": slot.call_id, "resumed": True,
                     "code": None if ok else "TOOL_ERROR"},
                )
                await d.guard.after(
                    slot.name, slot.args, ok=ok, text=slot.result_text,
                    data=getattr(slot.result, "data", None),
                )
                out_messages.append(
                    ToolMessage(content=slot.result_text, tool_call_id=slot.call_id)
                )
                continue

            if slot.mode in ("budget", "duplicate"):
                out_messages.append(
                    ToolMessage(content=slot.result_text, tool_call_id=slot.call_id)
                )
                continue
            if slot.mode == "unknown":
                # W6: the effect may have happened. Refuse, surface it, and do
                # NOT overwrite the `unknown` row with `done`.
                tool_errors += 1
                failure_details.append(
                    {"name": slot.name, "code": "TOOL_OUTCOME_UNKNOWN"}
                )
                await d.emitter.emit(
                    "tool.failed",
                    {"name": slot.name, "toolCallId": slot.call_id,
                     "code": "TOOL_OUTCOME_UNKNOWN",
                     "reconcile": True},
                )
                await d.emitter.emit("effect.unknown", {
                    "name": slot.name, "toolCallId": slot.call_id,
                    "reason": "outcome_unknown", "reconcile": True,
                })
                out_messages.append(
                    ToolMessage(content=slot.result_text, tool_call_id=slot.call_id)
                )
                continue
            if slot.mode in ("halted", "skipped"):
                out_messages.append(
                    ToolMessage(content=slot.result_text, tool_call_id=slot.call_id)
                )
                continue

            result = slot.result
            if result is None:
                # Defensive: a pending slot that never dispatched (a cancellation
                # before the task started) still gets a paired message. If an
                # effect began but never ran, the outcome is unknown, not done.
                result = self._cancelled_result(slot)
                slot.result = result
                if slot.effect_begun and not slot.effect_finalized and d.effects is not None:
                    d.effects.mark_unknown(d.request.runId, step_index, slot.call_id)
                    slot.effect_finalized = True
            if slot.warned is not None:
                result.text = f"{result.text}{slot.warned.directive()}"

            if d.plan is not None:
                try:
                    d.plan.record_tool_result(result, d.request.runId)
                except Exception:  # noqa: BLE001 - evidence must not break the run
                    pass
            await d.hook(
                "after_tool_call",
                {"tool": slot.name, "ok": result.ok, "runId": d.request.runId},
            )
            if d.lineage is not None and (slot.dispatched or slot.mode == "vetoed"):
                d.lineage.mark_tool_done(
                    d.request.runId, step_index, slot.call_id, result,
                )
            if result.ok:
                from .code.activity import tool_result_summary  # noqa: PLC0415

                d.gaps.record_result(
                    d.request.runId, tool=slot.name, result=result,
                    result_text=result.text, need=d.gap_need,
                    task_id=str(getattr(d.request, "taskId", "") or ""),
                )
                summary = tool_result_summary(slot.name, result.data)
                await d.emitter.emit(
                    "tool.completed",
                    {
                        "name": slot.name,
                        "toolCallId": slot.call_id,
                        "durationMs": result.duration_ms,
                        "truncated": result.truncated,
                        "shownChars": result.shown_chars,
                        "totalChars": result.total_chars,
                        **({"summary": summary} if summary else {}),
                    },
                )
                if result.truncated:
                    await d.emitter.emit(
                        "tool.truncated",
                        {"name": slot.name, "toolCallId": slot.call_id,
                         "shownChars": result.shown_chars,
                         "totalChars": result.total_chars},
                    )
            else:
                tool_errors += 1
                failure_details.append(
                    {"name": slot.name,
                     "code": result.error.code if result.error else "TOOL_ERROR"}
                )
                d.gaps.record_result(
                    d.request.runId, tool=slot.name, result=result,
                    result_text=result.text, need=d.gap_need,
                    task_id=str(getattr(d.request, "taskId", "") or ""),
                )
                await d.emitter.emit(
                    "tool.failed",
                    {"name": slot.name, "toolCallId": slot.call_id,
                     "code": result.error.code if result.error else "TOOL_ERROR",
                     "durationMs": result.duration_ms},
                )
            try:
                d.store.record_tool_execution(d.request.runId, result)
            except Exception:  # noqa: BLE001
                pass
            d.guard.after(
                slot.name, slot.args, ok=result.ok, text=result.text,
                data=getattr(result, "data", None),
            )
            out_messages.append(
                ToolMessage(content=result.text, tool_call_id=slot.call_id)
            )
            data = getattr(result, "data", None)
            media = data.get("media") if isinstance(data, dict) else None
            if isinstance(media, dict) and media.get("dataUrl"):
                media_items.append(media)

        if media_items:
            # Gap #11: a read image/PDF is attached as REAL model content after
            # its tool result, so the next step can actually see it.
            from .code.media import model_message  # noqa: PLC0415

            out_messages.append(model_message(media_items))
        if d.write_progress is not None:
            d.write_progress(toolCalls=tool_calls, toolErrors=tool_errors)
        else:  # pragma: no cover - embedded callers only
            d.store.update_run(d.request.runId, toolCalls=tool_calls, toolErrors=tool_errors)
        step_failures = tool_errors - int(state.get("tool_errors", 0) or 0)
        turn_set = sorted(set(turn_keys))
        stalled = bool(turn_set) and turn_set == sorted(set(prev_keys))
        all_messages = [*state["messages"], *out_messages]
        all_messages = _prune_media_messages(all_messages)
        from .context.pruning import maybe_prune_for_run  # noqa: PLC0415

        all_messages, _pruned_count = await maybe_prune_for_run(
            all_messages, d.emitter, int(state.get("step", 0) or 0),
        )
        # Accumulate the route memory the answer guard needs: a name the run has
        # already called is never suggested again, and a route that answered
        # UNAVAILABLE is named as exhausted rather than retried unchanged.
        turn_tools = [str(slot.name) for slot in slots]
        turn_unavailable = [
            str(slot.name) for slot in slots
            if (slot.result is not None and not slot.result.ok)
            or str(slot.result_text or "").lstrip().lower().startswith("unavailable")
        ]
        tools_used = sorted(set([*(state.get("tools_used") or []), *turn_tools]))
        unavailable_tools = sorted(set([
            *(state.get("unavailable_tools") or []), *turn_unavailable,
        ]))
        tool_done: dict[str, Any] = {
            **state, "messages": all_messages,
            "tool_calls": tool_calls, "tool_errors": tool_errors,
            "step_failures": step_failures,
            "stalled": stalled,
            "last_turn_keys": turn_set,
            "step_failure_details": failure_details,
            "tools_used": tools_used,
            "unavailable_tools": unavailable_tools,
        }
        if halted is not None:
            tool_done["guardrail_halt"] = {
                "rule": halted.rule, "count": halted.count, "message": halted.message,
            }
            tool_done["final"] = (
                "Stopped to avoid a tool loop. "
                f"{halted.message}\n\n"
                "No further tool calls were run. Continue from the evidence "
                "already gathered, or change the approach before retrying."
            )
        await d.write_checkpoint("tool_results", tool_done)
        await d.emit_step_completed(
            step_index,
            status="failed" if step_failures else "ok",
            tool_calls=tool_calls,
            tool_errors=tool_errors,
            error=(
                {"code": (failure_details[-1].get("code") if failure_details else "TOOL_ERROR")}
                if step_failures else None
            ),
        )
        return tool_done

    async def _dispatch(self, slots: list[_Slot]) -> None:
        d = self.d
        pending = [slot for slot in slots if slot.mode == "pending"]
        if not pending:
            return

        groups = self._contiguous_groups(slots)
        if d.parallel_mode != "off":
            await d.emitter.emit("tool.batch_plan", {
                "calls": len(pending),
                "batches": [[slot.index for slot in group] for group in groups],
                "parallelSafe": [
                    resolve_parallel_safe(d.registry, slot.name) for slot in pending
                ],
                "wouldSaveMs": None,
            })

        started = time.monotonic()
        if d.parallel_mode != "on":
            for slot in pending:
                slot.result = await self._execute_one(slot)
                slot.dispatched = True
            await self._emit_batch(pending, groups, started, concurrency=1)
            return

        concurrency = max(1, min(int(d.concurrency), 12))
        semaphore = asyncio.Semaphore(concurrency)
        for group in groups:
            if len(group) == 1 or not all(
                resolve_parallel_safe(d.registry, slot.name) for slot in group
            ):
                for slot in group:
                    slot.result = await self._execute_one(slot)
                    slot.dispatched = True
                continue
            await self._execute_batch(group, semaphore)
        await self._emit_batch(pending, groups, started, concurrency=concurrency)

    async def _emit_batch(
        self, pending: list[_Slot], groups: list[list[_Slot]],
        started: float, *, concurrency: int,
    ) -> None:
        """One W8 batch record. Result order is structural: slots keep their index."""
        d = self.d
        parallel_groups = [
            group for group in groups
            if len(group) > 1 and all(resolve_parallel_safe(d.registry, s.name) for s in group)
        ]
        max_concurrency = min(concurrency, max((len(g) for g in parallel_groups), default=1))
        try:
            await d.emitter.emit("tool.batch", {
                "calls": len(pending),
                "batches": len(groups),
                "maxConcurrency": max_concurrency,
                # Slots are filled in issue order; a violation would mean the
                # postflight reordered them. The conformance case asserts this.
                "orderingViolations": 0,
                "durationMs": int((time.monotonic() - started) * 1000.0),
                "mode": d.parallel_mode,
            })
        except Exception:  # noqa: BLE001 - telemetry never fails a tool turn
            pass

    def _contiguous_groups(self, slots: list[_Slot]) -> list[list[_Slot]]:
        """Group adjacent dispatchable slots; a refused call breaks the run."""
        groups: list[list[_Slot]] = []
        current: list[_Slot] = []
        for slot in slots:
            if slot.mode != "pending":
                if current:
                    groups.append(current)
                    current = []
                continue
            current.append(slot)
        if current:
            groups.append(current)
        return groups

    async def _execute_batch(
        self, group: list[_Slot], semaphore: asyncio.Semaphore,
    ) -> None:
        d = self.d
        remaining = max(0.0, d.deadline.remaining())
        budget = min(float(d.batch_timeout_s), remaining) if remaining else 0.0

        async def _one(slot: _Slot) -> None:
            async with semaphore:
                if budget <= 0:
                    slot.result = self._timeout_result(
                        slot, "the run or batch deadline expired before dispatch"
                    )
                    return
                try:
                    slot.result = await asyncio.wait_for(
                        self._execute_one(slot, isolated=True), timeout=budget,
                    )
                except asyncio.TimeoutError:
                    slot.result = self._timeout_result(
                        slot, f"the batch deadline of {int(budget)}s expired"
                    )
                slot.dispatched = True

        await asyncio.gather(*(_one(slot) for slot in group))

    async def _execute_one(self, slot: _Slot, *, isolated: bool = False) -> ToolResult:
        d = self.d
        from .recovery.hotpath import run_tool_with_recovery  # noqa: PLC0415

        ctx = self._clone_context() if isolated else d.ctx
        try:
            result = await run_tool_with_recovery(
                d.registry, slot.name, slot.call_id, slot.args, ctx,
                d.allowed, d.tool_timeout_ms, d.request,
            )
        except asyncio.CancelledError:
            self._finalize_effect(slot, ok=False, unknown=True)
            raise
        except Exception as exc:  # noqa: BLE001 - one call never breaks the turn
            result = ToolResult(
                tool_call_id=slot.call_id, name=slot.name, ok=False,
                text=f"Tool {slot.name!r} failed: {exc}",
                error=HarnessError(code="TOOL_ERROR", message=str(exc)),
            )
            self._finalize_effect(slot, ok=False, unknown=True)
            return result
        # A handler can return a failure after a partial side effect; that is a
        # recorded `done` with ok=False, not an unknown. Only an exception path
        # (or a process death) leaves the outcome genuinely unknown.
        self._finalize_effect(slot, ok=result.ok, unknown=False)
        return result

    def _finalize_effect(self, slot: _Slot, *, ok: bool, unknown: bool) -> None:
        d = self.d
        if not slot.effect_begun or slot.effect_finalized or d.effects is None:
            return
        if unknown:
            d.effects.mark_unknown(d.request.runId, slot.step, slot.call_id)
        else:
            d.effects.finish(
                d.request.runId, slot.step, slot.call_id, ok=ok,
            )
        slot.effect_finalized = True

    def _timeout_result(self, slot: _Slot, message: str) -> ToolResult:
        return ToolResult(
            tool_call_id=slot.call_id, name=slot.name, ok=False,
            text=f"Tool {slot.name!r} timed out: {message}.",
            error=HarnessError(code="TOOL_TIMEOUT", message=message),
        )

    def _cancelled_result(self, slot: _Slot) -> ToolResult:
        return ToolResult(
            tool_call_id=slot.call_id, name=slot.name, ok=False,
            text=f"Tool {slot.name!r} was cancelled before it completed.",
            error=HarnessError(code="CANCELLED", message="cancelled"),
        )


__all__ = [
    "GUARDRAIL_EVENT",
    "ToolTurn",
    "ToolTurnDeps",
    "resolve_parallel_safe",
    "stable_key",
]
