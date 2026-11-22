"""Model-family coding prompt variants (gap #13).

A single coding prompt is a compromise: models trained with different tool-call
conventions respond better to different emphasis. OpenCode ships separate
anthropic / gpt / kimi / trinity / meta system prompts. Eclipse keeps ONE
audited base prompt (the safety rules must not fork) and appends a short,
family-specific guidance block selected from the RESOLVED provider/model.

Properties:

  * only CODING surfaces get a variant (`code-agent`, or the chat code profile);
    a finance turn is byte-identical;
  * the block is deterministic for a (provider, model) pair, so the prompt-cache
    prefix stays stable within a run;
  * an unknown family gets the generic block, never a guess at a vendor;
  * applying twice is idempotent (the banner is checked).
"""

from __future__ import annotations

from typing import Optional

FAMILIES = ("anthropic", "gpt", "kimi", "trinity", "meta", "google", "generic")

_BANNER = "## Model-specific coding guidance"

#: Short, operational blocks. Each changes HOW the model should use the same
#: tools; none grants a capability and none weakens a safety rule.
GUIDANCE: dict[str, str] = {
    "anthropic": (
        "Read the relevant region before changing it, and keep each edit small "
        "enough to verify. After an edit, check the changed region rather than "
        "re-reading the whole file. Prefer a sequence of small, individually "
        "verified changes over one large rewrite."
    ),
    "gpt": (
        "Batch independent reads and searches in one turn, then act on the "
        "results together. Use `code_edit` for a partial change and `write_file` "
        "only for a new or fully replaced file. Run the project's check after a "
        "change before declaring it done."
    ),
    "kimi": (
        "Do not restate file contents in prose; move the work through tool calls. "
        "On a large repository search first and read only the matching regions. "
        "Keep one dependency chain per step: a search, then a read, then the edit."
    ),
    "trinity": (
        "Be terse. Use `read_file` then `code_edit` for source changes, and avoid "
        "a shell command when a dedicated file or search tool does the job. State "
        "the verification you actually ran, in one line."
    ),
    "meta": (
        "Plan one concrete step, execute it, and verify it before the next. Keep "
        "edits minimal and anchored on text you have read. If a check fails, "
        "change one thing and re-run the same check."
    ),
    "google": (
        "Prefer explicit, well-typed tool arguments and one focused change per "
        "step. Read before editing, and verify with the project's own check rather "
        "than assuming the change works."
    ),
    "generic": (
        "Use the dedicated file, search and check tools before shell equivalents. "
        "Read before editing, keep changes minimal, and verify with a command "
        "whose exit code you can cite."
    ),
}


def family_of(provider: Optional[str], model: Optional[str]) -> str:
    """The model family for a resolved provider/model. Unknown → `generic`."""
    text = f"{provider or ''} {model or ''}".lower()
    if "claude" in text or "anthropic" in text:
        return "anthropic"
    if any(token in text for token in ("gpt", "chatgpt", "codex", "o1", "o3", "o4")):
        return "gpt"
    if "kimi" in text or "moonshot" in text:
        return "kimi"
    if "trinity" in text:
        return "trinity"
    if "llama" in text or "meta" in text:
        return "meta"
    if "gemini" in text or "gemma" in text or "google" in text:
        return "google"
    return "generic"


def guidance_for(provider: Optional[str], model: Optional[str]) -> str:
    family = family_of(provider, model)
    return GUIDANCE.get(family, GUIDANCE["generic"])


def is_coding_surface(agent_id: str, autonomy: Optional[str]) -> bool:
    if agent_id == "code-agent":
        return True
    return agent_id == "eclipse-agent" and bool(autonomy)


def apply_coding_variant(
    prompt: str,
    *, provider: Optional[str], model: Optional[str],
    agent_id: str, autonomy: Optional[str],
) -> str:
    """Append the family block for a coding surface. Idempotent and pure."""
    if not is_coding_surface(agent_id, autonomy):
        return prompt
    if _BANNER in prompt:
        return prompt
    family = family_of(provider, model)
    block = (
        f"{_BANNER} ({family})\n"
        "The safety and honesty rules above always win. This section only adjusts "
        "how you use the same tools for this model family.\n\n"
        f"{guidance_for(provider, model)}"
    )
    return prompt.rstrip() + "\n\n" + block


def selected_family(prompt: str) -> Optional[str]:
    """The family named by an already-varied prompt (None when unvaried)."""
    marker = f"{_BANNER} ("
    start = prompt.find(marker)
    if start < 0:
        return None
    rest = prompt[start + len(marker):]
    end = rest.find(")")
    return rest[:end] if end >= 0 else None


__all__ = [
    "FAMILIES",
    "GUIDANCE",
    "apply_coding_variant",
    "family_of",
    "guidance_for",
    "is_coding_surface",
    "selected_family",
]
