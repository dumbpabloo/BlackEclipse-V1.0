"""Minimal ACP client: handshake, prompt streaming, and permission policy."""

from __future__ import annotations

import asyncio
import sys

import pytest

from harness.acp_client import run_acp_prompt
from harness.automation.dispatcher import Dispatcher
from harness.automation.model import AutomationDefinition
from harness.automation.validation import is_valid, validate_automation

FAKE = r'''
import json, sys

def send(obj):
    print(json.dumps(obj), flush=True)

for line in sys.stdin:
    if not line.strip():
        continue
    msg = json.loads(line)
    method = msg.get("method")
    rid = msg.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": 2}})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": "sess_test"}})
    elif method == "session/prompt":
        send({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": "sess_test",
            "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Hello "}},
        }})
        send({"jsonrpc": "2.0", "id": 99, "method": "session/request_permission", "params": {
            "sessionId": "sess_test",
            "options": [{"optionId": "allow-once", "kind": "allow_once"}],
        }})
        reply = json.loads(sys.stdin.readline())
        outcome = (((reply.get("result") or {}).get("outcome") or {}).get("outcome"))
        text = "allowed" if outcome == "selected" else "denied"
        send({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": "sess_test",
            "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}},
        }})
        send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
    elif "id" in msg:
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
'''


def _run(tmp_path, policy: str):
    script = tmp_path / "fake_acp.py"
    script.write_text(FAKE, encoding="utf-8")
    return run_acp_prompt(
        sys.executable,
        "Say hello",
        args=[str(script)],
        permission_policy=policy,
        timeout_s=20,
    )


def test_acp_prompt_streams_text_and_streams_permission_denial(tmp_path):
    result = _run(tmp_path, "deny")
    assert result.stop_reason == "end_turn"
    assert result.text == "Hello denied"


CAPS = r'''
import json, sys

next_id = 100

def send(obj):
    print(json.dumps(obj), flush=True)

def rpc(method, params):
    global next_id
    rid = next_id
    next_id += 1
    send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
    while True:
        msg = json.loads(sys.stdin.readline())
        if msg.get("id") == rid:
            return msg.get("result"), msg.get("error")

def chunk(text):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {
        "sessionId": "sess_caps",
        "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}},
    }})

for line in sys.stdin:
    if not line.strip():
        continue
    msg = json.loads(line)
    method = msg.get("method")
    rid = msg.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": 1, "agentCapabilities": {"loadSession": True},
        }})
    elif method in ("session/new", "session/load"):
        mcp = len((msg.get("params") or {}).get("mcpServers") or [])
        chunk(("loaded=1" if method == "session/load" else "new=1") + f" mcp={mcp}")
        send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": "sess_caps"}})
    elif method == "session/prompt":
        loaded, _ = rpc("fs/read_text_file", {"sessionId": "sess_caps", "path": "hello.txt"})
        chunk(" fs=" + ((loaded or {}).get("content") or "").strip())
        rpc("fs/write_text_file", {"sessionId": "sess_caps", "path": "out.txt", "content": "written"})
        created, _ = rpc("terminal/create", {
            "sessionId": "sess_caps", "command": sys.executable, "args": ["-c", "print('term-ok')"],
        })
        tid = (created or {}).get("terminalId")
        rpc("terminal/wait_for_exit", {"sessionId": "sess_caps", "terminalId": tid})
        out, _ = rpc("terminal/output", {"sessionId": "sess_caps", "terminalId": tid})
        chunk(" term=" + ((out or {}).get("output") or "").strip())
        denied, _ = rpc("fs/read_text_file", {"sessionId": "sess_caps", "path": "../escape.txt"})
        chunk(" denied=" + ("yes" if denied is None else "no"))
        send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
    elif "id" in msg:
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
'''


def test_acp_session_load_mcp_and_opt_in_fs_terminal(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "hello.txt").write_text("fs-ok", encoding="utf-8")
    script = tmp_path / "caps_acp.py"
    script.write_text(CAPS, encoding="utf-8")
    result = run_acp_prompt(
        sys.executable,
        "exercise caps",
        args=[str(script)],
        cwd=str(workspace),
        workspace_root=str(workspace),
        session_id="resume-1",
        mcp_servers=[{"name": "filesystem", "command": "x"}],
        fs_read=True,
        fs_write=True,
        terminal=True,
        timeout_s=20,
    )
    assert "loaded=1" in result.text
    assert "mcp=1" in result.text
    assert "fs=fs-ok" in result.text
    assert "term=term-ok" in result.text
    assert "denied=yes" in result.text
    assert (workspace / "out.txt").read_text(encoding="utf-8") == "written"


def test_acp_permission_policy_can_allow_once(tmp_path):
    result = _run(tmp_path, "allow")
    assert result.text == "Hello allowed"


class _Service:
    tasks = None


@pytest.mark.asyncio
async def test_dispatcher_runs_external_agent(tmp_path):
    script = tmp_path / "fake_acp.py"
    script.write_text(FAKE, encoding="utf-8")
    definition = AutomationDefinition(
        id="auto_acp",
        name="External agent",
        state="active",
        trigger={"type": "manual"},
        action={
            "type": "run_external_agent",
            "objective": "Say hello",
            "config": {"externalAgent": {
                "command": sys.executable,
                "args": [str(script)],
                "permissionPolicy": "deny",
                "timeoutS": 20,
            }},
        },
        created_at="2026-11-26T00:00:00Z",
        updated_at="2026-11-26T00:00:00Z",
    )
    result = await Dispatcher(_Service())._run_external_agent(definition, "run_1")
    assert result.state == "completed"
    assert result.output == "Hello denied"
    assert result.detail["externalAgent"] is True
    assert is_valid(validate_automation(definition))
