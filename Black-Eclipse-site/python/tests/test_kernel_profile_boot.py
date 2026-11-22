"""Phase 2: profile/bundle composition is applied at boot, with rollback."""

from __future__ import annotations

import json
import sys
import types

import pytest

from harness.kernel import Kernel, Supervisor
from harness.kernel.profile import load_profile
from harness.kernel.profile_boot import ProfileBoot
from harness.kernel.services.runtime import KernelRuntime
from harness.tools import ToolRegistry


def good(ctx, config):
    ctx.provide("boot_good", config or {})


def boom(ctx, config):
    raise RuntimeError("boom")


@pytest.fixture(autouse=True)
def _fixture_module():
    module = types.ModuleType("kernel_fixtures")
    module.good = good
    module.boom = boom
    sys.modules["kernel_fixtures"] = module
    yield
    sys.modules.pop("kernel_fixtures", None)


def _profile(tmp_path, rows: str):
    root = tmp_path / "profiles"
    profile = root / "web"
    profile.mkdir(parents=True)
    (profile / "profile.json").write_text(json.dumps({"name": "web", "bundles": [], "patch": "cordis.patch.yaml"}))
    (profile / "cordis.patch.yaml").write_text(rows, encoding="utf-8")
    return root, profile


def _runtime():
    registry = ToolRegistry()
    runtime = KernelRuntime(enabled=True).configure(registry)
    return runtime, registry


async def test_profile_mounts_and_stop_unmounts(tmp_path):
    root, profile = _profile(tmp_path, """
- insert:
    - id: good
      name: kernel_fixtures:good
      config: {v: 1}
""")
    runtime, registry = _runtime()
    boot = ProfileBoot(runtime, profiles_root=root, profile_name="web")
    status = await boot.apply()
    assert status["status"] == "applied"
    assert status["ok"] is True
    assert runtime.kernel.root.require("boot_good") == {"v": 1}

    dumped = load_profile(profile, bundle_paths=[])
    assert [row.id for row in dumped.composition.rows] == [row["id"] for row in status["rows"]]
    assert [row.name for row in dumped.composition.rows] == [row["name"] for row in status["rows"]]

    await boot.stop()
    assert runtime.kernel.root.get("boot_good") is None
    await runtime.kernel.shutdown()


async def test_a_failing_row_restores_the_previous_generation(tmp_path):
    root, _profile_dir = _profile(tmp_path, """
- insert:
    - id: good
      name: kernel_fixtures:good
    - id: boom
      name: kernel_fixtures:boom
""")
    runtime, _registry = _runtime()
    boot = ProfileBoot(runtime, profiles_root=root, profile_name="web")
    status = await boot.apply()
    assert status["status"] == "failed"
    assert status["ok"] is False
    assert "KERNEL_ROW_FAILED" in status["error"]["code"]
    assert runtime.kernel.root.get("boot_good") is None
    await runtime.kernel.shutdown()


async def test_absent_profile_and_disabled_kernel_are_no_ops(tmp_path):
    runtime, _registry = _runtime()
    boot = ProfileBoot(runtime, profiles_root=tmp_path / "nothing", profile_name="web")
    status = await boot.apply()
    assert status["status"] == "absent"
    assert status["rows"] == []
    await runtime.kernel.shutdown()

    off = KernelRuntime(enabled=False)
    status = await ProfileBoot(off, profiles_root=tmp_path).apply()
    assert status == {"enabled": False, "status": "disabled", "rows": []}


async def test_profile_stop_then_reapply_is_idempotent(tmp_path):
    root, _ = _profile(tmp_path, """
- insert:
    - id: good
      name: kernel_fixtures:good
""")
    runtime, _ = _runtime()
    boot = ProfileBoot(runtime, profiles_root=root, profile_name="web")
    await boot.apply()
    await boot.stop()
    assert runtime.kernel.root.get("boot_good") is None
    await boot.apply()
    assert runtime.kernel.root.require("boot_good") == {}
    await boot.stop()
    await runtime.kernel.shutdown()
