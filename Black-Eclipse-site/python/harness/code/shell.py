"""Real shell execution with background processes.

`terminal.py` is argv-only and shell-free by design. This module adds the
explicitly-opted-in *real shell* a coding harness needs: a command string runs
through the detected shell (PowerShell/CMD on Windows, bash/sh elsewhere),
still confined to the bound workspace root, still timed out, still capped in
output, and still gated by the same approval policy as `terminal_run`.

Two surfaces:
  * one-shot:  `run(command...)` -> structured result (for `shell_run`)
  * background: `spawn(command...)` -> ProcessHandle with pollable output
    (for `shell_spawn` / `process_output` / `process_kill` / `process_list`)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

MAX_OUTPUT_CHARS = 200_000
DEFAULT_TIMEOUT_S = 120
MAX_TIMEOUT_S = 900


def _scrubbed_env() -> dict[str, str]:
    """The child environment: the allowlist ONLY, never `os.environ` in full.

    This is the SAME builder the sandboxed command path uses
    (`code.sandbox.build_env`), so a shell and a sandboxed command see the
    same world. A secret that is absent from one is absent from both.
    """
    from .sandbox import build_env  # noqa: PLC0415

    return build_env()


def detect_shell() -> list[str]:
    """The argv prefix for the host shell, best-effort."""
    if os.name == "nt":
        pwsh = shutil.which("powershell") or shutil.which("pwsh")
        if pwsh:
            return [pwsh, "-NoProfile", "-NonInteractive", "-Command"]
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        return [comspec, "/d", "/s", "/c"]
    for name in ("bash", "sh"):
        found = shutil.which(name)
        if found:
            return [found, "-c"]
    return ["/bin/sh", "-c"]


@dataclass
class ShellResult:
    command: str
    cwd: str
    exit_code: Optional[int]
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    truncated: bool
    status: str  # "success" | "failed" | "timeout" | "killed"


def run(
    command: str,
    *,
    cwd: Path,
    timeout_s: Optional[int] = None,
    env_extra: Optional[dict[str, str]] = None,
    stdin: Optional[str] = None,
) -> ShellResult:
    """Run one command string through the SAME isolation primitive as a program.

    W6: this used to call `subprocess.run` directly, which gave the shell the
    scrubbed environment but none of the process-tree isolation. It now builds a
    sandbox `RunSpec` (argv shell + command, scrubbed env, timeout, output cap,
    no network) and uses `sandbox.run`, so a timeout/cancel kills the whole tree
    through the Windows Job Object / POSIX process group.

    `stdin` is accepted for compatibility; the sandbox runs with DEVNULL.
    """
    from .sandbox import RunSpec as SandboxSpec  # noqa: PLC0415
    from .sandbox import SandboxUnavailable  # noqa: PLC0415
    from .sandbox import run as sandbox_run  # noqa: PLC0415
    from .shell_safety import broad_python_kill_error  # noqa: PLC0415

    refusal = broad_python_kill_error(command)
    if refusal:
        # Refuse BEFORE spawning: `pkill python` would kill this process, and the
        # caller would see a "successful" command followed by a dead harness.
        return ShellResult(
            command=command, cwd=str(cwd), exit_code=126, stdout="", stderr=refusal,
            duration_ms=0, timed_out=False, truncated=False, status="failed",
        )

    argv = detect_shell() + [command]
    env = _scrubbed_env()
    if env_extra:
        env.update(env_extra)
    timeout = max(1, min(int(timeout_s or DEFAULT_TIMEOUT_S), MAX_TIMEOUT_S))
    spec = SandboxSpec(
        argv=argv,
        cwd=Path(cwd),
        env=env,
        timeout_s=timeout,
        output_cap_bytes=MAX_OUTPUT_CHARS,
        network=False,
    )
    try:
        raw = sandbox_run(spec)
    except SandboxUnavailable as exc:
        # Honest refusal: never run the command unsandboxed.
        return ShellResult(
            command, str(cwd), None, "", f"SANDBOX_UNAVAILABLE: {exc}", 0,
            False, False, "failed",
        )
    if raw.launch_error:
        return ShellResult(
            command, str(cwd), None, "", raw.launch_error, raw.duration_ms,
            False, False, "failed",
        )
    status = "timeout" if raw.timed_out else ("killed" if raw.killed else
                                             ("success" if raw.exit_code == 0 else "failed"))
    return ShellResult(
        command=command,
        cwd=str(cwd),
        exit_code=raw.exit_code,
        stdout=raw.output,
        stderr="",
        duration_ms=raw.duration_ms,
        timed_out=raw.timed_out,
        truncated=raw.truncated,
        status=status,
    )


@dataclass
class ProcessHandle:
    pid: str
    command: str
    cwd: str
    started: float
    proc: subprocess.Popen
    stdout_buf: list[str] = field(default_factory=list)
    stderr_buf: list[str] = field(default_factory=list)
    exit_code: Optional[int] = None
    status: str = "running"  # running | exited | killed
    _lock: threading.Lock = field(default_factory=threading.Lock)
    # W6: the isolation handle (Job Object / process group). When present,
    # `kill()` uses it so the WHOLE tree dies, not just the shell parent.
    _spawned: Optional[Any] = None

    def output(self) -> tuple[str, str]:
        with self._lock:
            return "".join(self.stdout_buf), "".join(self.stderr_buf)

    def poll(self) -> None:
        rc = self.proc.poll()
        if rc is not None and self.status == "running":
            self.exit_code = rc
            self.status = "exited"


_PROCESSES: dict[str, ProcessHandle] = {}
_PROCESSES_LOCK = threading.Lock()


def spawn(command: str, *, cwd: Path, env_extra: Optional[dict[str, str]] = None) -> ProcessHandle:
    """Start a background shell under the SAME isolation as a spawned program.

    W6: reuses `terminal.spawn.spawn_process` (per-process Job Object on
    Windows, its own process group on POSIX) so `process_kill` kills the tree
    and harness death cannot orphan a child. The `ProcessHandle` facade stays the
    same for the tools.
    """
    from .sandbox import RunSpec as SandboxSpec  # noqa: PLC0415
    from .sandbox import SandboxUnavailable  # noqa: PLC0415

    argv = detect_shell() + [command]
    env = _scrubbed_env()
    if env_extra:
        env.update(env_extra)
    spec = SandboxSpec(
        argv=argv,
        cwd=Path(cwd),
        env=env,
        timeout_s=MAX_TIMEOUT_S,
        output_cap_bytes=MAX_OUTPUT_CHARS,
        network=False,
    )
    from ..terminal.spawn import spawn_process  # noqa: PLC0415

    try:
        spawned = spawn_process(spec)
    except SandboxUnavailable:
        raise
    proc = spawned.proc
    handle = ProcessHandle(
        pid=uuid.uuid4().hex[:8], command=command, cwd=str(cwd),
        started=time.time(), proc=proc, _spawned=spawned,
    )

    def _pump(stream, buf):
        try:
            for line in iter(stream.readline, ""):
                with handle._lock:
                    buf.append(line)
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_pump, args=(proc.stdout, handle.stdout_buf), daemon=True).start()
    threading.Thread(target=_pump, args=(proc.stderr, handle.stderr_buf), daemon=True).start()
    with _PROCESSES_LOCK:
        _PROCESSES[handle.pid] = handle
    return handle


def get(pid: str) -> Optional[ProcessHandle]:
    with _PROCESSES_LOCK:
        return _PROCESSES.get(pid)


def kill(pid: str) -> bool:
    handle = get(pid)
    if handle is None:
        return False
    try:
        if handle._spawned is not None:
            handle._spawned.kill_tree()
        else:  # pragma: no cover - only a handle built without isolation
            handle.proc.kill()
        handle.status = "killed"
        try:
            handle.exit_code = handle.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001 - tree kill already reaped it
            handle.exit_code = None
        return True
    except Exception:  # noqa: BLE001
        return False


def list_processes() -> list[ProcessHandle]:
    with _PROCESSES_LOCK:
        handles = list(_PROCESSES.values())
    for h in handles:
        h.poll()
    return handles
