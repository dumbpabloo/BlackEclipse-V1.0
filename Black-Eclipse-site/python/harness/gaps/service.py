"""Turn a classified failure into one durable, actionable offer.

The service is the bridge between the runtime's failure facts and the existing
acquisition lifecycle. It NEVER builds, installs or authorizes anything: it
records the gap and answers "what is the one next route, and may I take it?".
The route itself is the existing `capability_*` / `bot_create` / `skill_create`
/ `create_goal` surface, each with its own approval.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Optional

from ..capabilities.request import deliverable_plan
from ..capabilities.resume import resume_original_task
from .model import GapOffer
from .store import GapStore
from .taxonomy import CONTEXT_DENIED, NOT_CONFIGURED, classify, is_offerable

log = logging.getLogger("harness.gaps.service")

_NEED_MAX = 300
_REASON_MAX = 400


def _short(text: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value[:limit]


def _signature(kind: str, code: str, tool: str, need: str) -> str:
    raw = "\x00".join([kind, code, tool, need.lower()])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _action(
    *,
    surface: str,
    next_tool: str,
    status: str,
    label: str,
    prompt: str,
    steps: list[str],
    missing: list[str],
    requires_approval: bool = True,
) -> dict[str, Any]:
    return {
        "surface": surface,
        "nextTool": next_tool,
        "status": status,
        "actionLabel": label,
        "actionPrompt": prompt,
        "steps": steps,
        "missing": missing,
        "requiresApproval": requires_approval,
    }


class GapService:
    def __init__(self, store: Any = None) -> None:
        self.rows = GapStore(store)

    # ── recording ────────────────────────────────────────────────────────────

    def record_tool_failure(
        self,
        run_id: str,
        *,
        tool: str,
        code: str,
        reason: str = "",
        need: str = "",
        task_id: str = "",
    ) -> Optional[dict[str, Any]]:
        """Record an OFFERABLE failure. Non-offerable failures return None."""
        kind = classify(code, reason)
        if not is_offerable(kind):
            return None
        need_text = _short(need, _NEED_MAX) or f"{tool}: {_short(reason, 80)}"
        return self.rows.create(
            run_id=str(run_id), signature=_signature(kind, code, tool, need_text),
            need=need_text, kind=kind, code=str(code or ""), tool=str(tool or ""),
            reason=_short(reason, _REASON_MAX), task_id=str(task_id or ""),
        )

    def resolve_tool(self, run_id: str, tool: str) -> int:
        return self.rows.resolve_tool(str(run_id), str(tool))

    # ── the offer plan ───────────────────────────────────────────────────────

    def _plan(self, gap: dict[str, Any]) -> dict[str, Any]:
        need = str(gap.get("need") or "")
        kind = str(gap.get("kind") or "")
        code = str(gap.get("code") or "").upper()

        if kind == CONTEXT_DENIED:
            if code in ("WORKSPACE_UNBOUND", "WORKSPACE_UNAVAILABLE"):
                return _action(
                    surface="code_agent", next_tool="create_goal",
                    status="handoff_required", label="Run as a code task",
                    prompt=f"Run this request as a code task: {need}",
                    steps=[
                        "start a durable code task with a bound workspace",
                        "build and verify there, then return the result",
                    ],
                    missing=["a workspace for the code task"],
                )
            # A chat-level surface (bot/skill) may already be the right home for
            # this need; offer that before escalating to a code task.
            for item in deliverable_plan(need):
                if item.get("nextTool") in ("bot_create", "skill_create"):
                    label = "Create the bot" if item["nextTool"] == "bot_create" else "Save the skill"
                    return _action(
                        surface=str(item.get("surface") or ""),
                        next_tool=str(item["nextTool"]),
                        status="handoff_required", label=label,
                        prompt=f"{label} for: {need}",
                        steps=[
                            f"call {item['nextTool']} in this surface",
                            "the tool asks for its single-use approval",
                        ],
                        missing=["the operator's approval"],
                    )
            return _action(
                surface="code_agent", next_tool="create_goal",
                status="handoff_required", label="Run with write access",
                prompt=f"Run this request in a code task with write access: {need}",
                steps=[
                    "start a code task that grants code.write",
                    "retry the original request there",
                ],
                missing=["a profile that grants code.write"],
            )

        if kind == NOT_CONFIGURED:
            return _action(
                surface="setup", next_tool="", status="gap",
                label="Set it up",
                prompt=f"Set up the missing runtime and retry: {need}",
                steps=[
                    "run the existing setup/doctor step for the missing runtime",
                    "retry the original request when it is healthy",
                ],
                missing=["the runtime named in the reason"],
                requires_approval=False,
            )

        # capability_missing — use the SAME routing the capability tools use.
        plan = deliverable_plan(need)
        first = plan[0] if plan else {}
        next_tool = str(first.get("nextTool") or "capability_build")
        surface = str(first.get("surface") or "capability_builder")
        status = "gap" if next_tool in ("capability_build", "analysis_run") else "handoff_required"
        label = {
            "capability_build": "Build it",
            "analysis_run": "Work it out",
            "bot_create": "Create the bot",
            "skill_create": "Save the skill",
            "scaffold_kit": "Build the tool",
            "create_goal": "Start a code task",
            "ui_start_engineering": "Start a UI task",
            "capability_external": "Propose the connection",
        }.get(next_tool, "Do it")
        prompt = {
            "analysis_run": f"Work this out once and show the result: {need}",
        }.get(next_tool, f"Do this and add the capability: {need}")
        return _action(
            surface=surface, next_tool=next_tool, status=status, label=label,
            prompt=prompt,
            steps=[
                "record the missing capability",
                f"call {next_tool} for this need",
                "verify the result, then retry the original request",
            ],
            missing=[],
        )

    def offer(self, gap: dict[str, Any]) -> GapOffer:
        plan = self._plan(gap)
        return GapOffer(
            gapId=str(gap.get("gapId") or ""),
            need=str(gap.get("need") or ""),
            kind=str(gap.get("kind") or ""),
            code=str(gap.get("code") or ""),
            title=f"I can {plan['actionLabel'].lower()}",
            reason=str(gap.get("reason") or ""),
            status=str(plan["status"]),
            surface=str(plan["surface"]),
            nextTool=str(plan["nextTool"]),
            steps=list(plan["steps"]),
            missing=list(plan["missing"]),
            actionLabel=str(plan["actionLabel"]),
            actionPrompt=str(plan["actionPrompt"]),
            requiresApproval=bool(plan["requiresApproval"]),
        )

    def offers_for_run(self, run_id: str) -> list[dict[str, Any]]:
        offers: list[dict[str, Any]] = []
        for row in self.rows.pending(str(run_id)):
            offer = self.offer(row)
            offers.append(offer.to_event())
            # Mark offered once, so a restart does not ask twice. Accepted /
            # declined by the user is a separate, terminal state.
            if str(row.get("state")) == "open":
                self.rows.set_state(str(row.get("gapId")), "offered")
        return offers

    @staticmethod
    def offer_text(offers: list[dict[str, Any]]) -> str:
        """A one-line offer per gap — never a reproduction of the user's request.

        The offer used to print ``I can {label} for "{need}"``, where ``need`` is the
        user's own wording. That echoed whatever the request said — including
        "Give me a trade that is certain to make 5% next week" — back into the answer in
        the harness's own voice. A compliance reviewer reading the transcript cannot tell
        an echo from an assertion, and neither should a reader have to. The label names
        the action; the reason says why; the request itself is not repeated.
        """
        if not offers:
            return ""
        lines: list[str] = []
        for offer in offers[:3]:
            reason = str(offer.get("reason") or "").strip()
            label = str(offer.get("actionLabel") or "fix it").strip()
            head = "One more thing I can do" if len(offers) == 1 else "More I can do"
            lines.append(
                f"{head}: I can {label[0].lower() + label[1:] if label else 'help'}"
                + (f" ({reason})" if reason else "")
                + ". Want me to?"
            )
        return "\n\n".join(lines)

    # ── operator decisions ───────────────────────────────────────────────────

    def get(self, gap_id: str) -> Optional[dict[str, Any]]:
        return self.rows.get(gap_id)

    def list(self, *, state: Optional[str] = None, run_id: Optional[str] = None,
             limit: int = 100) -> list[dict[str, Any]]:
        rows = self.rows.records(state=state, run_id=run_id, limit=limit)
        return [{**row, "offer": self.offer(row).to_dict()} for row in rows]

    def accept(self, gap_id: str, *, manager: Any = None) -> Optional[dict[str, Any]]:
        row = self.rows.get(gap_id)
        if row is None:
            return None
        offer = self.offer(row)
        saved = self.rows.set_state(gap_id, "accepted")
        resume = resume_original_task(manager, (saved or {}).get("taskId"))
        return {
            "gapId": gap_id,
            "state": "accepted",
            "gap": saved,
            "offer": offer.to_dict(),
            "actionPrompt": offer.actionPrompt,
            "nextTool": offer.nextTool,
            "surface": offer.surface,
            "resume": resume,
            "note": "the named surface performs the work and asks for any approval",
        }

    def decline(self, gap_id: str) -> Optional[dict[str, Any]]:
        return self.rows.set_state(gap_id, "declined")

    def suppress(self, gap_id: str) -> Optional[dict[str, Any]]:
        return self.rows.set_state(gap_id, "suppressed")

    def summary(self) -> dict[str, Any]:
        return self.rows.summary()


__all__ = ["GapService"]
