"""The research-hypothesis registry contract: ids, lifecycle, and ranked search.

A hypothesis is the unit of falsifiable work: a title, a thesis, a lifecycle status, and
the run cards that tested it. This module is the pure part — id derivation, status
validation, and search ranking — ported from Vibe-Trading's
`hypotheses/registry.py` (MIT), which keeps storage (JSON) separate from these rules.

Three details are the contract, and each is a behaviour a naive implementation gets
wrong:

* **the id is derived, not random** — ``hyp_`` + the first 12 hex of
  ``sha256("<title>|<created_at>")``, with ``_2``/``_3`` on collision. Two runs that
  record the same hypothesis at the same second therefore agree on its id, and a
  re-import cannot silently duplicate it;
* **status is a closed vocabulary** — ``exploring``/``testing``/``validated``/
  ``rejected``/``monitoring``, lower-cased, unknown values refused with the allowed
  list in the message;
* **search ranks by token overlap, then recency** — and ``limit=0`` means **zero**
  results, not "at least one": an explicit 0 from a caller must not become 1.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable, Mapping, Optional, Sequence

__all__ = [
    "HYPOTHESIS_STATUSES",
    "hypothesis_id",
    "search",
    "validate_status",
]

#: The lifecycle a hypothesis moves through.
HYPOTHESIS_STATUSES = ("exploring", "testing", "validated", "rejected", "monitoring")

_STATUS_SET = frozenset(HYPOTHESIS_STATUSES)

#: Tokens of two or more characters, plus single CJK characters — the reference's
#: tokenizer, so a Chinese title is searchable and one-letter noise is not a hit.
_TOKEN_RE = re.compile(r"[a-zA-Z0-9]{2,}|[\u4e00-\u9fff]")


def validate_status(status: Any) -> str:
    """Lower-case and validate a status, naming the vocabulary on refusal."""
    normalized = str(status).strip().lower()
    if normalized not in _STATUS_SET:
        raise ValueError(
            f"unknown hypothesis status {status!r}. Allowed: {', '.join(HYPOTHESIS_STATUSES)}"
        )
    return normalized


def hypothesis_id(title: str, created_at: str, existing: Iterable[str] = ()) -> str:
    """Deterministic id for a hypothesis, with a collision suffix.

    Deterministic on purpose: the same title recorded at the same instant yields the
    same id, so a re-import is idempotent rather than a duplicate row.
    """
    seed = f"{str(title).strip().lower()}|{created_at}"
    base = "hyp_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
    taken = set(existing)
    if base not in taken:
        return base
    index = 2
    while f"{base}_{index}" in taken:
        index += 1
    return f"{base}_{index}"


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(str(text).lower()))


def _haystack(record: Mapping[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)


def search(
    records: Sequence[Mapping[str, Any]],
    *,
    query: str = "",
    status: Optional[str] = None,
    limit: int = 10,
) -> list[Mapping[str, Any]]:
    """Hypotheses matching ``query`` and/or ``status``, best first.

    Score is the size of the token intersection over the whole record (title, thesis,
    universe, signal, sources, skills, notes and links), ties broken by the most
    recently updated. An empty query matches everything (score 1). ``limit`` is
    clamped to ``0..100``, and **0 returns nothing** — an explicit zero is a request
    for zero results, not a floor of one.
    """
    status_filter = validate_status(status) if status else None
    query_tokens = _tokens(query)
    scored: list[tuple[int, str, Mapping[str, Any]]] = []
    for record in records:
        if status_filter and str(record.get("status")) != status_filter:
            continue
        if not query_tokens:
            score = 1
        else:
            score = len(query_tokens & _tokens(_haystack(record)))
        if score > 0:
            scored.append((score, str(record.get("updated_at") or ""), record))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [record for _score, _updated, record in scored[: max(0, min(int(limit), 100))]]
