"""Hot reload: digest watching swaps generations and keeps the old one on bad edits."""

from __future__ import annotations

import json
import sys
import types

import pytest

from harness.kernel.profile_boot import ProfileBoot
from harness.kernel.services.runtime import KernelRuntime
from harness.kernel.watch import ProfileWatcher, digest_paths, watch_enabled, watch_interval
from harness.tools import ToolRegistry


@pytest.fixture(autouse=True)
def _fixture_module():
    module = types.ModuleType("kernel_watch_fixtures")

    def stamp(ctx, config):
        ctx.provide("watched_stamp", config or {})

    module.stamp = stamp
    sys.modules["kernel_watch_fixtures"] = module
    yield
    sys.modules.pop("kernel_watch_fixtures", None)


def _profile(tmp_path, value):
    root = tmp_path / "profiles"
    profile = root / "web"
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "profile.json").write_text(json.dumps({"name": "web", "bundles": [], "patch": "cordis.patch.yaml"}))
    (profile / "cordis.patch.yaml").write_text(f"""
- insert:
    - id: watched
      name: kernel_watch_fixtures:stamp
      config: {{v: {value}}}
""", encoding="utf-8")
    return root


async def test_watch_poll_swaps_the_generation_once(tmp_path):
    root = _profile(tmp_path, 1)
    runtime = KernelRuntime(enabled=True).configure(ToolRegistry())
    boot = ProfileBoot(runtime, profiles_root=root, profile_name="web")
    assert (await boot.apply())["status"] == "applied"
    assert runtime.kernel.root.require("watched_stamp") == {"v": 1}

    watcher = ProfileWatcher(boot, interval=0.05)
    assert await watcher.poll() is None
    assert watcher.reloads == 0

    _profile(tmp_path, 2)
    status = await watcher.poll()
    assert status["status"] == "applied"
    assert watcher.reloads == 1
    assert runtime.kernel.root.require("watched_stamp") == {"v": 2}

    assert await watcher.poll() is None
    assert watcher.reloads == 1
    await runtime.kernel.shutdown()


async def test_a_broken_edit_keeps_the_previous_generation(tmp_path):
    root = _profile(tmp_path, 1)
    runtime = KernelRuntime(enabled=True).configure(ToolRegistry())
    boot = ProfileBoot(runtime, profiles_root=root, profile_name="web")
    await boot.apply()
    watcher = ProfileWatcher(boot, interval=0.05)

    (root / "web" / "cordis.patch.yaml").write_text("- insert: [", encoding="utf-8")
    status = await watcher.poll()
    assert status["status"] == "invalid"
    assert status["keptPrevious"] is True
    assert runtime.kernel.root.require("watched_stamp") == {"v": 1}
    await runtime.kernel.shutdown()


def test_watch_env_is_opt_in(tmp_path):
    assert watch_enabled({}) is False
    assert watch_enabled({"ECLIPSE_KERNEL_WATCH": "1"}) is True
    assert watch_interval({"ECLIPSE_KERNEL_WATCH_INTERVAL": "0"}) == 0.05
    first = tmp_path / "a"
    first.mkdir()
    (first / "profile.json").write_text("{}")
    digest = digest_paths([first])
    (first / "profile.json").write_text('{"name": "web"}')
    assert digest_paths([first]) != digest


async def test_start_profile_honours_the_watch_flag(tmp_path, monkeypatch):
    from harness.kernel.profile_boot import start_profile, stop_profile

    root = _profile(tmp_path, 1)

    class Service:
        pass

    service = Service()
    service.kernel_runtime = KernelRuntime(enabled=True).configure(ToolRegistry())
    monkeypatch.setenv("ECLIPSE_PROFILES_DIR", str(root))
    monkeypatch.setenv("ECLIPSE_KERNEL_WATCH", "1")
    status = await start_profile(service)
    assert status["status"] == "applied"
    assert status["watch"] is True
    assert service._kernel_profile.watcher is not None
    await stop_profile(service)
    assert service._kernel_profile.watcher is None
    await service.kernel_runtime.kernel.shutdown()
