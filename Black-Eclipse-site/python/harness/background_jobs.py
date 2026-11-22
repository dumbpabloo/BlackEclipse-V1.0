"""Background-process completion notices.

A coding agent starts a long command (`shell_spawn`), does other work, and needs
to learn when the command finished. Polling `process_output` is the failure mode
this module removes: every poll is a model step that carries no new information,
and a busy model can spend its whole step budget checking a log.

The contract:

  * `watch_process(run_id, handle)` starts one daemon thread per spawned
    process. It waits for the process to exit, then records a bounded, redacted
    notice for that run.
  * `inject_into_state(state, run_id)` is called at the graph's ONE step
    boundary. It appends each notice once as a `HumanMessage` carrying
    `<background-result kind="process" ...>`, and records the key in
    `w7_pending` so a durable resume does not repeat it.
  * `drain_messages(run_id)` is the same queue without graph state, for tests
    and for any other loop.

Nothing here starts, stops or inspects a process beyond the handle it was given.
The notice is DATA: it contains the command and the output tail, and neither is
an instruction. Notices are per run and in memory; a run that has already ended
cannot be resumed by a notice (that needs the durable job store, not this
module).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

#: The output tail a notice carries. The full output stays available through
#: `process_output` / the process handle; the notice is a pointer, not a dump.
MAX_NOTICE_OUTPUT_CHARS = 4_000

#: How many undelivered notices one run may hold. A burst cannot grow without
#: bound; the oldest notices are dropped with a count.
MAX_PENDING_PER_RUN = 20

_PENDING: dict[str, list["Notice"]] = {}
_DROPPED: dict[str, int] = {}
_LOCK = threading.Lock()


@dataclass(frozen=True)
class Notice:
    """One finished background command, bounded and safe to show the model."""

    run_id: str
    pid: str
    command: str
    status: str
    exit_code: Optional[int]
    duration_ms: int
    output_tail: str
    redacted_lines: int

    @property
    def key(self) -> str:
        return f"bg:{self.pid}:{self.status}:{self.exit_code}"


def _tail(text: str) -> str:
    value = str(text or "")
    if len(value) <= MAX_NOTICE_OUTPUT_CHARS:
        return value
    return value[-MAX_NOTICE_OUTPUT_CHARS:]


def _notice(run_id: str, handle: Any, started: float) -> Notice:
    try:
        handle.poll()
    except Exception:  # noqa: BLE001 - a dead handle still yields a notice
        pass
    try:
        stdout, stderr = handle.output()
    except Exception:  # noqa: BLE001
        stdout, stderr = "", ""
    combined = (stdout or "") + (("\nSTDERR:\n" + stderr) if stderr else "")
    from .code.events import redact_output  # noqa: PLC0415 - avoids a module cycle

    cleaned, redacted = redact_output(combined)
    status = str(getattr(handle, "status", "") or "exited")
    exit_code = getattr(handle, "exit_code", None)
    if exit_code is not None and status == "running":
        status = "exited"
    return Notice(
        run_id=run_id,
        pid=str(getattr(handle, "pid", "") or ""),
        command=str(getattr(handle, "command", "") or ""),
        status=status,
        exit_code=exit_code if isinstance(exit_code, int) else None,
        duration_ms=max(0, int((time.monotonic() - started) * 1000)),
        output_tail=_tail(cleaned),
        redacted_lines=redacted,
    )


def watch_process(run_id: str, handle: Any) -> Optional[threading.Thread]:
    """Record a notice when `handle` exits. Returns the watcher thread.

    One daemon thread per process, no polling in the agent loop. A blank run id
    is a no-op (nothing to notify).
    """
    run_id = str(run_id or "")
    if not run_id or handle is None:
        return None
    started = time.monotonic()

    def _wait() -> None:
        try:
            handle.proc.wait()
        except Exception:  # noqa: BLE001 - killed/failed still produces a notice
            pass
        notice = _notice(run_id, handle, started)
        with _LOCK:
            queue = _PENDING.setdefault(run_id, [])
            queue.append(notice)
            if len(queue) > MAX_PENDING_PER_RUN:
                overflow = len(queue) - MAX_PENDING_PER_RUN
                del queue[:overflow]
                _DROPPED[run_id] = _DROPPED.get(run_id, 0) + overflow

    thread = threading.Thread(target=_wait, name=f"bg-notice-{run_id[:8]}", daemon=True)
    thread.start()
    return thread


def drain(run_id: str) -> list[Notice]:
    """Pop every notice waiting for `run_id`, oldest first."""
    with _LOCK:
        return list(_PENDING.pop(str(run_id or ""), []))


def pending_count(run_id: str) -> int:
    with _LOCK:
        return len(_PENDING.get(str(run_id or ""), []))


def dropped_count(run_id: str) -> int:
    with _LOCK:
        return _DROPPED.get(str(run_id or ""), 0)


def render_notice(notice: Notice) -> str:
    """The model-facing notice. Fenced, labelled, and explicitly not a poll request."""
    lines = [
        f'<background-result kind="process" pid="{notice.pid}" state="{notice.status}" '
        f'exit="{notice.exit_code if notice.exit_code is not None else "none"}">',
        f"Command: {notice.command}",
        f"Finished in {notice.duration_ms} ms.",
    ]
    if notice.redacted_lines:
        lines.append(f"[{notice.redacted_lines} line(s) redacted for privacy]")
    if notice.output_tail.strip():
        lines.append("Output (tail):")
        lines.append(notice.output_tail.rstrip())
    else:
        lines.append("Output: (none)")
    lines.append(
        "You were notified automatically because this background command finished. "
        "Do not poll process_output for this process, and do not start another copy. "
        "Use this result and continue the task."
    )
    lines.append("</background-result>")
    return "\n".join(lines)


def drain_messages(run_id: str) -> list[tuple[str, Any]]:
    """The pending notices as `(key, HumanMessage)` pairs, once each."""
    from langchain_core.messages import HumanMessage  # noqa: PLC0415

    return [(notice.key, HumanMessage(content=render_notice(notice))) for notice in drain(run_id)]


def inject_into_state(state: dict[str, Any], run_id: str) -> dict[str, Any]:
    """Append undelivered notices to a graph state at its step boundary.

    Respects the state's `w7_pending` ledger, so a resumed run does not receive
    the same notice twice. Returns the same state object when there is nothing
    new (the graph's cheap path).
    """
    entries = drain_messages(run_id)
    if not entries:
        return state
    applied = {
        str(item.get("key"))
        for item in (state.get("w7_pending") or [])
        if isinstance(item, dict)
    }
    fresh = [(key, message) for key, message in entries if key not in applied]
    if not fresh:
        return state
    return {
        **state,
        "messages": [*(state.get("messages") or []), *[message for _key, message in fresh]],
        "w7_pending": [
            *(state.get("w7_pending") or []),
            *[{"key": key, "kind": "background"} for key, _message in fresh],
        ],
    }


def clear(run_id: str) -> None:
    """Drop everything queued for a run (used when a run ends)."""
    with _LOCK:
        _PENDING.pop(str(run_id or ""), None)
        _DROPPED.pop(str(run_id or ""), None)


__all__ = [
    "MAX_NOTICE_OUTPUT_CHARS",
    "MAX_PENDING_PER_RUN",
    "Notice",
    "clear",
    "drain",
    "drain_messages",
    "dropped_count",
    "inject_into_state",
    "pending_count",
    "render_notice",
    "watch_process",
]
