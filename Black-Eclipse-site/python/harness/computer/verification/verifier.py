"""The action verifier.

Before an action the runtime captures a light `ObservationState` (window, AX
digest, screen digest, OCR digest). After it, the verifier compares the expected
effect (from the caller, the adapter or a default) against what actually
changed and returns verified / failed / uncertain — plus the screen diff.

The verifier is provider-agnostic: it works on any driver by taking fresh
digests and optional before/after frames. It never claims success it cannot show.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Optional

from ..models import Snapshot
from ..screenshots.diff import ScreenDiff


@dataclass
class ObservationState:
    window_id: str = ""
    window_title: str = ""
    ax_digest: str = ""
    ocr_digest: str = ""
    screen_digest: str = ""
    frame: Any = None
    focused_window: str = ""

    def fingerprint(self) -> str:
        raw = f"{self.window_id}|{self.ax_digest}|{self.ocr_digest}|{self.screen_digest}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class VerificationResult:
    status: str                       # verified | failed | uncertain
    action: str
    state_changed: bool = False
    checks: list[dict[str, Any]] = field(default_factory=list)
    diff: Optional[dict[str, Any]] = None
    summary: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.status == "verified"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status, "action": self.action, "stateChanged": self.state_changed,
            "checks": self.checks, "diff": self.diff, "summary": self.summary,
            "warnings": self.warnings,
        }


class ActionVerifier:
    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self._diff = ScreenDiff()

    def capture(self, snapshot: Optional[Snapshot] = None, *, frame: Any = None, focused_window: str = "") -> ObservationState:
        state = ObservationState(
            window_id=snapshot.window_id if snapshot else "",
            window_title=snapshot.window_title if snapshot else "",
            ax_digest=snapshot.ax_digest if snapshot else "",
            ocr_digest=snapshot.ocr_digest if snapshot else "",
            frame=frame,
            focused_window=focused_window,
        )
        state.screen_digest = hashlib.sha256(
            f"{state.ax_digest}|{state.ocr_digest}|{state.window_id}".encode("utf-8")
        ).hexdigest()[:20]
        return state

    def verify(
        self,
        before: ObservationState,
        after: ObservationState,
        *,
        action: str,
        expect: Optional[dict[str, Any]] = None,
        after_frame: Any = None,
        after_text: str = "",
    ) -> VerificationResult:
        expect = expect or {}
        result = VerificationResult(status="uncertain", action=action)

        if not self.enabled:
            result.status = "uncertain"
            result.summary = "verification is disabled; the action ran but its effect wasn't checked"
            return result

        # SEMANTIC change: the accessibility tree, OCR text, window identity or
        # title. This is the AUTHORITATIVE signal — it is stable and content-based.
        semantic_change = (
            before.ax_digest != after.ax_digest
            or before.ocr_digest != after.ocr_digest
            or before.window_id != after.window_id
            or before.window_title != after.window_title
        )
        pixel_change = False
        if before.frame is not None and after_frame is not None:
            diff = self._diff.compare(before.frame, after_frame)
            result.diff = diff.to_dict()
            pixel_change = diff.changed
        # Screen digest folds in the semantic digest; keep it as corroboration.
        digest_change = before.screen_digest != after.screen_digest
        state_changed = semantic_change or pixel_change or digest_change
        result.state_changed = state_changed
        result.checks.append({
            "name": "semantic_change", "passed": semantic_change,
            "pixel_change": pixel_change, "expectation": False,
        })

        # Expectation checks.
        failed_expectations: list[str] = []
        if expect.get("stateChanged") is True:
            result.checks.append({"name": "state_changed_required", "passed": state_changed})
            if not state_changed:
                failed_expectations.append("state_changed")
        if expect.get("windowChanged") is True:
            passed = before.window_id != after.window_id
            result.checks.append({"name": "window_changed", "passed": passed})
            if not passed:
                failed_expectations.append("window_changed")
        if expect.get("dialogAppeared") is True:
            # A dialog is a new foreground window with a dialog-ish title.
            passed = after.window_id != before.window_id and bool(after.window_title)
            result.checks.append({"name": "dialog_appeared", "passed": passed})
            if not passed:
                failed_expectations.append("dialog_appeared")
        if expect.get("axContains"):
            needle = str(expect["axContains"]).lower()
            # Compare against the AFTER snapshot's rendered text, which the
            # session supplies. A digest is a hash and can never contain text, so
            # the old digest comparison made this check dead.
            haystack = (after_text or str(expect.get("_after_text", ""))).lower()
            passed = needle in haystack
            result.checks.append({"name": "ax_contains", "passed": passed, "expected": needle})
            if not passed:
                failed_expectations.append("ax_contains")

        if failed_expectations:
            result.status = "failed"
            result.warnings.append("The expected effect didn't occur: " + ", ".join(failed_expectations))
            result.summary = "The action ran but the expected change wasn't observed."
        elif semantic_change:
            # A content-based change is a real change.
            result.status = "verified"
            result.summary = "The desktop's content changed after the action."
        elif pixel_change or digest_change:
            # Pixels moved but the SEMANTIC content did not: this is incidental
            # churn (a blinking caret, a hover highlight, an animation). It is NOT
            # a verified effect — the agent must look, not assume.
            result.status = "uncertain"
            result.warnings.append("only incidental pixel change was detected; no semantic change")
            result.summary = "The action ran; only incidental visual change was seen, not a content change."
        else:
            # No expectation and no change ⇒ uncertain (NOT success).
            result.status = "uncertain"
            result.summary = "The action ran but no visible change was detected."
        return result


__all__ = ["ActionVerifier", "ObservationState", "VerificationResult"]
