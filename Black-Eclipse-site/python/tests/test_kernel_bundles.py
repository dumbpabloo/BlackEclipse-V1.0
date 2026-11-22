"""Bundle trust and compatibility: platform, version, deps/cycles, signature."""

from __future__ import annotations

import json

import pytest

from harness.kernel.errors import CompositionError
from harness.kernel.profile import (
    BundlePolicy,
    bundle_policy_from_env,
    bundle_signature_payload,
    load_profile,
)
from harness.plugins.ed25519 import public_key, sign

PATCH = """
- insert:
    - id: row-{name}
      name: pkg.{name}:apply
"""


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _bundle(root, name, *, extra="", patch=None):
    _write(root / name / "plugin.yaml", f"id: {name}\nbundle:\n  patch: cordis.patch.yaml\n{extra}")
    _write(root / name / "cordis.patch.yaml", patch or PATCH.format(name=name))


def _profile(tmp_path, bundles):
    profile = tmp_path / "profiles" / "web"
    _write(profile / "profile.json", json.dumps({"name": "web", "bundles": bundles}))
    return profile


def test_platform_mismatch_skips_the_bundle(tmp_path):
    bundles = tmp_path / "bundles"
    _bundle(bundles, "mac-only", extra="  platforms: [darwin]\n")
    loaded = load_profile(
        _profile(tmp_path, ["mac-only"]), bundle_paths=[bundles],
        policy=BundlePolicy(platform="win32"),
    )
    assert loaded.composition.rows == []
    diagnostic = loaded.composition.diagnostics[0]
    assert diagnostic["code"] == "KERNEL_BUNDLE_SKIPPED"
    assert "platform" in diagnostic["message"]


def test_kernel_version_range_is_enforced(tmp_path):
    bundles = tmp_path / "bundles"
    _bundle(bundles, "future", extra="  kernel: '>=9.0.0'\n")
    loaded = load_profile(
        _profile(tmp_path, ["future"]), bundle_paths=[bundles],
        policy=BundlePolicy(host_version="1.0.0"),
    )
    assert loaded.composition.rows == []
    assert "requires kernel >=9.0.0" in loaded.composition.diagnostics[0]["message"]


def test_unknown_requirement_skips_the_bundle(tmp_path):
    bundles = tmp_path / "bundles"
    _bundle(bundles, "top", extra="  requires: [absent]\n")
    loaded = load_profile(_profile(tmp_path, ["top"]), bundle_paths=[bundles])
    assert loaded.composition.rows == []
    assert "requires missing bundle" in loaded.composition.diagnostics[0]["message"]


def test_dependency_cycle_skips_every_member(tmp_path):
    bundles = tmp_path / "bundles"
    _bundle(bundles, "a", extra="  requires: [b]\n")
    _bundle(bundles, "b", extra="  requires: [a]\n")
    _bundle(bundles, "clean")
    loaded = load_profile(_profile(tmp_path, ["a", "b", "clean"]), bundle_paths=[bundles])
    assert [row.id for row in loaded.composition.rows] == ["row-clean"]
    cycle = [row for row in loaded.composition.diagnostics if "cycle" in row["message"]]
    assert {row["bundle"] for row in cycle} == {"a", "b"}


def test_signature_required_but_absent_skips_the_bundle(tmp_path):
    bundles = tmp_path / "bundles"
    _bundle(bundles, "unsigned")
    loaded = load_profile(
        _profile(tmp_path, ["unsigned"]), bundle_paths=[bundles],
        policy=BundlePolicy(require_signature=True, trusted_keys=(b"\x01" * 32,)),
    )
    assert loaded.composition.rows == []
    assert "missing signature" in loaded.composition.diagnostics[0]["message"]


def test_valid_signature_mounts_and_tampering_is_refused(tmp_path):
    seed = bytes.fromhex("11" * 32)
    key = public_key(seed)
    bundles = tmp_path / "bundles"
    bundle = bundles / "signed"
    _bundle(bundles, "signed")
    declaration = {"patch": "cordis.patch.yaml"}
    payload = bundle_signature_payload(declaration, [bundle / "cordis.patch.yaml"])
    signature = sign(seed, payload).hex()
    _write(
        bundle / "plugin.yaml",
        f"id: signed\nbundle:\n  patch: cordis.patch.yaml\n  signature: {signature}\n",
    )
    policy = BundlePolicy(trusted_keys=(key,), require_signature=True)
    loaded = load_profile(_profile(tmp_path, ["signed"]), bundle_paths=[bundles], policy=policy)
    assert [row.id for row in loaded.composition.rows] == ["row-signed"]

    _write(bundle / "cordis.patch.yaml", PATCH.format(name="tampered"))
    loaded = load_profile(_profile(tmp_path, ["signed"]), bundle_paths=[bundles], policy=policy)
    assert loaded.composition.rows == []
    assert "does not verify" in loaded.composition.diagnostics[0]["message"]


def test_policy_reads_trusted_keys_and_never_silently_accepts_a_bad_key():
    policy = bundle_policy_from_env({
        "ECLIPSE_KERNEL_TRUSTED_KEYS": "11" * 32,
        "ECLIPSE_KERNEL_REQUIRE_SIGNATURE": "1",
    })
    assert policy.require_signature is True
    assert len(policy.trusted_keys) == 1
    with pytest.raises(CompositionError) as info:
        bundle_policy_from_env({"ECLIPSE_KERNEL_TRUSTED_KEYS": "not-hex"})
    assert info.value.code == "KERNEL_BUNDLE_TRUSTED_KEY"
