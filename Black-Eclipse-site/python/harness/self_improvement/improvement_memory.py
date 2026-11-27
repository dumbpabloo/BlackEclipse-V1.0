"""Self-Improvement Memory (SIM): the harness optimizer's own experience ledger.

Eclipse already has memory for the *task-solving agent* (`eclipse_memory/`) and
outcome scoring for *lessons* (`lesson_feedback.py`). This module is a third,
narrower thing: a durable, problem-keyed record of changes the harness
optimizer itself attempted, what each attempt predicted, what was actually
observed, and in which setting. It implements the transferable mechanism from
*Self-Improvement Memory Enables Continual Recursive Self-Improvement*
(arXiv:2611.18856): when the model or the benchmark changes, the evolved harness
alone does not say which parts to keep, revise or remove — the improvement
record does.

Design rules, in the same spirit as the rest of RSL:

* **Evidence, never authority.** SIM cannot promote, enable, land or reorder a
  decision by itself. It is text a proposer/selector may read.
* **No fabricated measurement.** A missing score is `unmeasured`; a missing
  prediction is `no prediction recorded`; a prediction with no observed task
  result stays `unresolved`. Nothing is defaulted to zero.
* **Append-only.** A returning edit is recorded even when it regresses or is
  rejected, and merging two problems keeps every attempt and the old id as an
  alias.
* **Bounded and honest about it.** `render()` is deterministic, newest-first and
  character-budgeted; an omission is stated, never silent.
* **One portable file.** A JSON ledger, written atomically, loadable on another
  run or machine. No second database, no model call, no background task.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

from ..persistence import now_iso

MEMORY_VERSION = 1
OUTCOMES: tuple[str, ...] = ("improved", "regressed", "flat", "unmeasured")
UNMEASURED = "unmeasured"

MAX_NAME_CHARS = 300
MAX_EDIT_CHARS = 2_000
MAX_RATIONALE_CHARS = 2_000
MAX_TASKS = 40
MAX_TASK_CHARS = 160
MAX_PROBLEMS = 200
MAX_ATTEMPTS_PER_PROBLEM = 50
DEFAULT_RENDER_CHARS = 6_000

MEMORY_ENV = "HARNESS_RSL_MEMORY"
MEMORY_PATH_ENV = "HARNESS_RSL_MEMORY_PATH"
MEMORY_CHARS_ENV = "HARNESS_RSL_MEMORY_CHARS"


def _clean(text: Any, limit: int) -> str:
    value = " ".join(str(text or "").split())
    return value[:limit]


def _normalise(text: Any) -> str:
    """The identity key: whitespace/case-normalised root-cause text.

    Exact-normalised only. The paper lets the proposer decide identity and
    merges identical root-cause text; we do the same and nothing fuzzier, so a
    merge is always explainable.
    """
    return " ".join(str(text or "").strip().lower().split())


def _task_keys(values: Any) -> tuple[str, ...]:
    if not values:
        return ()
    if isinstance(values, (str, bytes)):
        values = [values]
    seen: list[str] = []
    for item in values:
        key = _clean(item, MAX_TASK_CHARS)
        if key and key not in seen:
            seen.append(key)
    return tuple(seen[:MAX_TASKS])


def _number(value: Any) -> Optional[float]:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def compute_outcome(
    *, passed: int, total: int, score_before: Any, score_after: Any,
) -> str:
    """The observed outcome, or `unmeasured` when nothing was measured.

    An unmeasured attempt is never `flat`: a missing value is not a zero delta.
    """
    if int(total) <= 0:
        return UNMEASURED
    before = _number(score_before)
    after = _number(score_after)
    if before is not None and after is not None:
        if after > before:
            return "improved"
        if after < before:
            return "regressed"
        return "flat"
    # Count-only evidence: compare the pass ratio against a full pass.
    ratio = float(passed) / float(total)
    if ratio >= 1.0:
        return "improved"
    if passed <= 0:
        return "regressed"
    return "flat"


def score_prediction(
    predicted: Sequence[str], fixed: Sequence[str], missed: Sequence[str],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Split predictions into fixed / missed / unresolved, preserving order.

    Only explicit evidence decides a prediction. A prediction that nobody
    recorded an observation for stays unresolved; it is never assumed fixed.
    """
    fixed_set = {_normalise(item) for item in fixed}
    missed_set = {_normalise(item) for item in missed}
    resolved: list[str] = []
    unresolved: list[str] = []
    fixed_out: list[str] = []
    missed_out: list[str] = []
    for item in _task_keys(predicted):
        key = _normalise(item)
        if key in fixed_set:
            fixed_out.append(item)
            resolved.append(key)
        elif key in missed_set:
            missed_out.append(item)
            resolved.append(key)
        else:
            unresolved.append(item)
    return tuple(fixed_out), tuple(missed_out), tuple(unresolved)


@dataclass(frozen=True)
class Provenance:
    """Where and when an attempt happened. This is what makes reuse decidable."""

    domain: str = "global"
    model: str = ""
    round: int = 0
    runId: str = ""
    candidateId: str = ""
    stateDigest: str = ""
    createdAt: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "domain", _clean(self.domain, 120) or "global")
        object.__setattr__(self, "model", _clean(self.model, 160))
        object.__setattr__(self, "round", max(0, int(self.round or 0)))
        object.__setattr__(self, "runId", _clean(self.runId, 120))
        object.__setattr__(self, "candidateId", _clean(self.candidateId, 120))
        object.__setattr__(self, "stateDigest", _clean(self.stateDigest, 128))
        object.__setattr__(self, "createdAt", _clean(self.createdAt, 40))

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "model": self.model,
            "round": int(self.round),
            "runId": self.runId,
            "candidateId": self.candidateId,
            "stateDigest": self.stateDigest,
            "createdAt": self.createdAt,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "Provenance":
        data = payload if isinstance(payload, Mapping) else {}
        return cls(
            domain=str(data.get("domain") or "global"),
            model=str(data.get("model") or ""),
            round=int(data.get("round") or 0),
            runId=str(data.get("runId") or ""),
            candidateId=str(data.get("candidateId") or ""),
            stateDigest=str(data.get("stateDigest") or ""),
            createdAt=str(data.get("createdAt") or ""),
        )


@dataclass(frozen=True)
class Attempt:
    """One recorded change: what was tried, why, what was expected, what happened."""

    id: str
    edit: str
    rationale: str = ""
    operator: str = "unknown"
    predicted: tuple[str, ...] = ()
    fixed: tuple[str, ...] = ()
    missed: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    passed: int = 0
    total: int = 0
    scoreBefore: Optional[float] = None
    scoreAfter: Optional[float] = None
    accepted: bool = False
    outcome: str = UNMEASURED
    provenance: Provenance = field(default_factory=Provenance)

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise ValueError(f"unknown attempt outcome {self.outcome!r}")
        object.__setattr__(self, "edit", _clean(self.edit, MAX_EDIT_CHARS))
        object.__setattr__(self, "rationale", _clean(self.rationale, MAX_RATIONALE_CHARS))
        object.__setattr__(self, "operator", _clean(self.operator, 60) or "unknown")
        object.__setattr__(self, "predicted", _task_keys(self.predicted))
        object.__setattr__(self, "fixed", _task_keys(self.fixed))
        object.__setattr__(self, "missed", _task_keys(self.missed))
        object.__setattr__(self, "unresolved", _task_keys(self.unresolved))
        object.__setattr__(self, "passed", max(0, int(self.passed or 0)))
        object.__setattr__(self, "total", max(0, int(self.total or 0)))
        object.__setattr__(self, "scoreBefore", _number(self.scoreBefore))
        object.__setattr__(self, "scoreAfter", _number(self.scoreAfter))
        object.__setattr__(self, "accepted", bool(self.accepted))

    @property
    def measured(self) -> bool:
        return self.outcome != UNMEASURED

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "edit": self.edit,
            "rationale": self.rationale,
            "operator": self.operator,
            "predicted": list(self.predicted),
            "fixed": list(self.fixed),
            "missed": list(self.missed),
            "unresolved": list(self.unresolved),
            "passed": int(self.passed),
            "total": int(self.total),
            "scoreBefore": self.scoreBefore,
            "scoreAfter": self.scoreAfter,
            "accepted": self.accepted,
            "outcome": self.outcome,
            "provenance": self.provenance.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any], *, attempt_id: str = "") -> "Attempt":
        predicted = _task_keys(payload.get("predicted"))
        fixed = _task_keys(payload.get("fixed"))
        missed = _task_keys(payload.get("missed"))
        computed_fixed, computed_missed, computed_unresolved = score_prediction(
            predicted,
            [*fixed, *_task_keys(payload.get("resolvedFixed"))],
            [*missed, *_task_keys(payload.get("resolvedMissed"))],
        )
        supplied_unresolved = _task_keys(payload.get("unresolved"))
        unresolved = supplied_unresolved or computed_unresolved
        return cls(
            id=str(payload.get("id") or attempt_id),
            edit=str(payload.get("edit") or ""),
            rationale=str(payload.get("rationale") or ""),
            operator=str(payload.get("operator") or "unknown"),
            predicted=predicted,
            fixed=computed_fixed,
            missed=computed_missed,
            unresolved=unresolved,
            passed=int(payload.get("passed") or 0),
            total=int(payload.get("total") or 0),
            scoreBefore=payload.get("scoreBefore"),
            scoreAfter=payload.get("scoreAfter"),
            accepted=bool(payload.get("accepted")),
            outcome=str(payload.get("outcome") or UNMEASURED),
            provenance=Provenance.from_dict(payload.get("provenance")),
        )


def _problem_order(problem: Problem) -> tuple[int, int]:
    """Newest activity first, with a numeric tie-break (P10 after P9)."""
    last_round = problem.attempts[-1].provenance.round if problem.attempts else problem.opened.round
    digits = "".join(char for char in problem.id if char.isdigit())
    return (int(last_round), int(digits or 0))


@dataclass
class Problem:
    """A recurring harness problem, with every attempt kept in order."""

    id: str
    name: str
    opened: Provenance = field(default_factory=Provenance)
    attempts: list[Attempt] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.name = _clean(self.name, MAX_NAME_CHARS) or "unnamed harness problem"

    @property
    def last(self) -> Optional[Attempt]:
        return self.attempts[-1] if self.attempts else None

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "opened": self.opened.to_dict(),
            "attempts": [attempt.to_dict() for attempt in self.attempts],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Problem":
        problem_id = str(payload.get("id") or "")
        attempts = [
            Attempt.from_dict(item, attempt_id=f"{problem_id}.A{index + 1}")
            for index, item in enumerate(payload.get("attempts") or [])
            if isinstance(item, Mapping)
        ]
        return cls(
            id=problem_id,
            name=str(payload.get("name") or ""),
            opened=Provenance.from_dict(payload.get("opened")),
            attempts=attempts,
        )


@dataclass(frozen=True)
class MemoryConfig:
    """The opt-in knob. Off by default; nothing else in RSL changes when off."""

    enabled: bool = False
    path: str = ""
    max_chars: int = DEFAULT_RENDER_CHARS

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "MemoryConfig":
        env = os.environ if environ is None else environ
        raw = str(env.get(MEMORY_ENV) or "").strip().lower()
        enabled = raw in ("1", "true", "yes", "on", "enable", "enabled")
        path = str(env.get(MEMORY_PATH_ENV) or "").strip()
        try:
            chars = int(str(env.get(MEMORY_CHARS_ENV) or DEFAULT_RENDER_CHARS))
        except (TypeError, ValueError):
            chars = DEFAULT_RENDER_CHARS
        return cls(
            enabled=enabled,
            path=os.path.abspath(os.path.expanduser(path)) if path else "",
            max_chars=max(800, min(20_000, chars)),
        )

    @property
    def resolved_path(self) -> str:
        if self.path:
            return self.path
        from ..eclipse_memory.config import repo_root  # noqa: PLC0415 - light import

        return os.path.join(
            repo_root(), ".black-eclipse", "self-improvement", "memory.json",
        )


class ImprovementMemory:
    """The ledger. Append-only, merge-preserving, serialisable, inert."""

    def __init__(
        self,
        *,
        problems: Optional[Iterable[Problem]] = None,
        next_problem: int = 1,
        next_attempt: int = 1,
        aliases: Optional[Mapping[str, str]] = None,
        carried_from: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.problems: list[Problem] = list(problems or [])
        self.next_problem = max(1, int(next_problem))
        self.next_attempt = max(1, int(next_attempt))
        self.aliases: dict[str, str] = dict(aliases or {})
        self.carried_from: Optional[dict[str, Any]] = (
            dict(carried_from) if carried_from else None
        )

    # -- identity ---------------------------------------------------------
    def _new_problem_id(self) -> str:
        problem_id = f"P{self.next_problem}"
        self.next_problem += 1
        return problem_id

    def _new_attempt_id(self, problem_id: str) -> str:
        attempt_id = f"{problem_id}.A{self.next_attempt}"
        self.next_attempt += 1
        return attempt_id

    def find(self, problem: Any) -> Optional[Problem]:
        """Resolve an id, an alias, or exact-normalised root-cause text."""
        key = str(problem or "").strip()
        if not key:
            return None
        key = self.aliases.get(key, key)
        for item in self.problems:
            if item.id == key:
                return item
        wanted = _normalise(key)
        for item in self.problems:
            if _normalise(item.name) == wanted:
                return item
        return None

    def open_problem(self, name: Any, provenance: Provenance) -> Problem:
        """Return the matching problem or create one. Identical text merges."""
        existing = self.find(name)
        if existing is not None:
            return existing
        problem = Problem(
            id=self._new_problem_id(),
            name=_clean(name, MAX_NAME_CHARS) or "unnamed harness problem",
            opened=provenance,
        )
        self.problems.append(problem)
        self._enforce_limits()
        return problem

    def merge(self, alias_id: str, into_id: str) -> Problem:
        """Merge `alias_id` into `into_id`, keeping every attempt and the alias."""
        alias = self.find(alias_id)
        target = self.find(into_id)
        if alias is None or target is None:
            raise KeyError("both problems must exist to merge")
        if alias.id == target.id:
            return target
        target.attempts.extend(alias.attempts)
        target.attempts.sort(key=lambda item: (item.provenance.round, item.id))
        self.problems = [item for item in self.problems if item.id != alias.id]
        self.aliases[alias.id] = target.id
        # Keep transitive aliases pointing at the surviving problem.
        for key, value in list(self.aliases.items()):
            if value == alias.id:
                self.aliases[key] = target.id
        return target

    # -- recording --------------------------------------------------------
    def record(
        self,
        problem: Any,
        *,
        edit: Any,
        rationale: Any = "",
        operator: str = "unknown",
        provenance: Optional[Provenance] = None,
        predicted: Sequence[str] = (),
        fixed: Sequence[str] = (),
        missed: Sequence[str] = (),
        passed: int = 0,
        total: int = 0,
        score_before: Any = None,
        score_after: Any = None,
        accepted: bool = False,
        outcome: Optional[str] = None,
    ) -> Attempt:
        """Append one attempt. A recognised problem is reused, else opened."""
        where = provenance or Provenance(createdAt=now_iso())
        entry = self.find(problem) or self.open_problem(problem, where)
        if not where.createdAt:
            where = Provenance(
                domain=where.domain, model=where.model, round=where.round,
                runId=where.runId, candidateId=where.candidateId,
                stateDigest=where.stateDigest, createdAt=now_iso(),
            )
        predicted_keys = _task_keys(predicted)
        fixed_keys, missed_keys, unresolved = score_prediction(
            predicted_keys, fixed, missed,
        )
        resolved = compute_outcome(
            passed=passed, total=total,
            score_before=score_before, score_after=score_after,
        )
        attempt = Attempt(
            id=self._new_attempt_id(entry.id),
            edit=str(edit or ""),
            rationale=str(rationale or ""),
            operator=operator,
            predicted=predicted_keys,
            fixed=fixed_keys,
            missed=missed_keys,
            unresolved=unresolved,
            passed=int(passed or 0),
            total=int(total or 0),
            scoreBefore=score_before,
            scoreAfter=score_after,
            accepted=accepted,
            outcome=outcome or resolved,
            provenance=where,
        )
        entry.attempts.append(attempt)
        entry.attempts.sort(key=lambda item: (item.provenance.round, item.id))
        if len(entry.attempts) > MAX_ATTEMPTS_PER_PROBLEM:
            # Keep the newest attempts; the cut is visible in `stats`/`render`.
            entry.attempts = entry.attempts[-MAX_ATTEMPTS_PER_PROBLEM:]
        self._enforce_limits()
        return attempt

    def record_result(
        self,
        problem: Any,
        *,
        edit: Any,
        rationale: Any = "",
        operator: str = "unknown",
        provenance: Optional[Provenance] = None,
        predicted: Sequence[str] = (),
        fixed: Sequence[str] = (),
        missed: Sequence[str] = (),
        passed: int = 0,
        total: int = 0,
        score_before: Any = None,
        score_after: Any = None,
        accepted: bool = False,
    ) -> Attempt:
        """`record` with the outcome derived strictly from the observation."""
        return self.record(
            problem, edit=edit, rationale=rationale, operator=operator,
            provenance=provenance, predicted=predicted, fixed=fixed,
            missed=missed, passed=passed, total=total,
            score_before=score_before, score_after=score_after,
            accepted=accepted,
        )

    def _enforce_limits(self) -> None:
        if len(self.problems) <= MAX_PROBLEMS:
            return
        # Drop the least recently touched problems first. Aliases to a dropped
        # problem stay (they are history), but the entry itself is gone.
        ordered = sorted(self.problems, key=_problem_order)
        keep = {item.id for item in ordered[-MAX_PROBLEMS:]}
        self.problems = [item for item in self.problems if item.id in keep]

    # -- transfer ---------------------------------------------------------
    def carry_from(
        self,
        source: "ImprovementMemory",
        *,
        domain: str,
        model: str = "",
        round: int = 0,
    ) -> "ImprovementMemory":
        """Adopt another run's ledger as prior experience and mark the carry.

        Same mechanism as the paper's `carried_from`: everything is retained,
        and the provenance of the new setting is recorded so later readers can
        tell source evidence from target evidence.
        """
        merged = ImprovementMemory.from_dict(source.to_dict())
        # The source provenance is kept on every entry: a reader must be able
        # to tell source evidence from evidence produced in the target setting.
        merged.carried_from = {
            "domain": _clean(domain, 120) or "global",
            "model": _clean(model, 160),
            "round": max(0, int(round)),
            "problems": len(merged.problems),
        }
        return merged

    # -- reporting --------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        attempts = [a for p in self.problems for a in p.attempts]
        measured = [a for a in attempts if a.measured]
        predictions = [a for a in attempts if a.predicted]
        resolved_predictions = [a for a in predictions if a.fixed or a.missed]
        fixed = sum(len(a.fixed) for a in predictions)
        missed = sum(len(a.missed) for a in predictions)
        scored = fixed + missed
        return {
            "problems": len(self.problems),
            "attempts": len(attempts),
            "measured": len(measured),
            "accepted": sum(1 for a in attempts if a.accepted),
            "improved": sum(1 for a in attempts if a.outcome == "improved"),
            "regressed": sum(1 for a in attempts if a.outcome == "regressed"),
            "flat": sum(1 for a in attempts if a.outcome == "flat"),
            "unmeasured": sum(1 for a in attempts if a.outcome == UNMEASURED),
            "predictions": len(predictions),
            "predictionsResolved": len(resolved_predictions),
            "predictionHitRate": (fixed / scored) if scored else None,
            "carriedFrom": self.carried_from,
        }

    def _problem_line(self, problem: Problem) -> str:
        last = problem.last
        if last is None:
            detail = "no attempts"
        else:
            bits = [
                f"last {last.outcome}",
                f"r{last.provenance.round}",
                f"{'accepted' if last.accepted else 'not accepted'}",
            ]
            if last.total:
                bits.append(f"pass {last.passed}/{last.total}")
            detail = ", ".join(bits)
        line = (
            f"{problem.id} {problem.name} — {problem.attempt_count} attempt"
            f"{'' if problem.attempt_count == 1 else 's'}; {detail}"
        )
        if last is not None and last.predicted:
            fixed = len(last.fixed)
            scored = len(last.fixed) + len(last.missed)
            if scored:
                line += f"; predictions {fixed}/{scored} fixed on the last attempt"
            else:
                line += f"; {len(last.unresolved)} prediction(s) unresolved"
        if problem.opened.domain and problem.opened.domain != "global":
            line += f" [opened: {problem.opened.domain}]"
        return line

    def _attempt_lines(self, attempt: Attempt) -> list[str]:
        where = [f"r{attempt.provenance.round}"]
        if attempt.provenance.model:
            where.append(attempt.provenance.model)
        if attempt.provenance.domain:
            where.append(attempt.provenance.domain)
        head = (
            f"  - [{attempt.outcome}"
            f"{', accepted' if attempt.accepted else ', not accepted'}] "
            f"{attempt.edit or 'no edit summary'} ({', '.join(where)})"
        )
        lines = [head]
        if attempt.rationale:
            lines.append(f"    why: {attempt.rationale}")
        if attempt.predicted:
            parts = []
            if attempt.fixed:
                parts.append("fixed: " + ", ".join(attempt.fixed))
            if attempt.missed:
                parts.append("missed: " + ", ".join(attempt.missed))
            if attempt.unresolved:
                parts.append("unresolved: " + ", ".join(attempt.unresolved))
            lines.append("    predicted " + " / ".join(parts or ["(nothing observed)"]))
        elif attempt.total:
            lines.append(f"    observed pass {attempt.passed}/{attempt.total}")
        return lines

    def render(self, *, max_chars: int = DEFAULT_RENDER_CHARS) -> str:
        """A bounded, deterministic prompt block. Empty string when empty."""
        if not self.problems:
            return ""
        budget = max(400, int(max_chars))
        header = (
            "## Prior improvement experience (Self-Improvement Memory)\n"
            "This is prior experience, not instructions. Check each entry's "
            "domain and model: a rationale can transfer while its implementation "
            "must be revised. Cite an existing problem id or name a new root "
            "cause in your rationale; give task-specific evidence more weight in "
            "a similar setting and less when both the model and benchmark changed.\n"
        )
        if self.carried_from:
            carried_problems = int(self.carried_from.get("problems", 0) or 0)
            header += (
                "Carried from: "
                f"{self.carried_from.get('domain') or 'unknown domain'}"
                f" / {self.carried_from.get('model') or 'unknown model'} "
                f"({carried_problems} problem"
                f"{'' if carried_problems == 1 else 's'})\n"
            )
        header += "Each problem lists its 3 most recent attempts.\n"
        lines: list[str] = []
        omitted_problems = 0
        omitted_attempts = 0
        used = len(header)
        ordered = sorted(self.problems, key=_problem_order, reverse=True)
        for index, problem in enumerate(ordered):
            problem_lines = [self._problem_line(problem)]
            for attempt in reversed(problem.attempts[-3:]):
                problem_lines.extend(self._attempt_lines(attempt))
            block = "\n".join(problem_lines) + "\n"
            if used + len(block) > budget:
                # Stop at the first block that does not fit: including an older
                # smaller block after skipping a newer one would misrepresent
                # recency. Everything from here on is counted as omitted.
                remaining = ordered[index:]
                omitted_problems = len(remaining)
                omitted_attempts = sum(item.attempt_count for item in remaining)
                break
            lines.append(block)
            used += len(block)
        if not lines:
            # Not even one problem fits: keep the newest problem's headline.
            newest = ordered[0]
            fallback = self._problem_line(newest)[: max(80, budget - used - 60)]
            lines.append(fallback + "\n")
            used += len(fallback) + 1
            omitted_problems = max(0, len(ordered) - 1)
            omitted_attempts = max(0, newest.attempt_count - 1)
        body = header + "".join(lines)
        if omitted_problems or omitted_attempts:
            body += (
                f"(Truncated: {omitted_problems} older problem(s) and "
                f"{omitted_attempts} attempt(s) omitted to fit the memory "
                "budget. The full ledger is on disk.)\n"
            )
        return body

    # -- persistence ------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "version": MEMORY_VERSION,
            "next_id": self.next_problem,
            "next_attempt_id": self.next_attempt,
            "carried_from": self.carried_from,
            "aliases": dict(self.aliases),
            "problems": [problem.to_dict() for problem in self.problems],
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "ImprovementMemory":
        data = payload if isinstance(payload, Mapping) else {}
        problems = [
            Problem.from_dict(item)
            for item in data.get("problems") or []
            if isinstance(item, Mapping)
        ]
        aliases = data.get("aliases")
        carried = data.get("carried_from")
        return cls(
            problems=problems,
            next_problem=int(data.get("next_id") or 1),
            next_attempt=int(data.get("next_attempt_id") or 1),
            aliases=aliases if isinstance(aliases, Mapping) else None,
            carried_from=carried if isinstance(carried, Mapping) else None,
        )

    @classmethod
    def load(cls, path: str) -> "ImprovementMemory":
        """Load the ledger. A missing file is an empty ledger, never an error.

        A malformed file is a named error: silently starting empty would look
        like "no history" and lose the evidence the operator is relying on.
        """
        if not path or not os.path.exists(path):
            return cls()
        with open(path, "r", encoding="utf-8") as handle:
            try:
                payload = json.load(handle)
            except json.JSONDecodeError as exc:
                raise ValueError(f"SIM_MEMORY_CORRUPT: {exc}") from exc
        return cls.from_dict(payload)

    def save(self, path: str) -> str:
        """Write atomically (temp file in the same directory + `os.replace`)."""
        target = os.path.abspath(path)
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        descriptor, temp = tempfile.mkstemp(
            prefix=".memory-", suffix=".tmp", dir=os.path.dirname(target) or ".",
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(self.to_dict(), handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, target)
        except Exception:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise
        return target


def load_memory(config: Optional[MemoryConfig] = None) -> Optional[ImprovementMemory]:
    """Load the configured ledger, or `None` when SIM is off.

    A corrupt file is reported by raising; callers that must not fail a climb
    treat that as "no memory" and surface the reason.
    """
    settings = config or MemoryConfig.from_env()
    if not settings.enabled:
        return None
    return ImprovementMemory.load(settings.resolved_path)


def attempt_edit_summary(record: Mapping[str, Any]) -> str:
    """A bounded, honest summary of what a hill-climb neighbour changed."""
    ops = record.get("ops") or record.get("proposedOps")
    if isinstance(ops, (list, tuple)) and ops:
        parts: list[str] = []
        for op in ops[:6]:
            if isinstance(op, Mapping):
                head = str(op.get("op") or "edit").strip()
                path = str(op.get("path") or op.get("targetPath") or "").strip()
                parts.append(f"{head} {path}".strip())
        if parts:
            return "; ".join(parts)
    for key in ("changeSummary", "description", "summary"):
        value = str(record.get(key) or "").strip()
        if value:
            return value
    digest = str(record.get("snapshotDigest") or record.get("digest") or "")
    if digest:
        return f"snapshot {digest[:12]}"
    return f"candidate {record.get('id') or 'unknown'}"


def _record_neighbor(
    memory: ImprovementMemory, record: Mapping[str, Any],
    *, scope: str, round_index: int,
) -> Attempt:
    """Map one evaluated hill-climb record onto a memory attempt.

    Utility is the ranking signal the acceptance rule used, so the remembered
    score delta is the same delta the climb decided on. Counts are read from the
    validation aggregates; an absent aggregate stays `unmeasured`.
    """
    validation = record.get("validation")
    aggregates: Mapping[str, Any] = {}
    if isinstance(validation, Mapping):
        raw = validation.get("aggregates")
        aggregates = raw if isinstance(raw, Mapping) else validation
    try:
        passed = int(aggregates.get("successes") or 0)
    except (TypeError, ValueError):
        passed = 0
    try:
        total = int(aggregates.get("sampleSize") or aggregates.get("trials") or 0)
    except (TypeError, ValueError):
        total = 0
    after = _number(record.get("utility"))
    improvement = _number(record.get("improvement"))
    before = after - improvement if after is not None and improvement is not None else None
    problem = str(record.get("problemStatement") or "").strip() or (
        f"harness optimization in scope {scope} "
        "(no problem statement was supplied)"
    )
    return memory.record_result(
        problem,
        edit=attempt_edit_summary(record),
        rationale=record.get("rationale") or record.get("description") or "",
        operator=str(record.get("operator") or "snapshot"),
        provenance=Provenance(
            domain=scope,
            model=str(record.get("modelName") or record.get("model") or ""),
            round=round_index,
            candidateId=str(record.get("id") or ""),
            stateDigest=str(record.get("snapshotDigest") or ""),
        ),
        predicted=record.get("predictedFixes") or record.get("predicted") or (),
        fixed=record.get("fixed") or (),
        missed=record.get("missed") or (),
        passed=passed,
        total=total,
        score_before=before,
        score_after=after,
        accepted=record.get("decision") == "accepted",
    )


def record_hillclimb_attempts(
    memory: ImprovementMemory, result: Mapping[str, Any], *, scope: str,
) -> dict[str, Any]:
    """Append one attempt per evaluated neighbour, accepted or not.

    A single malformed record is counted and skipped; it never fails the climb.
    The caller owns persistence (`memory.save`), so a dry run can record to a
    throwaway ledger and decide for itself whether to write it.
    """
    recorded = 0
    skipped = 0
    for generation in result.get("generations") or []:
        if not isinstance(generation, Mapping):
            continue
        round_index = int(generation.get("generation") or 0)
        for record in generation.get("records") or []:
            if not isinstance(record, Mapping):
                continue
            try:
                _record_neighbor(
                    memory, record, scope=scope, round_index=round_index,
                )
                recorded += 1
            except Exception:  # noqa: BLE001 - one bad record, not a climb
                skipped += 1
    return {
        "recorded": recorded,
        "skipped": skipped,
        "problems": len(memory.problems),
    }


__all__ = [
    "Attempt",
    "DEFAULT_RENDER_CHARS",
    "ImprovementMemory",
    "MAX_ATTEMPTS_PER_PROBLEM",
    "MAX_PROBLEMS",
    "MEMORY_ENV",
    "MEMORY_PATH_ENV",
    "MEMORY_CHARS_ENV",
    "MEMORY_VERSION",
    "MemoryConfig",
    "OUTCOMES",
    "Problem",
    "Provenance",
    "UNMEASURED",
    "compute_outcome",
    "load_memory",
    "record_hillclimb_attempts",
    "score_prediction",
]
