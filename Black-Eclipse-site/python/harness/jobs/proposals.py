"""Two-phase proposals for scheduled-job changes.

Creating, replacing or cancelling a scheduled job is a durable side effect, so it takes
two steps: a **proposal** is recorded with an expiry, and the change happens only when
the proposal is **committed**. A proposal that is dropped, expired or superseded leaves
the job store untouched.

The rules, transcribed from Vibe-Trading's `scheduled_research/proposals.py` (MIT):

  * ids are ``srp_`` + 32 lowercase hex characters, and anything else is rejected
    before a path is built (the reference validates the shape *then* resolves it);
  * a proposal lives for **15 minutes** (``TTL_MS``); committing an expired one marks
    it ``expired`` and refuses, so a stale confirmation cannot land later;
  * state is one-way — ``pending`` → ``committed`` | ``discarded`` | ``expired`` — and
    committing an already-committed proposal is **idempotent**, not an error (a
    retried confirmation must not create the job twice);
  * a **system** job (``system:`` prefix or ``source_type == "system"``) can never be
    created, replaced or cancelled by a proposal: those rows are seeded and repaired by
    the scheduler itself, so no user-facing surface may author them.

Pure: the caller supplies the clock, so expiry is testable without sleeping.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Mapping, Optional

__all__ = [
    "PROPOSAL_ID_RE",
    "PROPOSAL_STATUSES",
    "TTL_MS",
    "ProposalError",
    "SystemJobError",
    "commit",
    "discard",
    "is_system_job",
    "new_proposal",
    "validate_proposal_id",
]

#: How long a confirmation stays valid.
TTL_MS = 15 * 60 * 1000

#: The id shape the reference validates before touching the filesystem.
PROPOSAL_ID_RE = re.compile(r"^srp_[0-9a-f]{32}$")

PROPOSAL_STATUSES = ("pending", "committed", "discarded", "expired")

_SYSTEM_PREFIX = "system:"


class ProposalError(ValueError):
    """The proposal cannot be committed, discarded or resolved as asked."""


class SystemJobError(ProposalError):
    """A proposal tried to touch a scheduler-owned system job."""


def validate_proposal_id(proposal_id: str) -> str:
    if not isinstance(proposal_id, str) or not PROPOSAL_ID_RE.fullmatch(proposal_id):
        raise ProposalError("invalid scheduled research proposal id")
    return proposal_id


def is_system_job(job_id: Any, source_type: Any = None) -> bool:
    """Whether ``job_id`` names a scheduler-owned job (never proposal-writable)."""
    if source_type == "system":
        return True
    return isinstance(job_id, str) and job_id.startswith(_SYSTEM_PREFIX)


def _ensure_not_system_job(job_id: Any, source_type: Any, operation: str) -> None:
    if is_system_job(job_id, source_type):
        raise SystemJobError(
            f"scheduled research job {str(job_id)!r} is a protected system job and "
            f"cannot be {operation} by a proposal"
        )


def new_proposal(
    operation: str,
    *,
    now_ms: int,
    session_id: Optional[str] = None,
    job: Optional[Mapping[str, Any]] = None,
    job_id: Optional[str] = None,
    source_type: Optional[str] = None,
) -> dict[str, Any]:
    """Record a pending proposal. Nothing is written to the job store yet."""
    if operation not in ("create", "cancel"):
        raise ProposalError(f"unsupported proposal operation {operation!r}")
    if operation == "create" and job is None:
        raise ProposalError("a create proposal needs the job it would create")
    if operation == "cancel" and not job_id:
        raise ProposalError("a cancel proposal needs the job id it would cancel")

    if job is not None:
        _ensure_not_system_job(job.get("id"), job.get("sourceType"), "created or replaced")
    if job_id:
        _ensure_not_system_job(job_id, source_type, "cancelled")

    return {
        "proposalId": f"srp_{uuid.uuid4().hex}",
        "operation": operation,
        "status": "pending",
        "sessionId": session_id,
        "createdAt": int(now_ms),
        "expiresAt": int(now_ms) + TTL_MS,
        "job": dict(job) if job is not None else None,
        "jobId": job_id,
        "committedJobId": None,
    }


def commit(
    proposal: Mapping[str, Any],
    *,
    now_ms: int,
    executor_ready: bool = True,
    job_exists: bool = True,
) -> dict[str, Any]:
    """Commit a pending proposal and return the updated record.

    Idempotent on ``committed``. Refuses (and marks ``expired``) when the deadline has
    passed, when the executor is not running, and when a cancel's target has vanished.
    """
    record = dict(proposal)
    validate_proposal_id(str(record.get("proposalId")))
    status = record.get("status")
    if status == "committed":
        return record
    if status != "pending":
        raise ProposalError(f"proposal is {status}")
    if int(record.get("expiresAt") or 0) < int(now_ms):
        record["status"] = "expired"
        raise _Expired(record)
    if not executor_ready:
        raise ProposalError("scheduled research executor is not enabled and running")

    operation = record.get("operation")
    if operation == "create":
        job = record.get("job") or {}
        _ensure_not_system_job(job.get("id"), job.get("sourceType"), "created or replaced")
        record["committedJobId"] = job.get("id")
    elif operation == "cancel":
        if not job_exists:
            raise ProposalError("scheduled research job no longer exists")
        _ensure_not_system_job(record.get("jobId"), None, "cancelled")
        record["committedJobId"] = record.get("jobId")
    else:
        raise ProposalError("unsupported proposal operation")

    record["status"] = "committed"
    record["committedAt"] = int(now_ms)
    return record


class _Expired(ProposalError):
    """Carries the marked record so the caller can persist the ``expired`` status."""

    def __init__(self, record: dict[str, Any]) -> None:
        super().__init__("proposal has expired")
        self.record = record


def discard(proposal: Mapping[str, Any], *, now_ms: int) -> dict[str, Any]:
    """Mark a pending proposal discarded; anything else is returned unchanged."""
    record = dict(proposal)
    validate_proposal_id(str(record.get("proposalId")))
    if record.get("status") == "pending":
        record["status"] = "discarded"
        record["discardedAt"] = int(now_ms)
    return record
