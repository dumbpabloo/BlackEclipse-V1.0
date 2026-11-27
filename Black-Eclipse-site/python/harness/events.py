"""Harness event emission — the Python runtime owns the event model.

Events are assigned a monotonic per-run sequence and timestamp here, persisted
to the agent-memory database, then published on an in-process bus for live
subscribers. The TS side is no longer involved in event creation.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Callable, Optional

from .config.flags import get_bool
from .execution_log import ExecutionEventLog
from .persistence import AgentMemoryStore, now_iso
from .run_activity import forget as forget_activity
from .run_activity import touch as touch_activity
from .run_timings import RunTimings, TERMINAL_EVENT_TYPES

log = logging.getLogger("harness.events")

Listener = Callable[[dict[str, Any]], None]


class EventBus:
    def __init__(self) -> None:
        self._listeners: list[Listener] = []
        # Live per-run queues for SSE consumers. A queue (not a callback) so a
        # slow HTTP client can never stall the agent loop.
        self._streams: dict[str, list[asyncio.Queue[Optional[dict[str, Any]]]]] = {}

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def publish(self, event: dict[str, Any]) -> None:
        run_id = event.get("runId")
        if run_id:
            for queue in list(self._streams.get(run_id, [])):
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    pass  # a slow client drops live deltas; the run is unaffected
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:  # noqa: BLE001 - a broken subscriber must not break a run
                log.warning("event subscriber failed", exc_info=True)

    def open_stream(self, run_id: str) -> asyncio.Queue[Optional[dict[str, Any]]]:
        """Register a live queue for one run. Caller must `close_stream`."""
        queue: asyncio.Queue[Optional[dict[str, Any]]] = asyncio.Queue(maxsize=1000)
        self._streams.setdefault(run_id, []).append(queue)
        return queue

    def close_stream(self, run_id: str, queue: asyncio.Queue[Optional[dict[str, Any]]]) -> None:
        queues = self._streams.get(run_id)
        if not queues:
            return
        if queue in queues:
            queues.remove(queue)
        if not queues:
            self._streams.pop(run_id, None)

    def end_stream(self, run_id: str) -> None:
        """Signal all live consumers that a run is finished (sentinel None).

        The sentinel must never be dropped: it is the only signal that closes a
        live SSE stream. If a consumer's queue is full (a slow client that has
        already buffered the maximum), make room by discarding the OLDEST live
        event — token deltas are best-effort and the durable trace is replayed
        on reconnect — then enqueue the sentinel.
        """
        for queue in list(self._streams.get(run_id, [])):
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(None)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass


class RunEmitter:
    """Per-run emitter: sequence numbers, persistence, bus fan-out."""

    def __init__(self, run_id: str, store: AgentMemoryStore, bus: Optional[EventBus] = None) -> None:
        self.run_id = run_id
        self._store = store
        self._bus = bus
        self._seq = 0
        # Latency marks for this run. Fed by every event this emitter sees and
        # written ONCE as `run.timings` on the terminal event, so the trace gains
        # one row per run instead of one per token.
        self._clock = time.monotonic
        self._timings = RunTimings(self._clock)
        self._timings_emitted = False
        # Round 5 / C2: the run projection and the canonical log are written
        # together. The log never owns run state; `HarnessEvent` stays
        # authoritative for the run reader.
        log = getattr(store, "_execution_log", None)
        if log is None:
            db = getattr(store, "_db", None)
            if db is not None:
                log = ExecutionEventLog(db)
                try:
                    setattr(store, "_execution_log", log)
                except Exception:  # noqa: BLE001 - a read-only store stub
                    pass
        self._log = log

    async def emit(self, event_type: str, data: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        touch_activity(self.run_id)
        event = self._durable_event(event_type, data)
        if self._bus:
            self._bus.publish(event)
        if self._log is not None:
            try:
                self._log.append("run", self.run_id, event_type, event["data"])
            except Exception:  # noqa: BLE001 - the canonical log is fail-open
                pass
        self._timings.observe(event_type, event["data"], at=self._clock())
        if event_type in TERMINAL_EVENT_TYPES and not self._timings_emitted:
            # One durable latency record per run, written AFTER the terminal event
            # so a reader always sees the run's outcome first. Fail-open: the
            # summary is diagnostics and can never fail a finished run.
            self._timings_emitted = True
            forget_activity(self.run_id)
            try:
                await self.emit("run.timings", self._timings.summary(at=self._clock()))
            except Exception:  # noqa: BLE001
                pass
        return event

    def emit_sync(self, event_type: str, data: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Persist + publish an event from a SYNC seam (no await).

        Used for a low-frequency advisory signal that originates in synchronous
        registry code (e.g. a one-per-run deprecation warning). It shares the same
        sequence/persistence/bus path as `emit`; a persistence failure is
        swallowed (an advisory must never break a tool call).
        """
        touch_activity(self.run_id)
        event = self._durable_event(event_type, data)
        if self._bus:
            self._bus.publish(event)
        if self._log is not None:
            try:
                self._log.append("run", self.run_id, event_type, event["data"])
            except Exception:  # noqa: BLE001 - the canonical log is fail-open
                pass
        self._timings.observe(event_type, event["data"], at=self._clock())
        return event

    def _durable_event(
        self, event_type: str, data: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Allocate the durable sequence (W6) or fall back to the local counter.

        The store allocator runs inside `BEGIN IMMEDIATE`, so two emitters for
        one run cannot allocate the same seq. When `HARNESS_EVENT_SEQ=off` the
        legacy per-emitter behaviour is restored exactly.
        """
        if get_bool("HARNESS_EVENT_SEQ"):
            from .event_sequence import get_event_sequence  # noqa: PLC0415

            sequence = get_event_sequence(self._store)
            if sequence is not None:
                try:
                    return sequence.append(self.run_id, event_type, data)
                except Exception:  # noqa: BLE001 - losing an event must not kill the run
                    log.error("failed to persist sequenced event %s", event_type, exc_info=True)
        self._seq += 1
        event = {
            "id": f"evt_{self.run_id}_{self._seq}",
            "runId": self.run_id,
            "type": event_type,
            "seq": self._seq,
            "at": now_iso(),
            "data": data or {},
        }
        try:
            self._store.append_event(self.run_id, event_type, event["seq"], event["data"])
        except Exception:  # noqa: BLE001 - losing an event must not kill the run
            log.error("failed to persist event %s", event_type, exc_info=True)
        return event

    def emit_transient(self, event_type: str, data: Optional[dict[str, Any]] = None) -> None:
        """Publish a live-only event (no persistence, no sequence).

        Token deltas are high-frequency and worthless after the run ends; writing
        every one to SQLite would bloat the trace and slow the run. They ride the
        bus to live subscribers only — the final message is still persisted by
        `model.completed`.
        """
        touch_activity(self.run_id)
        self._seq += 1
        event = {
            # A transient event is never persisted, so it lives outside the
            # durable `(runId, seq)` space: a unique id and a NEGATIVE seq avoid
            # colliding with (or being mistaken for) a durable event.
            "id": f"evt_{self.run_id}_t_{uuid.uuid4().hex[:12]}",
            "runId": self.run_id,
            "type": event_type,
            "seq": -self._seq,
            "at": now_iso(),
            "data": data or {},
            "transient": True,
        }
        # Token deltas are where cadence is measured: the gap between them is the
        # difference between a smooth stream and a bursty one.
        self._timings.observe(event_type, event["data"], at=self._clock())
        if self._bus:
            self._bus.publish(event)


__all__ = ["EventBus", "RunEmitter"]
