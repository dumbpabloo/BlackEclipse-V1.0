"""Gap #4: a finished background command reaches the model without polling.

The failure this file gates: `shell_spawn` returned a pid and the model had to
call `process_output` in a loop. Every poll was a model step that carried no new
information, and a busy run could spend its budget checking a log.

The rules asserted here:

1. A spawned process is WATCHED; on exit its notice is queued for that run.
2. The notice is delivered exactly once at the step boundary, as a
   `<background-result>` message, and the message says not to poll.
3. Re-draining adds nothing (the run cannot loop on it).
4. The notice is bounded and redacted, and `shell_spawn` registers it with the
   run id the graph drains.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import HumanMessage

from harness import background_jobs
from harness.code import shell_tools
from harness.tools import ToolContext


class _Proc:
    def __init__(self) -> None:
        self.done = threading.Event()

    def wait(self) -> int:
        self.done.wait(5)
        return 0


class _Handle:
    def __init__(self, output: tuple[str, str] = ("done\n", "")) -> None:
        self.pid = "bg123456"
        self.command = "python -m pytest -q"
        self.status = "running"
        self.exit_code = None
        self.proc = _Proc()
        self._output = output

    def poll(self) -> None:
        if self.proc.done.is_set() and self.status == "running":
            self.status = "exited"
            self.exit_code = 0

    def output(self) -> tuple[str, str]:
        return self._output


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _finished_handle(run_id: str, output: tuple[str, str] = ("done\n", "")) -> _Handle:
    handle = _Handle(output)
    background_jobs.watch_process(run_id, handle)
    handle.proc.done.set()
    assert _wait_for(lambda: background_jobs.pending_count(run_id) == 1)
    return handle


def test_a_finished_process_becomes_one_message_and_stops_the_poll_loop():
    run_id = "run-bg-1"
    background_jobs.clear(run_id)
    try:
        _finished_handle(run_id)

        first = background_jobs.drain_messages(run_id)
        assert len(first) == 1
        _key, message = first[0]
        assert isinstance(message, HumanMessage)
        assert "<background-result" in message.content
        assert "kind=\"process\"" in message.content
        assert 'state="exited"' in message.content
        assert "done" in message.content
        assert "Do not poll" in message.content

        # The run cannot loop on it: draining again yields nothing.
        assert background_jobs.drain_messages(run_id) == []
        assert background_jobs.pending_count(run_id) == 0
    finally:
        background_jobs.clear(run_id)


def test_the_step_boundary_injects_the_notice_once_and_the_model_sees_it():
    run_id = "run-bg-2"
    background_jobs.clear(run_id)
    try:
        _finished_handle(run_id)
        state = {"messages": [HumanMessage(content="start the build")], "w7_pending": []}

        state = background_jobs.inject_into_state(state, run_id)
        assert len(state["messages"]) == 2
        assert "<background-result" in state["messages"][-1].content
        assert state["w7_pending"][-1]["kind"] == "background"

        # The exact list the graph passes to the model.
        captured = {}

        class _Model:
            async def ainvoke(self, messages):
                captured["messages"] = messages
                return SimpleNamespace(content="ok")

        asyncio.run(_Model().ainvoke(state["messages"]))
        assert "<background-result" in captured["messages"][-1].content

        # A resumed/second pass must not duplicate it.
        again = background_jobs.inject_into_state(state, run_id)
        assert len(again["messages"]) == 2
    finally:
        background_jobs.clear(run_id)


def test_the_notice_output_is_bounded_and_redacted():
    run_id = "run-bg-3"
    background_jobs.clear(run_id)
    try:
        secret = "sk-" + "a" * 40
        _finished_handle(run_id, output=("x" * 10_000 + f"\nkey={secret}\n", "err-line\n"))

        notices = background_jobs.drain(run_id)
        assert len(notices) == 1
        notice = notices[0]
        assert len(notice.output_tail) <= background_jobs.MAX_NOTICE_OUTPUT_CHARS
        assert secret not in notice.output_tail
        assert notice.redacted_lines >= 1
        rendered = background_jobs.render_notice(notice)
        assert "redacted" in rendered.lower()
    finally:
        background_jobs.clear(run_id)


def test_shell_spawn_registers_the_watcher_with_the_run_id(monkeypatch, tmp_path):
    handle = _Handle()
    seen = {}

    def _record(run_id, watched):
        seen["run_id"] = run_id
        seen["handle"] = watched

    async def _emit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(shell_tools, "background_jobs", SimpleNamespace(watch_process=_record))
    monkeypatch.setattr(shell_tools, "emit_code_event", _emit)
    monkeypatch.setattr(shell_tools, "_grants_command", lambda _ctx: True)
    monkeypatch.setattr(shell_tools, "_verdict", lambda _cmd: SimpleNamespace(kind="allow", reason="", program="python"))
    monkeypatch.setattr(shell_tools, "_ensure_code_workspace", lambda _ctx: (None, None, None, None))
    monkeypatch.setattr(shell_tools, "_resolve_cwd", lambda _ctx, _cwd: Path(tmp_path))
    monkeypatch.setattr(shell_tools.shell_mod, "spawn", lambda _cmd, cwd: handle)

    ctx = ToolContext(
        run_id="run-bg-4", log=lambda *_: None,
        workspace=SimpleNamespace(root=str(tmp_path)), autonomy="edit_verify",
    )
    text, payload = asyncio.run(shell_tools.shell_spawn({"command": "python -m pytest"}, ctx))
    assert payload["status"] == "running"
    assert "notified automatically" in text
    assert "do not poll" in text.lower()
    assert seen["run_id"] == "run-bg-4"
    assert seen["handle"] is handle


def test_no_run_id_means_no_watcher():
    handle = _Handle()
    assert background_jobs.watch_process("", handle) is None
