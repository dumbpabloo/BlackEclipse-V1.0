"""Admission waits (`model.queued`) reach the run trace, not just observability."""

from __future__ import annotations

from harness.model_runtime.run_events import bind_run_emitter, emit_model_event


class _Emitter:
    def __init__(self) -> None:
        self.durable: list[tuple[str, dict]] = []

    def emit_sync(self, event_type, data):  # noqa: ANN001
        self.durable.append((event_type, data))
        return {"type": event_type, "data": data}


def test_model_queued_is_persisted_for_the_bound_run():
    emitter = _Emitter()
    seen: list[str] = []
    with bind_run_emitter(emitter):
        emit_model_event(lambda t, _d: seen.append(t), "model.queued", {"reason": "concurrency"})
    assert [name for name, _ in emitter.durable] == ["model.queued"]
    assert emitter.durable[0][1]["reason"] == "concurrency"
    assert seen == ["model.queued"]


def test_high_frequency_attempt_events_are_not_duplicated():
    emitter = _Emitter()
    with bind_run_emitter(emitter):
        emit_model_event(lambda *_a: None, "model.attempt.failed", {"attempt": 1})
        emit_model_event(lambda *_a: None, "model.rate_limited", {"reason": "rate_limit"})
    assert [name for name, _ in emitter.durable] == ["model.rate_limited"]


def test_events_outside_a_run_binding_stay_observability_only():
    emitter = _Emitter()
    emit_model_event(lambda *_a: None, "model.queued", {"reason": "concurrency"})
    assert emitter.durable == []
