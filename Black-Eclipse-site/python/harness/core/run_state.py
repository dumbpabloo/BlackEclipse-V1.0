"""A run's terminal state, and why "cancelled" is not "failed".

A run ends in exactly one of three states, and the distinction is a product decision,
not a cosmetic one: a user stop is not an outage. Reporting a cancellation as a failure
made the reference's Reports view contradict its own chat history, which is the bug this
contract exists to prevent.

Transcribed from Vibe-Trading's `core/state.py` (MIT), minus its directory creation:
`cancelled` is its own status (with a default reason), `failed` requires a reason, and a
`success` may not carry one. The write is flush + `fsync` so a crash cannot leave a
truncated state file that reads as "still running".
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Optional

__all__ = [
    "CANCELLED",
    "FAILED",
    "SUCCESS",
    "TERMINAL_STATUSES",
    "RunStateError",
    "is_terminal",
    "state_payload",
    "write_state",
]

SUCCESS = "success"
FAILED = "failed"
CANCELLED = "cancelled"

#: Every terminal status. A run is expected to end in one of these.
TERMINAL_STATUSES = (SUCCESS, FAILED, CANCELLED)

_DEFAULT_CANCEL_REASON = "cancelled by user"


class RunStateError(ValueError):
    """The requested state is not a legal one."""


def is_terminal(status: Any) -> bool:
    return str(status or "").strip().lower() in TERMINAL_STATUSES


def state_payload(status: Any, reason: Optional[str] = None) -> dict[str, Any]:
    """Build the state record, refusing combinations that would mislead.

    * ``success`` carries no reason — a reason there implies a caveat the status denies;
    * ``failed`` requires one, because a failure nobody can explain is not actionable;
    * ``cancelled`` defaults to "cancelled by user" and is a **distinct** status.
    """
    value = str(status or "").strip().lower()
    if value not in TERMINAL_STATUSES:
        raise RunStateError(
            f"status must be one of {', '.join(TERMINAL_STATUSES)}, got {status!r}"
        )
    text = (reason or "").strip()
    if value == SUCCESS:
        if text:
            raise RunStateError("a successful run carries no reason")
        return {"status": SUCCESS}
    if value == FAILED and not text:
        raise RunStateError("a failed run needs a reason")
    return {"status": value, "reason": text or _DEFAULT_CANCEL_REASON}


def write_state(path: Path | str, payload: Mapping[str, Any]) -> Path:
    """Write the state file durably: encode, flush, `fsync`, then rename.

    The rename is what makes the file always readable as either the old state or the
    new one; the fsync is what makes the new one survive a power cut. A truncated
    state file would read as an unfinished run, which is the opposite of the truth.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(dict(payload), ensure_ascii=False, indent=2).encode("utf-8")
    tmp = target.with_name(target.name + ".tmp")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)
    return target
