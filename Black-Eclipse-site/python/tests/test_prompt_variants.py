"""Gap #13: the coding prompt follows the resolved model family.

OpenCode ships separate system prompts per model family. Eclipse keeps one
audited base prompt (the safety rules must not fork) and appends a short
family-specific tool-use block chosen from the resolved provider/model.

The rules asserted here:

1. provider/model → family is deterministic (including unknown → generic);
2. a coding surface gets a variant; a finance turn is byte-identical;
3. GPT and Claude get different coding prompts;
4. `resolve_surface_run` applies the variant from the resolved model;
5. applying twice is idempotent.
"""

from __future__ import annotations

from harness.agents import CODE_AGENT, ECLIPSE_AGENT
from harness.prompt_variants import (
    apply_coding_variant,
    family_of,
    guidance_for,
    selected_family,
)
from harness.surface import resolve_surface_run
from harness.tools import ToolRegistry

BASE = "You are the Eclipse Code Agent.\n\n## Rules\n- stay in the folder"


def test_family_selection_is_deterministic_and_unknown_is_generic():
    assert family_of("anthropic", "claude-sonnet-4") == "anthropic"
    assert family_of("openai", "gpt-5.1-codex") == "gpt"
    assert family_of("moonshot", "kimi-k2") == "kimi"
    assert family_of("trinity", "trinity-mini") == "trinity"
    assert family_of("meta", "llama-4-70b") == "meta"
    assert family_of("google", "gemini-2.5-pro") == "google"
    assert family_of("some-gateway", "mystery-1") == "generic"
    assert guidance_for("some-gateway", "mystery-1") == guidance_for(None, None)


def test_a_coding_surface_gets_a_variant_and_finance_does_not():
    coding = apply_coding_variant(
        BASE, provider="openai", model="gpt-4o",
        agent_id="code-agent", autonomy="edit_verify",
    )
    finance = apply_coding_variant(
        BASE, provider="openai", model="gpt-4o",
        agent_id="eclipse-agent", autonomy=None,
    )
    assert finance == BASE
    assert coding.startswith(BASE.rstrip())
    assert "Model-specific coding guidance (gpt)" in coding
    assert selected_family(coding) == "gpt"


def test_gpt_and_claude_get_different_coding_prompts():
    gpt = apply_coding_variant(
        BASE, provider="openai", model="gpt-4o",
        agent_id="code-agent", autonomy="edit_verify",
    )
    claude = apply_coding_variant(
        BASE, provider="anthropic", model="claude-sonnet-4",
        agent_id="code-agent", autonomy="edit_verify",
    )
    assert gpt != claude
    assert "Model-specific coding guidance (gpt)" in gpt
    assert "Model-specific coding guidance (anthropic)" in claude
    assert "Model-specific coding guidance (anthropic)" not in gpt
    assert "Model-specific coding guidance (gpt)" not in claude


def test_resolve_surface_run_uses_the_resolved_model():
    _resolution, gpt_prompt = resolve_surface_run(
        CODE_AGENT, ToolRegistry(), {}, "edit_verify",
        provider="openai", model="gpt-4o",
    )
    _resolution2, claude_prompt = resolve_surface_run(
        CODE_AGENT, ToolRegistry(), {}, "edit_verify",
        provider="anthropic", model="claude-sonnet-4",
    )
    assert selected_family(gpt_prompt) == "gpt"
    assert selected_family(claude_prompt) == "anthropic"
    assert gpt_prompt != claude_prompt


def test_applying_twice_is_idempotent():
    once = apply_coding_variant(
        BASE, provider="moonshot", model="kimi-k2",
        agent_id="code-agent", autonomy="edit_verify",
    )
    twice = apply_coding_variant(
        once, provider="moonshot", model="kimi-k2",
        agent_id="code-agent", autonomy="edit_verify",
    )
    assert once == twice


def test_the_finance_agent_prompt_is_unchanged_by_resolution():
    _resolution, prompt = resolve_surface_run(
        ECLIPSE_AGENT, ToolRegistry(), {}, None,
        provider="openai", model="gpt-4o",
    )
    assert "Model-specific coding guidance" not in prompt
