"""Typed event dispatch with the five Cordis modes.

The mode is part of an event's contract, not the caller's choice:

* ``emit``      - observe, no return value, registration order.
* ``waterfall`` - around-middleware; a listener must call ``next()`` to delegate.
* ``serial``    - awaited, registration order, first non-None result wins.
* ``bail``      - first non-None result wins, remaining listeners skipped.
* ``parallel``  - awaited, all listeners concurrently, no return value.

Handlers may be sync or async. A handler registered through a context is removed
automatically when that context is disposed, so event wiring is reversible.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any, Callable

from .errors import KernelError

MODES = ("emit", "waterfall", "serial", "bail", "parallel")


@dataclass
class _Handler:
    event: str
    fn: Callable[..., Any]
    mode: str
    order: int
    owner: Any = None
    label: str = ""
    sequence: int = 0


@dataclass
class EventBus:
    """One dispatcher per kernel. Contexts register through it as effects."""

    _handlers: dict[str, list[_Handler]] = field(default_factory=dict)
    _sequence: int = 0

    # -- registration ---------------------------------------------------------

    def on(
        self,
        event: str,
        handler: Callable[..., Any],
        *,
        mode: str = "emit",
        order: int = 100,
        prepend: bool = False,
        owner: Any = None,
        label: str = "",
    ) -> Callable[[], None]:
        if mode not in MODES:
            raise KernelError("KERNEL_EVENT_MODE", f"unknown event mode {mode!r}")
        if not callable(handler):
            raise KernelError("KERNEL_EVENT_HANDLER", "event handler must be callable")
        self._sequence += 1
        item = _Handler(event, handler, mode, -order if prepend else order, owner, label, self._sequence)
        bucket = self._handlers.setdefault(event, [])
        bucket.append(item)
        bucket.sort(key=lambda h: (h.order, h.sequence))

        def dispose() -> None:
            current = self._handlers.get(event)
            if current and item in current:
                current.remove(item)
                if not current:
                    self._handlers.pop(event, None)

        return dispose

    def clear_owner(self, owner: Any) -> None:
        for event, handlers in list(self._handlers.items()):
            kept = [h for h in handlers if h.owner is not owner]
            if kept:
                self._handlers[event] = kept
            else:
                self._handlers.pop(event, None)

    def listeners(self, event: str) -> list[dict[str, Any]]:
        return [
            {"event": h.event, "mode": h.mode, "order": -h.order, "label": h.label}
            for h in self._handlers.get(event, [])
        ]

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {"event": event, "handlers": self.listeners(event)}
            for event in sorted(self._handlers)
        ]

    # -- dispatch -------------------------------------------------------------

    async def emit(self, event: str, *args: Any) -> None:
        for handler in list(self._handlers.get(event, [])):
            await _invoke(handler.fn, *args)

    async def waterfall(self, event: str, *args: Any, fallback: Any = None) -> Any:
        handlers = list(self._handlers.get(event, []))
        index = 0

        async def next_call() -> Any:
            nonlocal index
            if index >= len(handlers):
                return fallback
            handler = handlers[index]
            index += 1
            result = handler.fn(*args, next_call)
            if inspect.isawaitable(result):
                return await result
            return result

        return await next_call()

    async def serial(self, event: str, *args: Any) -> Any:
        for handler in list(self._handlers.get(event, [])):
            result = await _invoke(handler.fn, *args)
            if result is not None:
                return result
        return None

    async def bail(self, event: str, *args: Any) -> Any:
        for handler in list(self._handlers.get(event, [])):
            result = await _invoke(handler.fn, *args)
            if result is not None:
                return result
        return None

    async def parallel(self, event: str, *args: Any) -> None:
        handlers = list(self._handlers.get(event, []))
        if handlers:
            await asyncio.gather(*(_invoke(h.fn, *args) for h in handlers))


async def _invoke(fn: Callable[..., Any], *args: Any) -> Any:
    result = fn(*args)
    if inspect.isawaitable(result):
        return await result
    return result
