"""Profiles and bundles: ordered layers, skip diagnostics, safe loading."""

from __future__ import annotations

import json

import pytest

from harness.kernel.errors import CompositionError
from harness.kernel.profile import load_profile


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_profile_composes_bundles_then_profile_patch(tmp_path):
    bundles = tmp_path / "bundles"
    _write(bundles / "base" / "plugin.yaml", "id: base\nbundle:\n  patch: cordis.patch.yaml\n")
    _write(bundles / "base" / "cordis.patch.yaml", """
- insert:
    - id: tools
      name: pkg.tools:apply
      config: {limit: 10}
""")
    profile = tmp_path / "profiles" / "web"
    _write(profile / "profile.json", json.dumps({
        "name": "web",
        "bundles": ["base"],
        "patch": "cordis.patch.yaml",
    }))
    _write(profile / "cordis.patch.yaml", """
- id: tools
  config: {limit: 20}
- insert:
    - id: extra
      name: pkg.extra:apply
""")
    loaded = load_profile(profile, bundle_paths=[bundles])
    assert [row.id for row in loaded.composition.rows] == ["tools", "extra"]
    assert loaded.composition.by_id("tools").config == {"limit": 20}
    assert loaded.composition.by_id("tools").source.endswith("cordis.patch.yaml")
    assert loaded.composition.diagnostics == []


def test_missing_bundle_is_skipped_with_a_diagnostic(tmp_path):
    profile = tmp_path / "profiles" / "web"
    _write(profile / "profile.json", json.dumps({"name": "web", "bundles": ["absent"]}))
    loaded = load_profile(profile, bundle_paths=[tmp_path / "bundles"])
    assert loaded.composition.rows == []
    assert loaded.composition.diagnostics[0]["code"] == "KERNEL_BUNDLE_SKIPPED"


def test_missing_profile_is_a_named_rejection(tmp_path):
    with pytest.raises(CompositionError) as info:
        load_profile(tmp_path / "nothing")
    assert info.value.code == "KERNEL_PROFILE_MISSING"


def test_malformed_patch_is_rejected_with_its_source(tmp_path):
    profile = tmp_path / "profiles" / "web"
    _write(profile / "profile.json", json.dumps({"name": "web"}))
    _write(profile / "cordis.patch.yaml", "id: not-a-list\n")
    with pytest.raises(CompositionError) as info:
        load_profile(profile)
    assert info.value.code == "KERNEL_COMPOSITION_SHAPE"
    assert "cordis.patch.yaml" in info.value.detail["source"]


def test_extra_patch_layers_apply_last(tmp_path):
    profile = tmp_path / "profiles" / "web"
    _write(profile / "profile.json", json.dumps({"name": "web"}))
    _write(profile / "cordis.patch.yaml", """
- insert:
    - id: row
      name: pkg:apply
      config: {v: 1}
""")
    overlay = tmp_path / "overlay.yaml"
    _write(overlay, "- id: row\n  config: {v: 2}\n")
    loaded = load_profile(profile, extra_patches=[overlay])
    assert loaded.composition.by_id("row").config == {"v": 2}
