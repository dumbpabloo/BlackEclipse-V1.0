"""Phase 4 manifest: prompt contributions and provides/requires declarations."""

from __future__ import annotations

import pytest

from harness.plugins.manifest import parse_manifest


def _manifest(**overrides):
    data = {
        "id": "service-demo",
        "name": "Service Demo",
        "version": "1.0.0",
        "runtime": {"type": "python", "entrypoint": "plugin:Impl"},
    }
    data.update(overrides)
    return parse_manifest(data)


def test_prompt_contribution_is_declared_and_cross_checked():
    manifest = _manifest(contributes={
        "prompts": [{"id": "demo.guidance", "scope": "demo-agent", "order": 50}],
    })
    assert manifest.contributes.prompts[0].id == "demo.guidance"
    assert manifest.contributes.prompts[0].scope == "demo-agent"
    assert manifest.contributes.all_contribution_names()["prompts"] == ["demo.guidance"]


def test_provides_and_requires_are_typed():
    manifest = _manifest(
        provides=[{"key": "demo.agent", "version": 1}],
        requires=[{"key": "tools"}, {"key": "optional.service", "optional": True}],
    )
    assert manifest.provides[0].key == "demo.agent"
    assert manifest.provides[0].version == 1
    assert manifest.requires[1].optional is True


def test_invalid_service_key_is_rejected():
    with pytest.raises(Exception):
        _manifest(provides=[{"key": "Not A Key"}])


def test_duplicate_provides_and_provide_require_overlap_are_rejected():
    with pytest.raises(Exception):
        _manifest(provides=[{"key": "demo.agent"}, {"key": "demo.agent"}])
    with pytest.raises(Exception):
        _manifest(provides=[{"key": "demo.agent"}], requires=[{"key": "demo.agent"}])


def test_workflows_are_in_the_contribution_name_index():
    manifest = _manifest(contributes={"workflows": [{"id": "demo-flow"}]})
    assert manifest.contributes.all_contribution_names()["workflows"] == ["demo-flow"]


def test_model_provider_contribution_is_indexed():
    manifest = _manifest(contributes={"models": [{"name": "demo-model", "provider": "demo"}]})
    assert manifest.contributes.models[0].provider == "demo"
    assert manifest.contributes.all_contribution_names()["models"] == ["demo-model"]
