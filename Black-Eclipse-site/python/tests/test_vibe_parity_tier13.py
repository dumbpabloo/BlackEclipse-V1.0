"""Branch parity, thirteenth tier: two-phase proposals for scheduled-job changes.

Reference fetched 2026-11-21 (MIT, `HKUDS/Vibe-Trading`,
`agent/src/scheduled_research/proposals.py`). Four rules, all checkable:

* ids are ``srp_`` + 32 hex and are validated **before** any path is built;
* a proposal lives **15 minutes**; committing an expired one marks it ``expired`` and
  refuses, so a stale confirmation cannot land later;
* ``pending`` → ``committed`` / ``discarded`` / ``expired`` is one-way, and committing
  an already-committed proposal is **idempotent** (a retried confirmation must not
  create the job twice);
* a **system** job (``system:`` prefix or ``source_type == "system"``) can never be
  created, replaced or cancelled by a proposal.

Eclipse had **no proposal step at all** for job changes. Ported as a pure state machine
with an injected clock; wiring it into the job tools is the follow-up named in the
register, not something this test claims.
"""

from __future__ import annotations

import re

import pytest

from harness.jobs import proposals as P


CLOCK = 1_700_000_000_000


def _create(**kwargs):
    job = {"id": "job-1", "sourceType": "user", "name": "premarket brief"}
    return P.new_proposal("create", now_ms=CLOCK, job=job, **kwargs)


def _cancel(**kwargs):
    return P.new_proposal("cancel", now_ms=CLOCK, job_id="job-1", **kwargs)


# -- the constants and the id shape --------------------------------------

def test_the_ttl_is_fifteen_minutes():
    assert P.TTL_MS == 15 * 60 * 1000


def test_an_id_is_srp_plus_32_hex():
    proposal = _create()
    assert re.fullmatch(r"srp_[0-9a-f]{32}", proposal["proposalId"])
    assert P.validate_proposal_id(proposal["proposalId"]) == proposal["proposalId"]


@pytest.mark.parametrize("bad", [
    "srp_", "srp_" + "A" * 32, "srp_" + "a" * 31, "srp_" + "a" * 33,
    "../../etc/passwd", "SRP_" + "a" * 32, "", None,
])
def test_an_invalid_id_is_refused_before_any_path_is_built(bad):
    with pytest.raises(P.ProposalError):
        P.validate_proposal_id(bad)  # type: ignore[arg-type]


# -- the state machine ---------------------------------------------------

def test_a_new_proposal_is_pending_and_carries_its_deadline():
    proposal = _create()
    assert proposal["status"] == "pending"
    assert proposal["expiresAt"] == CLOCK + P.TTL_MS
    assert proposal["committedJobId"] is None


def test_commit_marks_it_committed_and_names_the_job():
    proposal = _create()
    committed = P.commit(proposal, now_ms=CLOCK + 1_000)
    assert committed["status"] == "committed"
    assert committed["committedJobId"] == "job-1"
    assert committed["committedAt"] == CLOCK + 1_000
    assert proposal["status"] == "pending"          # input not mutated


def test_commit_is_idempotent_on_an_already_committed_proposal():
    first = P.commit(_create(), now_ms=CLOCK + 1_000)
    again = P.commit(first, now_ms=CLOCK + 2_000)
    assert again == first                            # no second job, no new timestamp


def test_committing_after_the_deadline_refuses_and_marks_expired():
    proposal = _create()
    with pytest.raises(P.ProposalError) as exc:
        P.commit(proposal, now_ms=CLOCK + P.TTL_MS + 1)
    assert "expired" in str(exc.value)
    expired = exc.value.record                       # type: ignore[attr-defined]
    assert expired["status"] == "expired"


def test_exactly_at_the_deadline_it_still_commits():
    proposal = _create()
    assert P.commit(proposal, now_ms=CLOCK + P.TTL_MS)["status"] == "committed"


def test_a_discarded_proposal_cannot_be_committed():
    discarded = P.discard(_create(), now_ms=CLOCK + 10)
    assert discarded["status"] == "discarded"
    with pytest.raises(P.ProposalError) as exc:
        P.commit(discarded, now_ms=CLOCK + 20)
    assert "discarded" in str(exc.value)


def test_discarding_a_committed_proposal_is_a_no_op():
    committed = P.commit(_create(), now_ms=CLOCK + 1)
    assert P.discard(committed, now_ms=CLOCK + 2) == committed


def test_the_executor_must_be_running_to_commit():
    with pytest.raises(P.ProposalError) as exc:
        P.commit(_create(), now_ms=CLOCK + 1, executor_ready=False)
    assert "executor" in str(exc.value)


def test_a_cancel_needs_its_target_to_still_exist():
    with pytest.raises(P.ProposalError) as exc:
        P.commit(_cancel(), now_ms=CLOCK + 1, job_exists=False)
    assert "no longer exists" in str(exc.value)
    assert P.commit(_cancel(), now_ms=CLOCK + 1)["committedJobId"] == "job-1"


# -- system jobs are untouchable -----------------------------------------

@pytest.mark.parametrize("job_id,source", [
    ("system:br-data-sync", "user"),
    ("job-1", "system"),
    ("system:anything", None),
])
def test_a_system_job_cannot_be_created(job_id, source):
    with pytest.raises(P.SystemJobError):
        P.new_proposal("create", now_ms=CLOCK,
                       job={"id": job_id, "sourceType": source})


@pytest.mark.parametrize("job_id", ["system:br-data-sync", "system:valuation-refresh"])
def test_a_system_job_cannot_be_cancelled(job_id):
    with pytest.raises(P.SystemJobError):
        P.new_proposal("cancel", now_ms=CLOCK, job_id=job_id)


def test_the_protection_is_rechecked_at_commit_not_only_at_proposal():
    """A hand-edited proposal file must not smuggle a system job through."""
    proposal = _create()
    proposal["job"] = {"id": "system:br-data-sync", "sourceType": "user"}
    with pytest.raises(P.SystemJobError):
        P.commit(proposal, now_ms=CLOCK + 1)


def test_is_system_job_is_the_single_predicate():
    assert P.is_system_job("system:x") is True
    assert P.is_system_job("job-1", "system") is True
    assert P.is_system_job("job-1", "user") is False
    assert P.is_system_job(None) is False


# -- bad input ------------------------------------------------------------

def test_an_unknown_operation_is_refused():
    with pytest.raises(P.ProposalError):
        P.new_proposal("replace", now_ms=CLOCK)


def test_a_create_without_a_job_is_refused():
    with pytest.raises(P.ProposalError):
        P.new_proposal("create", now_ms=CLOCK)


def test_a_cancel_without_an_id_is_refused():
    with pytest.raises(P.ProposalError):
        P.new_proposal("cancel", now_ms=CLOCK)


def test_the_status_vocabulary_is_the_references():
    assert set(P.PROPOSAL_STATUSES) == {"pending", "committed", "discarded", "expired"}


# -- the wiring: bot_delete now needs a committed proposal ----------------

class _StubService:
    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.memory = None

    def delete(self, bot_id: str) -> bool:
        self.deleted.append(bot_id)
        return True


def _run_delete(monkeypatch, args):
    import asyncio

    from harness.automation import tools as T

    service = _StubService()
    monkeypatch.setattr(T, "_require_service", lambda: service)
    text, data = asyncio.run(T._delete(args, None))
    return text, data, service


def test_a_delete_without_confirmation_returns_a_15_minute_proposal(monkeypatch):
    text, data, service = _run_delete(monkeypatch, {"botId": "bot-1"})
    assert data["deleted"] is False
    assert data["needsConfirmation"] is True
    assert re.fullmatch(r"srp_[0-9a-f]{32}", data["proposalId"])
    assert data["expiresAt"] - int(__import__("time").time() * 1000) <= P.TTL_MS
    assert service.deleted == []                       # nothing removed yet
    assert "within 15 minutes" in text


def test_the_confirmation_actually_deletes(monkeypatch):
    _text, pending, _svc = _run_delete(monkeypatch, {"botId": "bot-2"})
    _text2, done, service = _run_delete(
        monkeypatch, {"botId": "bot-2", "confirm": True, "proposalId": pending["proposalId"]}
    )
    assert done["deleted"] is True
    assert service.deleted == ["bot-2"]


def test_a_proposal_cannot_be_used_twice(monkeypatch):
    _t, pending, _svc = _run_delete(monkeypatch, {"botId": "bot-3"})
    args = {"botId": "bot-3", "confirm": True, "proposalId": pending["proposalId"]}
    _t1, first, first_service = _run_delete(monkeypatch, args)
    _text, again, second_service = _run_delete(monkeypatch, args)
    assert first["deleted"] is True and first_service.deleted == ["bot-3"]
    assert again["deleted"] is False
    assert again["reason"] == "unknown_proposal"
    assert second_service.deleted == []                # the retry removes nothing


def test_a_proposal_for_another_bot_is_refused(monkeypatch):
    _t, pending, _svc = _run_delete(monkeypatch, {"botId": "bot-4"})
    _text, data, service = _run_delete(
        monkeypatch, {"botId": "bot-5", "confirm": True, "proposalId": pending["proposalId"]}
    )
    assert data["deleted"] is False and data["reason"] == "proposal_mismatch"
    assert service.deleted == []


def test_a_system_job_can_never_be_deleted(monkeypatch):
    _text, data, service = _run_delete(
        monkeypatch, {"botId": "system:br-data-sync", "confirm": True, "proposalId": "x"}
    )
    assert data["deleted"] is False and data["reason"] == "system_job"
    assert service.deleted == []


def test_an_unknown_proposal_id_is_refused_with_a_way_forward(monkeypatch):
    text, data, service = _run_delete(
        monkeypatch, {"botId": "bot-6", "confirm": True, "proposalId": "srp_" + "0" * 32}
    )
    assert data["reason"] == "unknown_proposal"
    assert "fresh proposal" in text
    assert service.deleted == []
