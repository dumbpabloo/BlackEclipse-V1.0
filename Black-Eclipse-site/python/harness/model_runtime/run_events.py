"""Bind the run emitter to the model runtime for durable admission events.

`ModelRuntime` emits `model.queued` / `model.rate_limited` when it waits for a
credential cooldown or a per-key concurrency slot. Those events only reached
process observability, so a run could sit waiting for up to 120 s with no durable
trace — and the UI kept saying "working" without naming the wait.

The gate lives in a ContextVar (not on the runtime singleton) so two concurrent
runs cannot overwrite each other's emitter, mirroring `run_binding`.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Optional

_EMITTER: ContextVar[Optional[Any]] = ContextVar("model_run_emitter", default=None)

#: Events worth persisting for the run trace. High-frequency attempt telemetry
#: already has a durable path in `ModelGateway.stream`; persisting it here too
#: would double every row.
DURABLE_RUN_EVENTS = frozenset({"model.queued", "model.rate_limited"})


@contextmanager
def bind_run_emitter(emitter: Any) -> Iterator[None]:
    """Route durable model-admission events to this run's emitter."""
    token = _EMITTER.set(emitter if emitter is not None else None)
    try:
        yield
    finally:
        _EMITTER.reset(token)


def current_run_emitter() -> Optional[Any]:
    return _EMITTER.get()


def emit_model_event(observer: Any, event_type: str, data: dict[str, Any]) -> None:
    """Send to process observability, and persist admission waits for the run."""
    if observer is not None:
        try:
            observer(event_type, data)
        except Exception:  # noqa: BLE001 - observability must never break a run
            pass
    if event_type not in DURABLE_RUN_EVENTS:
        return
    emitter = current_run_emitter()
    if emitter is None:
        return
    try:
        emitter.emit_sync(event_type, data)
    except Exception:  # noqa: BLE001 - telemetry must never break a run
        pass


__all__ = [
    "DURABLE_RUN_EVENTS",
    "bind_run_emitter",
    "current_run_emitter",
    "emit_model_event",
]
