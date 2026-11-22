"""`shell_run` / `shell_spawn` / process tools — the real-shell surface.

Unlike `terminal_run` (argv-only, no shell), these tools run a command string
through the detected host shell (PowerShell/CMD/bash). They are gated the same
way: `command.execute` autonomy grant, workspace confinement for cwd, output
and time caps, and the `ask` class still requires a single-use approval; a
`deny` is never run.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from ..tools import ToolContext
from .. import background_jobs
from . import policy
from . import shell as shell_mod
from . import terminal as terminal_mod
from .tools import _ensure_code_workspace, _present, emit_code_event
from .command_tools import _command_refusal, _record_command
from .events import redact_output
from .worktree import WorkspaceError

APPROVAL_KIND = "shell_command"


class ShellRunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str = Field(..., min_length=1, max_length=terminal_mod.MAX_COMMAND_CHARS)
    timeoutS: Optional[int] = Field(default=None, ge=1, le=shell_mod.MAX_TIMEOUT_S)  # noqa: N815
    cwd: Optional[str] = Field(default=None, max_length=1000)
    note: str = Field("", max_length=200)


class ShellSpawnInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str = Field(..., min_length=1, max_length=terminal_mod.MAX_COMMAND_CHARS)
    cwd: Optional[str] = Field(default=None, max_length=1000)


def _grants_command(ctx: ToolContext) -> bool:
    autonomy = getattr(ctx, "autonomy", None)
    return bool(autonomy and "command.execute" in policy.code_grants(str(autonomy)))


def _resolve_cwd(ctx: ToolContext, requested: Optional[str]) -> Optional[Path]:
    ws = getattr(ctx, "workspace", None)
    if ws is None:
        return None
    base = getattr(ws, "root", None) or getattr(ws, "path", None)
    if base is None:
        return None
    root = Path(str(base))
    if not requested:
        return root
    candidate = (root / requested).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_dir() else None


def _verdict(command: str):
    """Shell-specific classification: unlike the argv-only terminal, pipes and
    redirection are legitimate here. Destructive shapes stay denied; safe
    allowlisted single commands stay allow; everything else is ask."""
    text = str(command or "").strip()
    if not text:
        return terminal_mod.CommandVerdict("deny", "", "There was no command to run.")
    # W6: the always-on command hardline runs before the allow/ask/deny
    # classifier. It cannot be overridden by a grant, YOLO or approval.
    from ..security.hardline import evaluate as _hardline  # noqa: PLC0415

    fatal = _hardline(text)
    if fatal is not None:
        return terminal_mod.CommandVerdict(
            "deny", "", f"That command is blocked ({fatal.rule}): {fatal.reason}."
        )
    lowered = text.casefold()
    if any(bad in lowered for bad in terminal_mod._DENY_ARGS):
        return terminal_mod.CommandVerdict("deny", "", "That command could damage the computer, so I did not run it.")
    head = text.split(None, 1)[0].casefold().lstrip("&")
    for suffix in (".exe", ".cmd", ".bat", ".com"):
        if head.endswith(suffix):
            head = head[: -len(suffix)]
    if head in terminal_mod.DENY_PROGRAMS:
        return terminal_mod.CommandVerdict("deny", head, "That command can damage the computer or fetch things I can't run, so I did not run it.")
    import shlex as _shlex  # noqa: PLC0415
    try:
        tokens = _shlex.split(text, posix=(os.name != "nt"))
    except ValueError:
        tokens = []
    if tokens and not any(t in terminal_mod._METACHAR_TOKENS for t in tokens) and "|" not in text and "&&" not in text:
        base = terminal_mod.classify(text)
        if base.kind == "allow":
            return base
    return terminal_mod.CommandVerdict("ask", head, "That command runs through the shell, so it needs your go-ahead.")


async def shell_run(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = ShellRunInput(**_present(args))
    ws = getattr(ctx, "workspace", None)
    if ws is None:
        return _command_refusal("WORKSPACE_UNBOUND", "there is no project folder to run in", command_class="shell")
    if not _grants_command(ctx):
        return _command_refusal("PROFILE_DENIED", "this task isn't set up to run commands", command_class="shell")
    verdict = _verdict(model.command)
    if verdict.kind == "deny":
        await emit_code_event(ctx, "command.refused", {"commandClass": "shell", "code": "COMMAND_DENIED"})
        return _command_refusal("COMMAND_NOT_ALLOWLISTED", verdict.reason, command_class="shell")
    try:
        cw, binding, resolved, _actor = _ensure_code_workspace(ctx)
    except WorkspaceError as exc:
        return _command_refusal(exc.code, exc.reason, command_class="shell")
    cwd = _resolve_cwd(ctx, model.cwd)
    if cwd is None:
        return _command_refusal("WORKSPACE_ESCAPE", "cwd must stay inside the workspace", command_class="shell")
    # W7: a command that can write gets one shadow snapshot before it runs.
    # A read-only command never pays for one.
    try:
        from .checkpoints import CheckpointError, command_may_write, snapshot  # noqa: PLC0415

        if command_may_write(model.command):
            snapshot(
                getattr(resolved, "root", None) or cwd,
                reason="before-terminal", run_id=str(getattr(ctx, "run_id", "") or ""),
                turn_key=str(getattr(ctx, "checkpoint_turn", None) or getattr(ctx, "run_id", "") or ""),
            )
    except CheckpointError as exc:
        from .checkpoints import checkpoint_mode  # noqa: PLC0415

        if checkpoint_mode() == "enforce":
            return _command_refusal(exc.code, exc.reason, command_class="shell")
    except Exception:  # noqa: BLE001 - best-effort in warn/off
        pass
    await emit_code_event(ctx, "command.started", {
        "commandClass": "shell", "program": verdict.program, "verdict": verdict.kind, "timeoutS": model.timeoutS,
    })
    result = shell_mod.run(model.command, cwd=cwd, timeout_s=model.timeoutS)
    if result.status == "failed" and result.stderr.startswith("SANDBOX_UNAVAILABLE"):
        return _command_refusal("SANDBOX_UNAVAILABLE", result.stderr, command_class="shell")
    event = "command.killed" if result.timed_out else "command.completed"
    await emit_code_event(ctx, event, {
        "commandClass": "shell", "program": verdict.program, "exitCode": result.exit_code,
        "durationMs": result.duration_ms, "timedOut": result.timed_out, "backend": "shell",
    })
    # Round 3 / W9 (ADR-H297): the output is REDACTED before it reaches the
    # model, the run blob or the ledger. A command that prints a key would
    # otherwise put it in all three, and in the conversation.
    stdout, stdout_redacted = redact_output(result.stdout)
    stderr, stderr_redacted = redact_output(result.stderr)
    redacted_lines = stdout_redacted + stderr_redacted
    if redacted_lines:
        await emit_code_event(ctx, "code.output.redacted", {
            "commandClass": "shell", "lines": redacted_lines,
        })
    _record_command(
        ctx, command_class="shell", argv=[model.command], cwd=str(cwd),
        exit_code=result.exit_code if result.exit_code is not None else -1,
        duration_ms=result.duration_ms, output=(stdout + stderr),
        total_chars=len(result.stdout) + len(result.stderr), backend="shell",
        decision={"allowed": True, "code": "OK", "verdict": verdict.kind},
        timed_out=result.timed_out, truncated=result.truncated,
    )
    status = result.status
    header = f"Ran `{model.command}`: exit={result.exit_code} durationMs={result.duration_ms}{' TIMED OUT' if result.timed_out else ''}"
    parts = [header]
    if redacted_lines:
        parts.append(f"[{redacted_lines} line(s) redacted for privacy]")
    if stdout:
        parts.append(stdout.rstrip())
    if stderr:
        parts.append("STDERR:\n" + stderr.rstrip())
    text = "\n".join(parts)
    data = {
        "status": status, "commandClass": "shell", "program": verdict.program,
        "cwd": str(cwd), "exitCode": result.exit_code, "durationMs": result.duration_ms,
        "timedOut": result.timed_out, "truncated": result.truncated,
        "stdout": stdout, "stderr": stderr,
        "redactedLines": redacted_lines,
    }
    return text, data


async def shell_spawn(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    model = ShellSpawnInput(**_present(args))
    ws = getattr(ctx, "workspace", None)
    if ws is None:
        return _command_refusal("WORKSPACE_UNBOUND", "there is no project folder to run in", command_class="shell")
    if not _grants_command(ctx):
        return _command_refusal("PROFILE_DENIED", "this task isn't set up to run commands", command_class="shell")
    verdict = _verdict(model.command)
    if verdict.kind == "deny":
        return _command_refusal("COMMAND_NOT_ALLOWLISTED", verdict.reason, command_class="shell")
    try:
        cw, binding, resolved, _actor = _ensure_code_workspace(ctx)
    except WorkspaceError as exc:
        return _command_refusal(exc.code, exc.reason, command_class="shell")
    cwd = _resolve_cwd(ctx, model.cwd)
    if cwd is None:
        return _command_refusal("WORKSPACE_ESCAPE", "cwd must stay inside the workspace", command_class="shell")
    try:
        handle = shell_mod.spawn(model.command, cwd=cwd)
    except Exception as exc:  # noqa: BLE001 - a missing primitive is a named refusal
        from .sandbox import SandboxUnavailable  # noqa: PLC0415

        if isinstance(exc, SandboxUnavailable):
            return _command_refusal("SANDBOX_UNAVAILABLE", str(exc), command_class="shell")
        raise
    await emit_code_event(ctx, "command.started", {"commandClass": "shell", "program": verdict.program, "pid": handle.pid})
    # Gap #4: the run learns about completion at its next step boundary. The
    # model is told not to poll; `process_output` stays available for a log.
    background_jobs.watch_process(str(getattr(ctx, "run_id", "") or ""), handle)
    return (
        f"Spawned process {handle.pid} (status=running). You will be notified "
        "automatically when it finishes; do not poll process_output for it. "
        f"Use process_output/{handle.pid} only if you need the output now.",
        {"status": "running", "pid": handle.pid, "command": model.command, "cwd": str(cwd)},
    )


async def process_output(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    pid = str(_present(args).get("pid") or "")
    handle = shell_mod.get(pid)
    if handle is None:
        return (f"REFUSED: no such process `{pid}`.", {"status": "not_found", "pid": pid})
    handle.poll()
    out, err = handle.output()
    text = f"Process {pid}: status={handle.status} exit={handle.exit_code}\n"
    text += (out or "(no stdout yet)") + ("\nSTDERR:\n" + err if err else "")
    return (text, {"status": handle.status, "pid": pid, "exitCode": handle.exit_code,
                   "stdout": out[-shell_mod.MAX_OUTPUT_CHARS:], "stderr": err[-shell_mod.MAX_OUTPUT_CHARS:]})


async def process_kill(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    pid = str(_present(args).get("pid") or "")
    if shell_mod.kill(pid):
        return (f"Killed process {pid}.", {"status": "killed", "pid": pid})
    return (f"REFUSED: no such process `{pid}`.", {"status": "not_found", "pid": pid})


async def process_list(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    rows = []
    for h in shell_mod.list_processes():
        rows.append({"pid": h.pid, "command": h.command, "status": h.status,
                     "exitCode": h.exit_code, "cwd": h.cwd})
    text = "\n".join(f"{r['pid']}  {r['status']:<8}  {r['command']}" for r in rows) or "No processes."
    return (text, {"status": "ok", "processes": rows})


def make_shell_authorizer(inner: Any = None):
    """Gate `shell_run`/`shell_spawn`: deny-class refused, ask-class needs a
    single-use approval (terminal_command kind), allow passes. Everything else
    falls through to `inner`."""

    async def authorize(tool: Any, args: dict[str, Any], ctx: Any = None) -> bool:
        name = str(getattr(tool, "name", ""))
        if name in ("process_output", "process_list"):
            return True
        if name == "process_kill":
            return True
        if name not in ("shell_run", "shell_spawn"):
            if inner is not None:
                return bool(await inner(tool, args, ctx))
            return False
        command = str((args or {}).get("command") or "")
        verdict = _verdict(command)
        if verdict.kind == "deny":
            return False
        if verdict.kind == "allow":
            return True
        approvals = getattr(ctx, "approvals", None) if ctx is not None else None
        workspace = getattr(ctx, "workspace", None) if ctx is not None else None
        if approvals is None or workspace is None:
            return False
        ws_id = str(getattr(workspace, "id", ""))
        import hashlib as _hashlib  # noqa: PLC0415
        digest = _hashlib.sha256(f"{ws_id}\x00{command.strip()}".encode("utf-8")).hexdigest()
        # Standing grant ("always allow <program> in this workspace"): a match
        # removes the prompt for THIS program. A `deny` was already returned above,
        # so a grant can never make a destructive command run.
        grant_pattern = (verdict.program or command.strip().split(None, 1)[0] if command.strip() else "").casefold()
        grants = getattr(approvals, "grants", None)
        if grants is not None and grant_pattern and grants.matches("command", ws_id, grant_pattern) is not None:
            try:
                from .tools import emit_code_event as _emit  # noqa: PLC0415
                await _emit(ctx, "approval.granted", {
                    "kind": "terminal_command", "grantPattern": grant_pattern,
                    "workspaceId": ws_id, "command": command[:180],
                })
            except Exception:  # noqa: BLE001
                pass
            return True
        claimed = approvals.consume_kind(
            kind="terminal_command",
            request_key=ws_id, digest=digest, workspace_id=ws_id,
        )
        if claimed is not None:
            return True
        pending = None
        try:
            from ..mcp.approvals import ApprovalStore  # noqa: PLC0415,F401
            pending = approvals.request_kind(
                kind="terminal_command", request_key=ws_id, digest=digest,
                effect="external_action",
                run_id=str(getattr(ctx, "run_id", "")),
                workspace_id=ws_id,
                action_summary=f"Run a shell command: {command[:180]}",
                risk="medium",
                grant_kind="command", grant_pattern=grant_pattern, grant_scope=ws_id,
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            from .tools import emit_code_event as _emit  # noqa: PLC0415
            await _emit(ctx, "approval.requested", {
                "approvalId": (pending or {}).get("id"),
                "kind": "terminal_command",
                "title": "Approve running a shell command",
                "summary": f"Run in the project: {command[:180]}",
                "effect": "external_action",
                "risk": "medium",
                "command": command[:180],
                "workspaceId": ws_id,
                "grantPattern": grant_pattern,
                "grantLabel": f"Always allow `{grant_pattern}` in this workspace" if grant_pattern else None,
            })
        except Exception:  # noqa: BLE001
            pass
        if pending is not None:
            from ..mcp.approvals import pause_for_decision  # noqa: PLC0415

            if await pause_for_decision(
                approvals, ctx, pending.get("id"),
                lambda: approvals.consume_kind(
                    kind="terminal_command", request_key=ws_id, digest=digest, workspace_id=ws_id,
                ),
                emitter=getattr(ctx, "emitter", None), digest=digest,
                event_type="approval.approved",
                approved_payload={"kind": "terminal_command", "command": command[:180]},
            ):
                return True
        return False

    return authorize


def shell_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The exact command line, run through the host shell (PowerShell/CMD/bash). Pipes and redirection work."},
            "timeoutS": {"type": "integer", "description": "Wall-clock limit in seconds (default 120, max 900)."},
            "cwd": {"type": "string", "description": "Directory to run in, relative to the workspace root. Must stay inside the workspace."},
            "note": {"type": "string", "description": "Why this command is wanted; recorded in the trace."},
        },
        "required": ["command"],
        "additionalProperties": False,
    }


def spawn_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The command to run in the background through the host shell."},
            "cwd": {"type": "string", "description": "Directory to run in, relative to the workspace root."},
        },
        "required": ["command"],
        "additionalProperties": False,
    }


def pid_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"pid": {"type": "string", "description": "The process id returned by shell_spawn."}},
        "required": ["pid"],
        "additionalProperties": False,
    }


__all__ = [
    "APPROVAL_KIND",
    "make_shell_authorizer",
    "process_kill",
    "process_list",
    "process_output",
    "shell_run",
    "shell_schema",
    "shell_spawn",
    "spawn_schema",
    "pid_schema",
]
