"""Phase 3: one agent catalog for built-ins, presets and local registrations."""

from __future__ import annotations

import pytest

from harness.kernel.services.agents import AgentCatalog
from harness.schemas import AgentConfig


def _agent(agent_id: str, name: str = "Local") -> AgentConfig:
    return AgentConfig(id=agent_id, name=name, systemPrompt="You are local.")


def test_builtins_resolve_through_the_catalog():
    catalog = AgentCatalog()
    assert catalog.resolve("eclipse-agent").id == "eclipse-agent"
    assert catalog.resolve("code-agent").id == "code-agent"
    assert {"eclipse-agent", "code-agent", "bookkeeper", "code-navigator"} <= set(catalog.names())


def test_local_agent_resolves_and_disposes():
    catalog = AgentCatalog()
    dispose = catalog.register(_agent("custom", "Custom"))
    assert catalog.resolve("custom").name == "Custom"
    assert catalog.has("custom")
    dispose()
    with pytest.raises(ValueError):
        catalog.resolve("custom")
    assert not catalog.has("custom")


def test_a_closed_preset_store_does_not_mask_unknown_agent(monkeypatch):
    """An optional store that is closed must degrade to the built-in answer."""
    import sqlite3

    from harness import agent_presets

    class ClosedStore:
        def get(self, *_args, **_kwargs):
            raise sqlite3.ProgrammingError("Cannot operate on a closed database.")

    monkeypatch.setattr(agent_presets, "get_preset_store", lambda: ClosedStore())
    catalog = AgentCatalog()
    with pytest.raises(ValueError):
        catalog.resolve("custom")


def test_local_registration_may_shadow_a_builtin_without_mutating_it():
    from harness.agents import get_agent

    catalog = AgentCatalog()
    dispose = catalog.register(_agent("eclipse-agent", "Shadow"))
    assert catalog.resolve("eclipse-agent").name == "Shadow"
    assert get_agent("eclipse-agent").name != "Shadow"
    dispose()
    assert catalog.resolve("eclipse-agent").name == get_agent("eclipse-agent").name


def test_duplicate_local_registration_is_rejected():
    catalog = AgentCatalog()
    catalog.register(_agent("custom"))
    with pytest.raises(ValueError):
        catalog.register(_agent("custom"))


def test_catalog_reports_sources():
    catalog = AgentCatalog()
    catalog.register(_agent("custom"))
    rows = {row["id"]: row for row in catalog.catalog()}
    assert rows["custom"]["source"] == "local"
    assert rows["eclipse-agent"]["source"] == "builtin"
