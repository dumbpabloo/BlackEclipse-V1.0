"""Branch parity, seventeenth tier: a shell command may not kill Python by name.

Reference fetched 2026-11-21 (MIT, `HKUDS/Vibe-Trading`,
`agent/src/tools/_shell_safety.py::broad_python_kill_error`).

The hazard is specific and severe: the harness runs in Python, so ``pkill python``,
``taskkill /IM python.exe`` or PowerShell's ``Stop-Process -Name python`` kills **the
harness itself** mid-run. The command looks like it succeeded and then the agent's own
process is gone — indistinguishable from a crash. Eclipse had no such guard at all
(`code/shell.py` went straight to the sandbox).

The reference's details are preserved: the check is **per shell segment** (``;``, a
bare ``&``, CR/LF) so ``echo hi && pkill python`` is caught; quotes are flattened so
quoting cannot smuggle the flag; and the refusal is actionable. One limit is shared
with the reference and stated rather than papered over: a bare
``get-process python | stop-process`` without a ``powershell`` prefix is not matched,
because that guard is scoped to a PowerShell command line.
"""

from __future__ import annotations

import pytest

from harness.code.shell import run
from harness.code.shell_safety import BROAD_PYTHON_KILL_ERROR, broad_python_kill_error


BLOCKED = [
    "pkill python",
    "pkill -9 python3",
    "sudo pkill python",
    "killall python",
    "pkill pythonw",
    "pkill python3.12",
    "taskkill /F /IM python.exe",
    "taskkill /IM python3.11 /T",
    "powershell Stop-Process -Name python",
    "pwsh -Command Stop-Process -Name python",
    'powershell -Command "Stop-Process -Name python"',
    "powershell Get-Process python | Stop-Process",
    "echo hi && pkill python",
    "cd /tmp; pkill python",
    "true\ntaskkill /F /IM python.exe",
]

ALLOWED = [
    "kill -9 1234",                 # a specific pid is legitimate
    "kill 4242",
    "pkill -f uvicorn",             # a named server, not every Python
    "killall node",
    "echo 'pkill python'",          # mentioned, not executed
    "python -c 'print(1)'",
    "npm run test",
    "pytest python/tests/test_x.py",
    "taskkill /F /PID 1234",
    "taskkill /IM node.exe",
    "powershell Get-Process node | Stop-Process",
    # shared limit (the reference does not match this either): no powershell prefix
    "get-process python | stop-process",
]


@pytest.mark.parametrize("command", BLOCKED)
def test_a_broad_python_kill_is_refused(command):
    assert broad_python_kill_error(command) == BROAD_PYTHON_KILL_ERROR


@pytest.mark.parametrize("command", ALLOWED)
def test_targeted_or_unrelated_commands_pass(command):
    assert broad_python_kill_error(command) is None


def test_the_refusal_says_what_to_do_instead():
    message = broad_python_kill_error("pkill python")
    assert "Eclipse itself" in message
    assert "by its id" in message
    assert "terminal_kill" in message


def test_the_check_is_case_insensitive():
    assert broad_python_kill_error("PKILL PYTHON") == BROAD_PYTHON_KILL_ERROR
    assert broad_python_kill_error("TaskKill /F /IM PYTHON.EXE") == BROAD_PYTHON_KILL_ERROR


def test_a_semicolon_hides_nothing():
    assert broad_python_kill_error("echo ok ; pkill python") == BROAD_PYTHON_KILL_ERROR
    assert broad_python_kill_error("echo ok && true") is None


def test_windows_specific_spellings_are_covered():
    for command in ("taskkill /F /IM Python3.12.exe", "taskkill /IM pythonw.exe"):
        assert broad_python_kill_error(command) == BROAD_PYTHON_KILL_ERROR


def test_an_empty_command_is_not_a_kill():
    assert broad_python_kill_error("") is None
    assert broad_python_kill_error(None) is None


# -- the wiring: the refusal happens BEFORE a process is spawned ----------

def test_run_refuses_without_spawning(tmp_path):
    result = run("pkill python", cwd=tmp_path)
    assert result.exit_code == 126
    assert result.status == "failed"
    assert result.duration_ms == 0                 # nothing ran
    assert "Eclipse itself" in result.stderr
    assert result.timed_out is False


def test_run_still_executes_an_ordinary_command(tmp_path):
    result = run("echo hello-from-the-shell", cwd=tmp_path)
    assert result.exit_code == 0
    assert "hello-from-the-shell" in result.stdout
