"""Automation service facade (§40) — the one entry point for API, tools and UI.

Owns CRUD, lifecycle transitions, validation/testing, manual execution, dry run,
history, versioning and observability. The engine owns WHEN; this owns the
durable domain operations. No agentic logic here.
"""

from __future__ import annotations

import secrets
from typing import Any, Optional

from pydantic.alias_generators import to_camel

from ..persistence import now_iso
from .engine import AutomationEngine
from .lifecycle import TransitionError, assert_lifecycle_transition
from .model import (
    ACTION_KINDS,
    AutomationDefinition,
    DeliveryPolicy,
    ExecutionPolicy,
    PermissionPolicy,
    ResourceLimits,
    RetryPolicy,
    ConcurrencyPolicy,
)
from .store import AutomationStore, new_automation_id
from .validation import ValidationIssue, is_valid, validate_automation


class AutomationError(ValueError):
    """A domain refusal with a stable code (maps to an HTTP 4xx)."""

    def __init__(self, code: str, message: str, *, issues: Optional[list[ValidationIssue]] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.issues = issues or []


# Fields a PATCH may replace (absent = preserve).
_PATCHABLE = (
    "name", "description", "trigger", "condition", "action", "execution", "retry",
    "concurrency", "limits", "permissions", "delivery", "scope_id", "tags", "state",
    "execution_authority",
)


def _patched(patch: dict[str, Any], key: str) -> Any:
    """Read a patchable field from EITHER spelling.

    Python callers send snake_case; the TS front door sends camelCase. Until now
    every patchable key happened to spell the same both ways, so this never
    mattered. `execution_authority` is the first that does not.
    """
    for candidate in (key, to_camel(key)):
        if candidate in patch and patch[candidate] is not None:
            return patch[candidate]
    return None


class AutomationService:
    def __init__(self, service: Any, store: AutomationStore, engine: AutomationEngine) -> None:
        self._service = service
        self._store = store
        self._engine = engine

    @property
    def memory(self) -> Any:
        """The owner's memory service (for bot-private conversation purges)."""
        return getattr(self._service, "memory", None)

    # ── Validation ───────────────────────────────────────────────────────────
    def validate(self, definition: AutomationDefinition) -> list[ValidationIssue]:
        return validate_automation(
            definition,
            skill_registry=getattr(self._service, "skill_registry", None),
            workflow_store=self._safe_workflow_store(),
            known_agents=self._known_agents(),
            tool_names=self._tool_names(),
            strategy_lookup=self._strategy_lookup(),
        )

    def _strategy_lookup(self):
        """`(id, version) -> record|None` from the quant store, when installed.

        The strategy layer is optional (a runtime without it must still boot), so
        a missing store yields `None` and validation simply skips the existence
        check rather than refusing every trading bot.
        """
        store = getattr(self._service, "quant_store", None)
        if store is None:
            return None
        try:
            return lambda strategy_id, version: store.get_strategy(strategy_id, version)
        except Exception:  # noqa: BLE001
            return None

    def _safe_workflow_store(self) -> Any:
        try:
            return self._service.workflow_store()
        except Exception:  # noqa: BLE001
            return None

    def _known_agents(self) -> Optional[set[str]]:
        try:
            from ..agents import AGENT_PRESETS  # noqa: PLC0415

            return set(AGENT_PRESETS.keys())
        except Exception:  # noqa: BLE001
            return None

    def _tool_names(self) -> Optional[set[str]]:
        try:
            return set(self._service.registry.schemas().keys())
        except Exception:  # noqa: BLE001
            return None

    def _assert_valid(self, definition: AutomationDefinition) -> None:
        issues = self.validate(definition)
        if not is_valid(issues):
            first = next(i for i in issues if i.severity == "error")
            raise AutomationError("INVALID_AUTOMATION", first.message, issues=issues)

    # ── CRUD ─────────────────────────────────────────────────────────────────
    def create(self, payload: dict[str, Any], *, created_by: Optional[str] = None) -> AutomationDefinition:
        now = now_iso()
        data = dict(payload)
        data.setdefault("id", new_automation_id())
        data.setdefault("version", 1)
        data.setdefault("state", "draft")
        data.setdefault("created_at", now)
        data.setdefault("updated_at", now)
        data.setdefault("trigger", {})
        data.setdefault("action", {})
        try:
            definition = AutomationDefinition(**data)
        except Exception as exc:  # noqa: BLE001 - a malformed body is a domain error
            raise AutomationError("INVALID_AUTOMATION", str(exc)) from exc
        definition = self._with_webhook_secret(definition)
        self._assert_valid(definition)
        saved = self._store.save_automation(definition, created_by=created_by)
        self._store.record_audit(
            saved.id, "created", actor=created_by,
            detail={"name": saved.name, "state": saved.state,
                    "triggerType": saved.trigger.type, "actionType": saved.action.type},
        )
        if saved.state == "active":
            self._engine.refresh_schedule(saved.id)
            saved = self._store.get_automation(saved.id) or saved
        return saved

    @staticmethod
    def _with_webhook_secret(definition: AutomationDefinition) -> AutomationDefinition:
        """A webhook bot always has its own generated bearer token.

        Never derived from the id: an id is visible, a token is not. Existing
        secrets are preserved (so an edit cannot silently rotate a caller's URL).
        """
        trigger = definition.trigger
        if trigger.type != "webhook" or trigger.webhook_secret:
            return definition
        return definition.model_copy(update={
            "trigger": trigger.model_copy(update={"webhook_secret": secrets.token_urlsafe(32)}),
        })

    def get(self, automation_id: str) -> Optional[AutomationDefinition]:
        return self._store.get_automation(automation_id)

    def audit(self, automation_id: Optional[str] = None, limit: int = 200) -> list[dict[str, Any]]:
        return self._store.list_audit(automation_id, limit=limit)

    def create_definition(
        self, definition: AutomationDefinition, *, created_by: Optional[str] = None,
    ) -> AutomationDefinition:
        """Create from an already-built definition (legacy adapter / migration)."""
        self._assert_valid(definition)
        saved = self._store.save_automation(definition, created_by=created_by)
        self._store.record_audit(
            saved.id, "created", actor=created_by,
            detail={"name": saved.name, "state": saved.state,
                    "triggerType": saved.trigger.type, "actionType": saved.action.type},
        )
        if saved.state == "active":
            self._engine.refresh_schedule(saved.id)
            saved = self._store.get_automation(saved.id) or saved
        return saved

    def list(self, *, limit: int = 200, scope_id: Optional[str] = None, state: Optional[str] = None) -> list[AutomationDefinition]:
        return self._store.list_automations(limit=limit, scope_id=scope_id, state=state)

    # ── Proactive monitors (§ Phase 3) ────────────────────────────────────────
    def list_monitors(self) -> list[dict[str, Any]]:
        """The closed catalogue of portfolio monitors a user may arm."""
        from ..advisory.monitors import list_monitors  # noqa: PLC0415

        return list_monitors()

    def create_monitor(
        self, monitor_id: str, params: dict[str, Any], *,
        name: Optional[str] = None, objective: Optional[str] = None,
        created_by: Optional[str] = None,
    ) -> AutomationDefinition:
        """Arm a monitor from the catalogue. Saved OFF; the operator turns it on."""
        from ..advisory.monitors import build_definition  # noqa: PLC0415

        try:
            definition = build_definition(monitor_id, params or {}, name=name, objective=objective)
        except ValueError as exc:
            raise AutomationError("INVALID_MONITOR", str(exc)) from exc
        return self.create_definition(definition, created_by=created_by)

    def update(
        self, automation_id: str, patch: dict[str, Any], *, updated_by: Optional[str] = None,
    ) -> AutomationDefinition:
        current = self._store.get_automation(automation_id)
        if current is None:
            raise AutomationError("NOT_FOUND", "automation not found")
        fields: dict[str, Any] = {}
        for key in _PATCHABLE:
            value = _patched(patch, key)
            if value is not None:
                fields[key] = value
        if not fields:
            return current
        # Activating / pausing a prepared bot is NOT a semantic edit: a version
        # bump would immediately invalidate the mandate pinned in `prepare`,
        # leaving every freshly activated bot stale before its first run.
        semantic = any(key != "state" for key in fields)
        version = current.version + 1 if semantic else current.version
        candidate = current.model_copy(update={**fields, "version": version, "updated_at": now_iso()})
        # Re-validate the typed sub-models (a malformed dict must be refused).
        candidate = self._retype(candidate, fields)
        candidate = self._with_webhook_secret(candidate)
        self._assert_valid(candidate)
        saved = self._store.save_automation(candidate, created_by=updated_by)
        self._store.record_audit(
            saved.id, "updated", actor=updated_by,
            detail={"version": saved.version, "fields": sorted(fields.keys())},
        )
        self._engine.refresh_schedule(saved.id)
        return self._store.get_automation(saved.id) or saved

    def attach_trading(
        self, automation_id: str, *, trial_id: str, consent: Optional[dict[str, Any]],
        config_patch: Optional[dict[str, Any]] = None,
    ) -> AutomationDefinition:
        """Record a prepared trial + the operator's standing consent.

        This is NOT a semantic edit: the strategy, authority and schedule are
        unchanged, so the definition version must not bump (a bump would
        immediately invalidate the mandate we just created). It still validates,
        so an incomplete setup is refused rather than saved half-prepared.
        """
        current = self._store.get_automation(automation_id)
        if current is None:
            raise AutomationError("NOT_FOUND", "automation not found")
        config = dict(current.action.config or {})
        config["trialId"] = trial_id
        if consent is not None:
            config["consent"] = consent
        if config_patch:
            config.update(config_patch)
        action = current.action.model_copy(update={"config": config})
        candidate = current.model_copy(update={"action": action, "updated_at": now_iso()})
        candidate = self._retype(candidate, {"action": action})
        self._assert_valid(candidate)
        saved = self._store.save_automation(candidate, snapshot=False)
        return self._store.get_automation(saved.id) or saved

    def _retype(self, candidate: AutomationDefinition, fields: dict[str, Any]) -> AutomationDefinition:
        try:
            model = AutomationDefinition(**candidate.model_dump(by_alias=False))
        except Exception as exc:  # noqa: BLE001
            raise AutomationError("INVALID_AUTOMATION", str(exc)) from exc
        return model

    def delete(self, automation_id: str) -> bool:
        # Revoke trading authority BEFORE the row disappears: a deleted bot must
        # never leave a usable mandate behind. Positions are untouched.
        self._stop_trading(automation_id, "bot deleted")
        deleted = self._store.delete_automation(automation_id)
        if deleted:
            self._store.record_audit(
                automation_id, "deleted", actor="operator",
                detail={"note": "run history retained for audit"},
            )
        return deleted

    def _stop_trading(self, automation_id: str, reason: str) -> dict[str, Any]:
        """Stop a strategy bot's authority. Best-effort; never fails a lifecycle call."""
        result: dict[str, Any] = {"revoked": 0, "trials": 0, "reason": reason}
        try:
            definition = self._store.get_automation(automation_id)
        except Exception:  # noqa: BLE001
            definition = None
        if definition is None:
            return result
        if getattr(definition.action, "type", "") != "run_strategy" and not (
            isinstance(definition.action.config, dict)
            and definition.action.config.get("consent")
        ):
            return result
        brokers = getattr(self._service, "brokers", None)
        if brokers is not None:
            try:
                result["revoked"] = len(brokers.revoke_mandates_for_bot(automation_id))
            except Exception:  # noqa: BLE001
                pass
        store = getattr(self._service, "quant_store", None)
        if store is not None:
            try:
                from ..quant.live import LiveStrategyService  # noqa: PLC0415

                result["trials"] = len(LiveStrategyService(store).stop_by_bot(automation_id))
            except Exception:  # noqa: BLE001
                pass
        return result

    # ── Lifecycle ────────────────────────────────────────────────────────────
    def set_state(self, automation_id: str, state: str, *, actor: str = "operator") -> AutomationDefinition:
        current = self._store.get_automation(automation_id)
        if current is None:
            raise AutomationError("NOT_FOUND", "automation not found")
        try:
            assert_lifecycle_transition(current.state, state)
        except TransitionError as exc:
            raise AutomationError(exc.code, exc.message) from exc
        if state == "active":
            self._assert_valid(current)
        updated = current.model_copy(update={"state": state, "updated_at": now_iso()})
        saved = self._store.save_automation(updated, snapshot=False)
        if state in ("paused", "disabled", "archived"):
            # Pause/disable/archive is an operator decision to stop trading now.
            self._stop_trading(automation_id, f"bot {state}")
        self._engine.refresh_schedule(saved.id)
        self._store.record_audit(
            saved.id, "lifecycle", actor=actor,
            detail={"from": current.state, "to": state},
        )
        return self._store.get_automation(saved.id) or saved

    def enable(self, automation_id: str, *, actor: str = "operator") -> AutomationDefinition:
        return self.set_state(automation_id, "active", actor=actor)

    def disable(self, automation_id: str, *, actor: str = "operator") -> AutomationDefinition:
        return self.set_state(automation_id, "disabled", actor=actor)

    def pause(self, automation_id: str, *, actor: str = "operator") -> AutomationDefinition:
        """Pause prevents future triggers. It does NOT kill an active execution."""
        return self.set_state(automation_id, "paused", actor=actor)

    def resume(self, automation_id: str, *, actor: str = "operator") -> AutomationDefinition:
        return self.set_state(automation_id, "active", actor=actor)

    def archive(self, automation_id: str, *, actor: str = "operator") -> AutomationDefinition:
        return self.set_state(automation_id, "archived", actor=actor)

    # ── Execution ────────────────────────────────────────────────────────────
    async def run_now(
        self, automation_id: str, *, trigger_ref: Optional[str] = None,
        allow_paused: bool = False,
    ) -> Optional[str]:
        execution_id = await self._engine.fire(
            automation_id, trigger_type="manual", trigger_ref=trigger_ref,
            allow_paused=allow_paused,
        )
        if execution_id:
            self._store.record_audit(
                automation_id, "run", actor="operator",
                detail={"executionId": execution_id, "allowPaused": allow_paused},
            )
        return execution_id

    async def run_webhook(
        self, automation_id: str, *, delivery_id: Optional[str] = None,
    ) -> Optional[str]:
        """Fire an active webhook bot. The caller already verified its token."""
        return await self._engine.fire(
            automation_id, trigger_type="webhook", trigger_ref=delivery_id,
        )

    async def resume_execution(self, execution_id: str) -> bool:
        """Resume a waiting execution once its approval has been granted."""
        return await self._engine.resume_execution(execution_id)

    async def dry_run(self, automation_id: str) -> Optional[dict[str, Any]]:
        return await self._engine.dry_run(automation_id)

    async def test(self, automation_id: str) -> Optional[dict[str, Any]]:
        definition = self._store.get_automation(automation_id)
        if definition is None:
            return None
        issues = self.validate(definition)
        return {
            "automationId": automation_id,
            "valid": is_valid(issues),
            "issues": [i.to_dict() for i in issues],
            "plannedActions": await self._engine.dry_run(automation_id),
        }

    def cancel_execution(self, execution_id: str) -> bool:
        execution = self._store.get_execution(execution_id)
        if execution is None or execution["state"] in ("completed", "failed", "cancelled", "timed_out", "skipped", "blocked"):
            return False
        task = self._engine._active.get(execution_id)  # noqa: SLF001 - cooperative cancel
        if task is not None:
            task.cancel()
        self._store.finish_execution(
            execution_id, state="cancelled", outcome_reason="Stopped by an operator."
        )
        self._store.append_execution_event(execution_id, "cancelled", {"reason": "operator"})
        return True

    def reset_breaker(self, automation_id: str, *, reset_by: str = "operator") -> Optional[AutomationDefinition]:
        return self._engine.reset_breaker(automation_id, reset_by=reset_by)

    # ── History / observability ──────────────────────────────────────────────
    def history(self, automation_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        return self._store.list_executions(automation_id, limit=limit)

    def get_execution(self, execution_id: str) -> Optional[dict[str, Any]]:
        execution = self._store.get_execution(execution_id)
        if execution is None:
            return None
        execution["events"] = self._store.list_execution_events(execution_id)
        execution["deliveries"] = self._store.list_deliveries(execution_id)
        return execution

    def timeline(self, execution_id: str) -> list[dict[str, Any]]:
        return self._store.list_execution_events(execution_id)

    def versions(self, automation_id: str) -> list[dict[str, Any]]:
        return self._store.list_versions(automation_id)

    def version(self, automation_id: str, version: int) -> Optional[dict[str, Any]]:
        return self._store.get_version(automation_id, version)

    def status(self, automation_id: str) -> Optional[dict[str, Any]]:
        definition = self._store.get_automation(automation_id)
        if definition is None:
            return None
        executions = self._store.list_executions(automation_id, limit=200)
        completed = [e for e in executions if e["state"] == "completed"]
        failed = [e for e in executions if e["state"] == "failed"]
        durations = []
        for execution in executions:
            start = execution.get("startedAt")
            end = execution.get("finishedAt")
            if start and end:
                from .timeutil import parse_iso  # noqa: PLC0415

                s, e = parse_iso(start), parse_iso(end)
                if s and e:
                    durations.append((e - s).total_seconds())
        total_cost = sum(e["cost"] or 0.0 for e in executions)
        total_tokens = sum(e["tokens"] or 0 for e in executions)
        finished = len(completed) + len(failed)
        return {
            "automationId": automation_id,
            "state": definition.state,
            "version": definition.version,
            "nextRunAt": definition.next_run_at,
            "lastRunAt": definition.last_run_at,
            "breaker": definition.breaker,
            "executionCount": len(executions),
            "successCount": len(completed),
            "failureCount": len(failed),
            "successRate": (len(completed) / finished) if finished else None,
            "averageDurationS": (sum(durations) / len(durations)) if durations else None,
            "totalCost": total_cost if total_cost else None,
            "totalTokens": total_tokens if total_tokens else None,
            "lastExecution": self._store.last_execution(automation_id),
        }

    def statuses(self, *, ids: Optional[list[str]] = None) -> dict[str, dict[str, Any]]:
        """Lightweight status for every automation, in two queries total.

        The Bots sidebar only renders the latest run (unread dot + subtitle), so
        it must not call the full `status()` per bot: that fans out one HTTP
        request and a 200-row execution scan per bot. Here the definitions and
        the latest executions come back together; full counters stay available
        from the per-bot `/status` route used by the Details panel.
        """
        definitions = self._store.list_automations(limit=1000)
        wanted = set(ids) if ids else None
        if wanted is not None:
            definitions = [d for d in definitions if d.id in wanted]
        latest = self._store.last_executions()
        return {
            d.id: {
                "automationId": d.id,
                "state": d.state,
                "version": d.version,
                "nextRunAt": d.next_run_at,
                "lastRunAt": d.last_run_at,
                "breaker": d.breaker,
                "lastExecution": latest.get(d.id),
            }
            for d in definitions
        }

    def metrics(self) -> dict[str, Any]:
        return self._engine.metrics()


__all__ = ["AutomationError", "AutomationService"]
