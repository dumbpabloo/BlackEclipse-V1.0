"""Plugin hooks around Eclipse's lifecycle points.

Hooks are the one place a plugin may observe or influence a core operation. They
run in a deterministic order (by `order`, then plugin id), each with its own
timeout, and each declares a failure policy:

  * `continue` — a failing hook is logged and skipped; the operation proceeds.
  * `abort`    — a failing hook vetoes the operation (the host decides how).

A single plugin can never block the whole system indefinitely: every handler is
bounded by its timeout, and the whole hook phase is bounded by a caller-supplied
budget. Cancellation is cooperative via an optional `cancel` event.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .errors import PluginError

# The canonical hook points the runtime emits. A plugin may only declare a hook
# in this set; a typo is rejected at registration, never silently ignored.
HOOK_POINTS: frozenset[str] = frozenset({
    "before_agent_run", "after_agent_run",
    "before_tool_call", "after_tool_call",
    "before_task_create", "after_task_create",
    "before_task_complete", "after_task_complete",
    "before_workflow_execute", "after_workflow_execute",
    "before_research", "after_research",
    "before_memory_write", "after_memory_write",
})


class HookError(PluginError):
    def __init__(self, code: str, message: str, **kwargs: Any) -> None:
        super().__init__(code, message, **kwargs)


@dataclass
class _Hook:
    plugin_id: str
    name: str
    handler: Callable[..., Any]
    order: int = 100
    on_error: str = "continue"
    timeout_ms: int = 5_000


@dataclass
class HookOutcome:
    name: str
    proceed: bool
    data: dict[str, Any]
    ran: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "proceed": self.proceed, "ran": self.ran, "errors": self.errors}


class HookRegistry:
    def __init__(self, on_failure: Optional[Callable[[str, str, str], None]] = None) -> None:
        self._hooks: dict[str, list[_Hook]] = {}
        self._on_failure = on_failure

    def register(
        self, plugin_id: str, name: str, handler: Callable[..., Any], *,
        order: int = 100, on_error: str = "continue", timeout_ms: int = 5_000,
    ) -> None:
        if name not in HOOK_POINTS:
            raise HookError("PLUGIN_HOOK_UNKNOWN", f"unknown hook point {name!r}")
        if on_error not in ("continue", "abort"):
            raise HookError("PLUGIN_HOOK_POLICY", f"invalid on_error policy {on_error!r}")
        self._hooks.setdefault(name, []).append(
            _Hook(plugin_id, name, handler, order, on_error, timeout_ms)
        )
        self._hooks[name].sort(key=lambda h: (h.order, h.plugin_id))

    def unregister_plugin(self, plugin_id: str) -> None:
        for name in list(self._hooks):
            self._hooks[name] = [h for h in self._hooks[name] if h.plugin_id != plugin_id]
            if not self._hooks[name]:
                del self._hooks[name]

    def unregister(self, plugin_id: str, name: str) -> bool:
        hooks = self._hooks.get(name, [])
        kept = [hook for hook in hooks if hook.plugin_id != plugin_id]
        removed = len(kept) != len(hooks)
        if kept:
            self._hooks[name] = kept
        elif name in self._hooks:
            del self._hooks[name]
        return removed

    def registered(self, plugin_id: Optional[str] = None) -> list[dict[str, Any]]:
        out = []
        for name, hooks in self._hooks.items():
            for h in hooks:
                if plugin_id is None or h.plugin_id == plugin_id:
                    out.append({"pluginId": h.plugin_id, "name": name, "order": h.order, "onError": h.on_error})
        return out

    def has(self, name: str) -> bool:
        return bool(self._hooks.get(name))

    async def run(
        self, name: str, payload: Optional[dict[str, Any]] = None, *,
        context_for: Optional[Callable[[str], Any]] = None,
        budget_ms: int = 10_000,
        cancel: Optional[asyncio.Event] = None,
    ) -> HookOutcome:
        data = dict(payload or {})
        hooks = self._hooks.get(name, [])
        outcome = HookOutcome(name=name, proceed=True, data=data)
        if not hooks:
            return outcome
        deadline = asyncio.get_event_loop().time() + (budget_ms / 1000.0)
        for hook in hooks:
            if cancel is not None and cancel.is_set():
                outcome.proceed = False
                outcome.errors.append({"pluginId": hook.plugin_id, "error": "cancelled"})
                return outcome
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                outcome.errors.append({"pluginId": hook.plugin_id, "error": "hook budget exhausted"})
                break
            ctx = None
            if context_for is not None:
                try:
                    ctx = context_for(hook.plugin_id)
                except Exception:  # noqa: BLE001
                    ctx = None
            try:
                result = hook.handler(data, ctx)
                if asyncio.iscoroutine(result):
                    result = await asyncio.wait_for(
                        result, timeout=min(remaining, hook.timeout_ms / 1000.0)
                    )
                if isinstance(result, dict):
                    data = result
                outcome.ran += 1
            except asyncio.TimeoutError:
                self._record(outcome, hook, "timeout")
            except Exception as exc:  # noqa: BLE001 - a hook failure is data
                self._record(outcome, hook, f"{type(exc).__name__}: {exc}")
        outcome.data = data
        return outcome

    def _record(self, outcome: HookOutcome, hook: _Hook, error: str) -> None:
        outcome.errors.append({"pluginId": hook.plugin_id, "error": error})
        if self._on_failure is not None:
            try:
                self._on_failure(hook.plugin_id, hook.name, error)
            except Exception:  # noqa: BLE001
                pass
        if hook.on_error == "abort":
            outcome.proceed = False


__all__ = ["HookRegistry", "HookOutcome", "HookError", "HOOK_POINTS"]
