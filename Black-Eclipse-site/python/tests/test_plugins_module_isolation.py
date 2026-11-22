"""Plugin module isolation: disable/re-enable never serves stale code."""

from __future__ import annotations

import sys
from pathlib import Path

from plugins_helpers import BUNDLED, env, package


def _modules_under(directory: Path) -> list[str]:
    root = Path(directory).resolve()
    found = []
    for name, module in list(sys.modules.items()):
        file_path = getattr(module, "__file__", None)
        if not file_path:
            continue
        try:
            path = Path(file_path).resolve()
        except (OSError, ValueError):
            continue
        if path == root or root in path.parents:
            found.append(name)
    return sorted(found)


def _source(value: int, marker: Path) -> str:
    return (
        "from pathlib import Path\n"
        "from harness.plugins.sdk import Plugin, tool\n\n\n"
        "with Path(%r).open('a', encoding='utf-8') as handle:\n"
        "    handle.write('x')\n\n\n"
        "VALUE = %d\n\n\n"
        "class Impl(Plugin):\n"
        "    @tool(name=\"ping\", description=\"ping\", schema={}, permission=\"read\")\n"
        "    async def ping(self, args, ctx):\n"
        "        return {\"v\": VALUE}\n\n\n"
        "plugin = Impl\n" % (str(marker), value)
    )


def _package(plugin_id: str, value: int, marker: Path) -> dict:
    pkg = package(plugin_id, tools=[])
    pkg["files"]["plugin.py"] = _source(value, marker)
    return pkg


async def test_disable_evicts_only_that_plugins_modules(env, tmp_path):
    marker = tmp_path / "imports.log"
    for plugin_id in ("acme-iso-a", "acme-iso-b"):
        await env.manager.install_package(**_package(plugin_id, 1, marker), enable=True, operator=True)

    dir_a = env.manager.directory_of("acme-iso-a")
    dir_b = env.manager.directory_of("acme-iso-b")
    assert _modules_under(dir_a)
    assert _modules_under(dir_b)

    await env.manager.disable("acme-iso-a")
    assert _modules_under(dir_a) == []
    assert _modules_under(dir_b)
    assert env.registry.get("plug_acme_iso_a_ping") is None
    assert env.registry.get("plug_acme_iso_b_ping") is not None
    # The host SDK is not a plugin module and is never evicted.
    assert "harness.plugins.sdk" in sys.modules


async def test_reenable_reexecutes_the_module(env, tmp_path):
    marker = tmp_path / "imports.log"
    await env.manager.install_package(**_package("acme-iso", 2, marker), enable=True, operator=True)
    tool_name = "plug_acme_iso_ping"
    directory = env.manager.directory_of("acme-iso")
    assert marker.read_text(encoding="utf-8") == "x"
    assert _modules_under(directory)
    assert (await env.registry.get(tool_name).handler({}, env.ctx()))["v"] == 2

    await env.manager.disable("acme-iso")
    assert _modules_under(directory) == []
    assert env.registry.get(tool_name) is None

    await env.manager.enable("acme-iso", operator=True)
    # Re-executed from source, not served from a stale sys.modules entry.
    assert marker.read_text(encoding="utf-8") == "xx"
    assert _modules_under(directory)
    assert (await env.registry.get(tool_name).handler({}, env.ctx()))["v"] == 2
    assert len([name for name in env.registry.names() if name == tool_name]) == 1
