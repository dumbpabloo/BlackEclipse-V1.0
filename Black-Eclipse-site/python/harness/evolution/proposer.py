"""Bounded mutation proposals over observed signals.

The proposer is deterministic and template-based. It never invents facts and
never emits more than one mutation per candidate. An unknown signal produces no
candidate. The output still goes through admission, evaluation and the journal.
"""

from __future__ import annotations

import hashlib
from typing import Any, Optional

from .assets import AssetKind, Candidate, Mutation, MutationOp
from .observer import Suggestion
from .service import AssetEvolution

# Generic, audited guidance only. Detail from a run is never interpolated into
# the prompt: an observation may be attacker-influenced text.
TEMPLATES: dict[str, str] = {
    "repeated_tool": (
        "Before repeating a tool call, inspect the previous result and change the query "
        "or stop; do not loop on the same call."
    ),
    "recovered_failure": (
        "When a tool fails, read the error and try one alternate source or argument; "
        "if none works, state the missing capability instead of retrying the same call."
    ),
    "user_correction": (
        "Apply the user's correction to the next answer and state the corrected fact "
        "explicitly rather than silently changing the claim."
    ),
    "prompt_gap": (
        "When a request names a capability that is not in the bound tool surface, say "
        "so plainly and offer the closest available route."
    ),
}
MAX_TEXT = 600


class AssetProposer:
    def __init__(self, evolution: AssetEvolution) -> None:
        self.evolution = evolution

    def propose(self, suggestion: Suggestion) -> Optional[Candidate]:
        text = TEMPLATES.get(suggestion.kind)
        if not text:
            return None
        text = text[:MAX_TEXT]
        target = "learned." + hashlib.sha256(
            f"{suggestion.kind}\0{suggestion.key}".encode("utf-8")
        ).hexdigest()[:16]
        scope = str(suggestion.detail.get("scope") or "")
        candidate = self.evolution.propose(
            kind=AssetKind.PROMPT_SECTION,
            target=target,
            mutations=[Mutation(
                op=MutationOp.ADD,
                kind=AssetKind.PROMPT_SECTION,
                target=target,
                content={"text": text, "scope": scope},
                rationale=f"{suggestion.kind}:{suggestion.key} observed {suggestion.count}x",
                expected_effect="reduce recurrence of the observed failure",
            )],
            evidence_refs=list(suggestion.evidence_refs),
        )
        return candidate
