"""Refuse shell commands that would kill every Python process by name.

The harness runs in Python. ``pkill python``, ``taskkill /IM python.exe`` or
PowerShell's ``Stop-Process -Name python`` therefore kills **the harness itself**
mid-run — the command appears to succeed and then the agent's own process is gone,
which is indistinguishable from a crash. There is no legitimate use for that in an
agent turn: a specific background task is cancelled by id, not by name.

This is a transcription of Vibe-Trading's `tools/_shell_safety.py` (MIT), including
that the check is per shell SEGMENT (``;``, a bare ``&``, CR/LF), so
``echo hi && pkill python`` is caught; quoted text is flattened so quoting does not
smuggle the flag; and the refusal tells the caller what to do instead.
"""

from __future__ import annotations

import re

__all__ = ["BROAD_PYTHON_KILL_ERROR", "broad_python_kill_error"]

_PYTHON_PROCESS = r"python(?:w|\d+(?:\.\d+)*)?(?:\.exe)?\b"

_TASKKILL_PYTHON = re.compile(
    rf"\btaskkill(?:\.exe)?\b"
    rf"[^;&\r\n]*(?:/im\s+{_PYTHON_PROCESS}|get-process\s+"
    rf"(?:-name\s+)?{_PYTHON_PROCESS})",
    re.IGNORECASE,
)
_UNIX_PYTHON_KILL = re.compile(
    rf"^(?:sudo\s+)?(?:pkill|killall)\b[^;&\r\n]*{_PYTHON_PROCESS}",
    re.IGNORECASE,
)
_POWERSHELL_PREFIX = re.compile(r"^(?:powershell|pwsh)(?:\.exe)?\b", re.IGNORECASE)
_POWERSHELL_PYTHON_BY_NAME = re.compile(
    rf"\bstop-process\b[^;&\r\n]*-name\s+{_PYTHON_PROCESS}",
    re.IGNORECASE,
)
_POWERSHELL_PYTHON_PIPE = re.compile(
    rf"\bget-process\s+(?:-name\s+)?{_PYTHON_PROCESS}"
    r"[^;&\r\n]*\|\s*stop-process\b",
    re.IGNORECASE,
)

#: The refusal, worded so the caller can act on it.
BROAD_PYTHON_KILL_ERROR = (
    "Broad Python process termination is blocked because it would terminate Eclipse "
    "itself. Cancel the specific background task by its id instead "
    "(terminal_kill / code_background_cancel), or narrow the pattern."
)


def broad_python_kill_error(command: str) -> str | None:
    """Return an actionable error when a command kills Python broadly by name.

    Per-segment, because ``a && pkill python`` must be caught as surely as the bare
    command. ``None`` means no broad kill was found — this is a narrow refusal, not a
    general command policy (that lives in ``code/policy.py``).
    """
    for segment in re.split(r"[;\r\n]|(?<![<>])&(?![<>])", str(command or "")):
        normalized = segment.strip().replace('"', " ").replace("'", " ")
        normalized = re.sub(r"\s+", " ", normalized)
        if _TASKKILL_PYTHON.search(normalized):
            return BROAD_PYTHON_KILL_ERROR
        if _UNIX_PYTHON_KILL.search(normalized):
            return BROAD_PYTHON_KILL_ERROR
        if _POWERSHELL_PREFIX.search(normalized) and (
            _POWERSHELL_PYTHON_BY_NAME.search(normalized)
            or _POWERSHELL_PYTHON_PIPE.search(normalized)
        ):
            return BROAD_PYTHON_KILL_ERROR
    return None
