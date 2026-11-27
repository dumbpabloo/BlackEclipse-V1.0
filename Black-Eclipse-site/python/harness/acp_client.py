"""A minimal Agent Client Protocol (ACP) client.

Eclipse's Python runtime has always been the agent loop. This module adds the
OTHER direction: launch an external ACP agent as a subprocess and drive it over
newline-delimited JSON-RPC 2.0 on stdio, so a bot can use a subscription-backed
CLI agent (Gemini, Claude Code, OpenCode, …) without Eclipse rebuilding that
agent.

Scope is deliberately small and honest:
  * initialize -> session/new -> session/prompt
  * stream `session/update` text into one answer
  * answer permission requests with a policy (deny by default)
  * unsupported client methods return a JSON-RPC error
  * no shell: the executable and argv are separate, so a model cannot inject one

It is not a full ACP SDK (no session/load, MCP bridging, terminals or file
editing). Those are explicit capability refusals, not silent failures.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

DEFAULT_TIMEOUT_S = 120.0
MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_TEXT_CHARS = 200_000


class AcpError(RuntimeError):
    """An ACP handshake/turn failure with a stable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class AcpResult:
    text: str
    stop_reason: str = "end_turn"
    stderr: str = ""
    updates: list[dict[str, Any]] = field(default_factory=list)


def _text_of(content: Any) -> str:
    """Extract text from an ACP content block / update, never guessing."""
    if isinstance(content, str):
        return content
    if not isinstance(content, dict):
        return ""
    block_type = content.get("type")
    if block_type == "text" and isinstance(content.get("text"), str):
        return content["text"]
    # Some agents wrap the block as {"content": {...}}.
    if isinstance(content.get("content"), (dict, str)):
        return _text_of(content["content"])
    return ""


def _update_text(update: dict[str, Any]) -> str:
    kind = str(update.get("sessionUpdate") or update.get("session_update") or "")
    if kind in ("agent_message_chunk", "agentMessageChunk", "agent_message"):
        return _text_of(update.get("content"))
    if isinstance(update.get("content"), dict):
        return _text_of(update["content"])
    return ""


class _Terminal:
    """One ACP terminal child with a bounded output buffer."""

    def __init__(self, command: str, args: list[str], cwd: str, env: dict[str, str]) -> None:
        self._out: list[str] = []
        self._lock = threading.Lock()
        self._limit = 200_000
        self._proc = subprocess.Popen(
            [command, *args],
            cwd=cwd,
            env={**os.environ, **env},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        if self._proc.stdout is None:
            return
        for line in self._proc.stdout:
            with self._lock:
                self._out.append(line)
                joined = "".join(self._out)
                if len(joined) > self._limit:
                    self._out = [joined[-self._limit:]]
        with self._lock:
            self._out.append("")

    def output(self) -> str:
        with self._lock:
            return "".join(self._out)[-self._limit:]

    def wait(self, timeout: float = 30.0) -> Optional[int]:
        try:
            return self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def kill(self) -> None:
        if self._proc.poll() is None:
            self._proc.kill()


class AcpClient:
    """One subprocess, one JSON-RPC connection, one prompt at a time."""

    def __init__(
        self,
        command: str,
        args: Optional[list[str]] = None,
        *,
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        permission_policy: str = "deny",
        timeout_s: float = DEFAULT_TIMEOUT_S,
        mcp_servers: Optional[list[dict[str, Any]]] = None,
        session_id: Optional[str] = None,
        fs_read: bool = False,
        fs_write: bool = False,
        terminal: bool = False,
        workspace_root: Optional[str] = None,
    ) -> None:
        if not command or not str(command).strip():
            raise AcpError("ACP_COMMAND_REQUIRED", "an ACP agent command is required")
        executable = shutil.which(command) or command
        self._argv = [executable, *(args or [])]
        self._cwd = os.path.abspath(cwd or os.getcwd())
        self._env = {**os.environ, **(env or {})}
        self._permission_policy = "allow" if permission_policy == "allow" else "deny"
        self._timeout_s = max(5.0, float(timeout_s))
        self._next_id = 1
        self._queue: queue.Queue[Any] = queue.Queue()
        self._stderr: list[str] = []
        self._proc: Optional[subprocess.Popen[str]] = None
        self._closed = False
        self._updates: list[dict[str, Any]] = []
        self._text: list[str] = []
        self._mcp_servers = list(mcp_servers or [])
        self._resume_session_id = str(session_id or "").strip() or None
        self._fs_read = bool(fs_read)
        self._fs_write = bool(fs_write)
        self._terminal = bool(terminal)
        self._workspace_root = os.path.abspath(workspace_root or self._cwd)
        self._terminals: dict[str, _Terminal] = {}
        self._terminal_seq = 0
        self._session_id: Optional[str] = None

    # ── process/transport ────────────────────────────────────────────────
    def start(self) -> None:
        if self._proc is not None:
            return
        try:
            self._proc = subprocess.Popen(
                self._argv,
                cwd=self._cwd,
                env=self._env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                shell=False,
            )
        except OSError as exc:
            raise AcpError("ACP_START_FAILED", f"could not start the ACP agent: {exc}") from exc
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            raw = line.strip()
            if not raw:
                continue
            if len(raw.encode("utf-8")) > MAX_LINE_BYTES:
                self._queue.put(AcpError("ACP_LINE_TOO_LARGE", "an ACP message exceeded the line limit"))
                return
            try:
                self._queue.put(json.loads(raw))
            except json.JSONDecodeError:
                # Logs belong on stderr; a malformed stdout line is a protocol
                # failure, not text to append to the answer.
                self._queue.put(AcpError("ACP_INVALID_JSON", raw[:500]))

    def _read_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for line in proc.stderr:
            if len(self._stderr) < 80:
                self._stderr.append(line.rstrip()[:500])

    def _send(self, message: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise AcpError("ACP_NOT_STARTED", "the ACP agent is not running")
        try:
            proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise AcpError("ACP_BROKEN_PIPE", f"the ACP agent closed its input: {exc}") from exc

    def _next(self) -> int:
        value = self._next_id
        self._next_id += 1
        return value

    def _confine(self, raw: Any) -> Path:
        if not isinstance(raw, str) or not raw.strip():
            raise AcpError("ACP_PATH_REQUIRED", "a path is required")
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = Path(self._workspace_root) / candidate
        resolved = candidate.resolve()
        root = Path(self._workspace_root).resolve()
        if resolved != root and root not in resolved.parents:
            raise AcpError("ACP_PATH_DENIED", "the path is outside the bot workspace")
        return resolved

    def _handle_agent_request(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        method = str(message.get("method") or "")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        try:
            if method == "session/request_permission":
                options = params.get("options") or []
                chosen = None
                if self._permission_policy == "allow":
                    for option in options:
                        if isinstance(option, dict) and str(option.get("kind") or "").startswith("allow"):
                            chosen = option.get("optionId") or option.get("option_id")
                            break
                result: dict[str, Any] = (
                    {"outcome": {"outcome": "selected", "optionId": chosen}}
                    if chosen else {"outcome": {"outcome": "cancelled"}}
                )
                self._send({"jsonrpc": "2.0", "id": request_id, "result": result})
                return
            if method == "fs/read_text_file" and self._fs_read:
                path = self._confine(params.get("path"))
                content = path.read_text(encoding="utf-8", errors="replace")
                line = params.get("line")
                limit = params.get("limit")
                if isinstance(line, int) and line > 0:
                    lines = content.splitlines(keepends=True)
                    start = line - 1
                    stop = start + (limit if isinstance(limit, int) and limit > 0 else len(lines))
                    content = "".join(lines[start:stop])
                self._send({"jsonrpc": "2.0", "id": request_id, "result": {
                    "content": content[:MAX_TEXT_CHARS],
                }})
                return
            if method == "fs/write_text_file" and self._fs_write:
                path = self._confine(params.get("path"))
                content = params.get("content")
                if not isinstance(content, str):
                    raise AcpError("ACP_CONTENT_REQUIRED", "write_text_file needs text content")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
                self._send({"jsonrpc": "2.0", "id": request_id, "result": {}})
                return
            if method == "terminal/create" and self._terminal:
                command = params.get("command")
                if not isinstance(command, str) or not command.strip():
                    raise AcpError("ACP_COMMAND_REQUIRED", "terminal/create needs a command")
                cwd = self._confine(params.get("cwd") or self._workspace_root)
                args = [str(arg) for arg in (params.get("args") or []) if isinstance(arg, (str, int, float))]
                env = params.get("env") if isinstance(params.get("env"), dict) else {}
                self._terminal_seq += 1
                terminal_id = f"term_{self._terminal_seq}"
                self._terminals[terminal_id] = _Terminal(
                    command, args, str(cwd), {str(k): str(v) for k, v in env.items()},
                )
                self._send({"jsonrpc": "2.0", "id": request_id, "result": {"terminalId": terminal_id}})
                return
            if method == "terminal/output" and self._terminal:
                terminal = self._terminals.get(str(params.get("terminalId") or ""))
                if terminal is None:
                    raise AcpError("ACP_TERMINAL_NOT_FOUND", "unknown terminalId")
                code = terminal._proc.poll()  # noqa: SLF001 - same module
                result = {"output": terminal.output(), "truncated": False}
                if code is not None:
                    result["exitStatus"] = {"exitCode": code, "signal": None}
                self._send({"jsonrpc": "2.0", "id": request_id, "result": result})
                return
            if method == "terminal/wait_for_exit" and self._terminal:
                terminal = self._terminals.get(str(params.get("terminalId") or ""))
                if terminal is None:
                    raise AcpError("ACP_TERMINAL_NOT_FOUND", "unknown terminalId")
                code = terminal.wait()
                self._send({"jsonrpc": "2.0", "id": request_id, "result": {
                    "exitCode": code, "signal": None,
                }})
                return
            if method in ("terminal/kill", "terminal/release") and self._terminal:
                terminal = self._terminals.pop(str(params.get("terminalId") or ""), None)
                if terminal is not None:
                    terminal.kill()
                self._send({"jsonrpc": "2.0", "id": request_id, "result": {}})
                return
        except AcpError as exc:
            self._send({
                "jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32602, "message": f"{exc.code}: {exc.message}"},
            })
            return
        except OSError as exc:
            self._send({
                "jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32603, "message": str(exc)[:300]},
            })
            return
        # We did not advertise this capability; refuse clearly.
        self._send({
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"unsupported client method: {method}"},
        })

    def _wait_for(self, request_id: int, deadline: float) -> dict[str, Any]:
        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            try:
                message = self._queue.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                continue
            if isinstance(message, AcpError):
                raise message
            if not isinstance(message, dict):
                continue
            if "method" in message and "id" in message:
                self._handle_agent_request(message)
                continue
            if "method" in message:
                if message.get("method") == "session/update":
                    params = message.get("params") or {}
                    update = params.get("update") if isinstance(params, dict) else None
                    if isinstance(update, dict):
                        self._updates.append(update)
                        text = _update_text(update)
                        if text:
                            self._text.append(text)
                continue
            if message.get("id") == request_id:
                error = message.get("error")
                if isinstance(error, dict):
                    raise AcpError(
                        str(error.get("code") or "ACP_ERROR"),
                        str(error.get("message") or "the ACP agent returned an error"),
                    )
                result = message.get("result")
                return result if isinstance(result, dict) else {}
        raise AcpError(
            "ACP_TIMEOUT",
            f"the ACP agent did not answer within {self._timeout_s:.0f}s",
        )

    def request(self, method: str, params: dict[str, Any], *, timeout_s: Optional[float] = None) -> dict[str, Any]:
        self.start()
        request_id = self._next()
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        timeout = timeout_s if timeout_s is not None else self._timeout_s
        return self._wait_for(request_id, time.monotonic() + max(1.0, timeout))

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    # ── ACP flow ─────────────────────────────────────────────────────────
    def prompt(self, text: str, *, cwd: Optional[str] = None) -> AcpResult:
        self._text = []
        self._updates = []
        capabilities = {
            "fs": {"readTextFile": self._fs_read, "writeTextFile": self._fs_write},
            "terminal": self._terminal,
        }
        try:
            # v1 keeps the blocking prompt-turn semantics this client relies on:
            # `session/prompt` answers when the turn is complete. v2 returns at
            # prompt acceptance and streams completion separately.
            init = self.request("initialize", {
                "protocolVersion": 1,
                "clientCapabilities": capabilities,
                "clientInfo": {"name": "eclipse", "title": "Black Eclipse", "version": "1.0.0"},
            })
        except AcpError:
            # A v2-only agent may reject the v1 capability shape. Retry once with
            # the v2 fields; an actual auth/setup failure still surfaces.
            init = self.request("initialize", {
                "protocolVersion": 2,
                "capabilities": capabilities,
                "info": {"name": "eclipse", "title": "Black Eclipse", "version": "1.0.0"},
            })
        # ACP v1 agents answer with `protocolVersion`; if they only speak v1 we
        # still proceed because the session methods we use exist in both.
        if init.get("protocolVersion") not in (1, 2, None):
            raise AcpError("ACP_VERSION_UNSUPPORTED", f"agent chose protocol {init.get('protocolVersion')}")
        session_params = {
            "cwd": cwd or self._cwd,
            "mcpServers": self._mcp_servers,
        }
        session: Optional[dict[str, Any]] = None
        agent_caps = init.get("agentCapabilities")
        can_load = bool(
            (isinstance(agent_caps, dict) and agent_caps.get("loadSession"))
            or init.get("loadSession")
        )
        if self._resume_session_id and can_load:
            try:
                session = self.request("session/load", {
                    "sessionId": self._resume_session_id,
                    **session_params,
                })
            except AcpError:
                # A failed resume is a new session, never a silent wrong history.
                session = None
        if session is None:
            session = self.request("session/new", session_params)
        session_id = (
            session.get("sessionId") or session.get("session_id")
            or self._resume_session_id
        )
        if not isinstance(session_id, str) or not session_id:
            raise AcpError("ACP_NO_SESSION", "the ACP agent did not return a session id")
        self._session_id = session_id
        result = self.request(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": text[:MAX_TEXT_CHARS]}],
            },
            timeout_s=self._timeout_s,
        )
        answer = "".join(self._text).strip()
        return AcpResult(
            text=answer[:MAX_TEXT_CHARS],
            stop_reason=str(result.get("stopReason") or result.get("stop_reason") or "end_turn"),
            stderr="\n".join(self._stderr)[-4_000:],
            updates=self._updates[-200:],
        )

    def cancel(self, session_id: str) -> None:
        try:
            self.notify("session/cancel", {"sessionId": session_id})
        except AcpError:
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for terminal in self._terminals.values():
            terminal.kill()
        self._terminals.clear()
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass


def run_acp_prompt(
    command: str,
    prompt: str,
    *,
    args: Optional[list[str]] = None,
    cwd: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
    permission_policy: str = "deny",
    timeout_s: float = DEFAULT_TIMEOUT_S,
    mcp_servers: Optional[list[dict[str, Any]]] = None,
    session_id: Optional[str] = None,
    fs_read: bool = False,
    fs_write: bool = False,
    terminal: bool = False,
    workspace_root: Optional[str] = None,
) -> AcpResult:
    """Run one prompt through an external ACP agent and close the process."""
    client = AcpClient(
        command, args,
        cwd=cwd, env=env,
        permission_policy=permission_policy,
        timeout_s=timeout_s,
        mcp_servers=mcp_servers,
        session_id=session_id,
        fs_read=fs_read,
        fs_write=fs_write,
        terminal=terminal,
        workspace_root=workspace_root,
    )
    try:
        return client.prompt(prompt, cwd=cwd)
    finally:
        client.close()


__all__ = ["AcpClient", "AcpError", "AcpResult", "run_acp_prompt"]
