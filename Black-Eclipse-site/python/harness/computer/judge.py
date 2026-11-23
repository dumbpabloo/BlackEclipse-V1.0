"""The behavior judge: compare branch narratives and pick one (BJudge-style).

Agent S3's largest lever is Behavior Best-of-N: run k rollouts, describe each as
a behavior narrative, and let a comparative judge pick the best. Eclipse's
`best_of` engine already produces deterministic captions; this module supplies
the optional *model* judge over those captions.

It compares TEXT ONLY — no screenshots, no raw UI, no tool output — so the judge
cannot be prompt-injected by screen content beyond the captions (which are
already scrubbed and bounded). A judge failure is never a run failure: the caller
falls back to the deterministic pick.
"""

from __future__ import annotations

import re
from typing import Any, Awaitable, Callable, Optional

_MAX_CAPTION_CHARS = 1_200

_PROMPT = """You are comparing {count} independent attempts at ONE desktop task.
Pick the attempt that best completed the task, using ONLY these rules, in order:

1. A verified success beats an unverified claim. A failed attempt never wins.
2. Prefer the attempt that followed a clean, direct path (fewer failed steps).
3. Prefer fewer wasted steps when both succeeded.
4. Never reward an attempt that used an unsafe or blocked action.

Task:
{task}

Attempts:
{attempts}

Reply with exactly one line: `CHOICE: <number>` where <number> is 1..{count}.
If no attempt succeeded, reply `CHOICE: 0`."""


def build_judge_prompt(task: str, captions: list[str]) -> str:
    attempts = "\n\n".join(
        f"Attempt {index + 1}:\n{caption[:_MAX_CAPTION_CHARS]}"
        for index, caption in enumerate(captions)
    )
    return _PROMPT.format(count=len(captions), task=task[:2_000], attempts=attempts)


def parse_judge_choice(text: str, count: int) -> Optional[int]:
    """1-based reply → 0-based index (None when unusable)."""
    if not text:
        return None
    match = re.search(r"CHOICE\s*[:#-]?\s*(\d+)", text, re.IGNORECASE)
    if match is None:
        numbers = re.findall(r"\b(\d+)\b", text)
        if not numbers:
            return None
        value = int(numbers[-1])
    else:
        value = int(match.group(1))
    if value <= 0 or value > count:
        return None
    return value - 1


def _text_of(response: Any) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, list):
        return " ".join(
            str(part.get("text") or "") if isinstance(part, dict) else str(part)
            for part in content
        )
    return str(content or "")


async def judge_with_model(
    task: str, captions: list[str],
    *, complete: Callable[[str], Any],
) -> Optional[int]:
    """Ask a model to choose. `complete(prompt)` may be sync or async."""
    prompt = build_judge_prompt(task, captions)
    result = complete(prompt)
    if hasattr(result, "__await__"):
        result = await result
    return parse_judge_choice(_text_of(result), len(captions))


def build_behavior_judge(settings: Any) -> Optional[Callable[[str, list[str]], Awaitable[Optional[int]]]]:
    """A judge bound to the run's model settings, or None when unavailable.

    The model is constructed lazily inside the call so a branch run never pays
    for it unless best-of-N actually has more than one candidate. A missing
    credential is an honest None (the caller keeps the deterministic pick).
    """
    if settings is None:
        return None
    try:
        from ..models import build_chat_model  # noqa: PLC0415

        model = build_chat_model(settings)
    except Exception:  # noqa: BLE001 - no model, no model judge
        return None

    async def judge(task: str, captions: list[str]) -> Optional[int]:
        response = await model.ainvoke(build_judge_prompt(task, captions))
        return parse_judge_choice(_text_of(response), len(captions))

    return judge


__all__ = [
    "build_behavior_judge", "build_judge_prompt", "judge_with_model",
    "parse_judge_choice",
]
