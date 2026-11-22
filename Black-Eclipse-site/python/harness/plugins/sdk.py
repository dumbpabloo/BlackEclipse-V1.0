"""The Plugin SDK — the stable, developer-facing surface.

Third-party developers subclass `Plugin` and decorate methods with `@tool`,
`@skill`, `@hook`, `@command`, `@event_handler`, `@worker`, `@webhook` and
`@ui_panel`. The runtime introspects the resulting contributions, cross-checks
them against the manifest, and wires them into Eclipse's existing systems.

The SDK deliberately exposes a small, stable vocabulary. Plugins never receive a
tool registry, event bus, database handle or model object directly; they receive
a `PluginContext` that offers only permission-checked, versioned operations.

`SDK_VERSION` (re-exported from `manifest`) identifies the SDK contract a plugin
is compiled against.

Example
-------
    from harness.plugins.sdk import Plugin, tool

    class Hello(Plugin):
        @tool(name="greet", description="Say hello",
              schema={"type": "object", "properties": {"name": {"type": "string"}}})
        async def greet(self, args, ctx):
            return f"Hello, {args.get('name', 'world')}!"
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Callable, Optional

from .manifest import SDK_VERSION  # noqa: F401  (re-exported for plugin authors)

CONTRIB_ATTR = "__plugin_contrib__"


class PluginToolError(Exception):
    """A tool handler may raise this to return a structured, non-crashing failure."""

    def __init__(self, message: str, *, code: str = "PLUGIN_TOOL_ERROR") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _mark(func: Callable[..., Any], contrib: dict[str, Any]) -> Callable[..., Any]:
    existing = getattr(func, CONTRIB_ATTR, None)
    marks = list(existing) if existing else []
    marks.append(contrib)
    setattr(func, CONTRIB_ATTR, marks)
    return func


def tool(
    name: str,
    *,
    description: str = "",
    schema: Optional[dict[str, Any]] = None,
    permission: str = "read",
    timeout_ms: int = 20_000,
    effect: Optional[str] = None,
    output_schema: Optional[dict[str, Any]] = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Declare a method as a contributed tool.

    `permission` is the tool effect class (`read`/`write`/`external_action`);
    a non-read tool is routed through Eclipse's existing effect authorizer, so
    the operator's approval flow applies unchanged.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        return _mark(func, {
            "kind": "tool",
            "name": name,
            "description": description or (func.__doc__ or "").strip(),
            "schema": schema or {"type": "object", "properties": {}},
            "permission": permission,
            "timeout_ms": timeout_ms,
            "effect": effect or (None if permission == "read" else permission),
            "output_schema": output_schema,
        })

    return decorator


def skill(name: str, *, description: str = "", path: Optional[str] = None,
          instructions: str = "") -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Declare a contributed skill. The method (if given) returns the SKILL.md
    body, or `path` names a file relative to the plugin package."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        return _mark(func, {
            "kind": "skill",
            "name": name,
            "description": description or (func.__doc__ or "").strip(),
            "path": path,
            "instructions": instructions,
        })

    return decorator


def hook(name: str, *, order: int = 100, on_error: str = "continue", timeout_ms: int = 5_000,
         ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Declare a hook handler for a named lifecycle point.

    `order` controls execution order (lower runs first); `on_error` is
    `continue` (the run proceeds) or `abort` (the hook may veto the operation).
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        return _mark(func, {
            "kind": "hook",
            "name": name,
            "order": order,
            "on_error": on_error,
            "timeout_ms": timeout_ms,
        })

    return decorator


def command(name: str, *, description: str = "",
            schema: Optional[dict[str, Any]] = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        return _mark(func, {
            "kind": "command",
            "name": name,
            "description": description or (func.__doc__ or "").strip(),
            "schema": schema or {"type": "object", "properties": {}},
        })

    return decorator


def event_handler(pattern: str, *, order: int = 100) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Subscribe to events whose type matches `pattern` (exact, `prefix.*`, or `*`)."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        return _mark(func, {"kind": "event", "pattern": pattern, "order": order})

    return decorator


def worker(name: str, *, interval_seconds: int = 0) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Declare a background worker started on enable and stopped on disable."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        return _mark(func, {"kind": "worker", "name": name, "interval_seconds": interval_seconds})

    return decorator


def webhook(webhook_id: str, *, description: str = "",
            direction: str = "inbound") -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Declare a webhook. `inbound` = Eclipse receives a signed delivery;
    `outbound` = the plugin delivers to a configured target via `ctx.webhooks`."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        return _mark(func, {"kind": "webhook", "id": webhook_id, "description": description,
                            "direction": direction})

    return decorator


def data_provider(name: str, *, kinds: Optional[list[str]] = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Declare a contributed data provider (market/fundamentals/filings/news)."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        return _mark(func, {"kind": "data_provider", "name": name, "kinds": kinds or []})

    return decorator


def research_provider(name: str, *, kinds: Optional[list[str]] = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Declare a contributed research provider whose results become evidence."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        return _mark(func, {"kind": "research_provider", "name": name, "kinds": kinds or []})

    return decorator


def agent_contribution(agent_id: str, *, description: str = "") -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Declare a contributed agent preset."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        return _mark(func, {"kind": "agent", "id": agent_id, "description": description})

    return decorator


def workflow_contribution(workflow_id: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        return _mark(func, {"kind": "workflow", "id": workflow_id})

    return decorator


def prompt_section(section_id: str, *, scope: str = "", order: int = 100,
                   ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Declare a prompt section. The handler returns the section text."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        return _mark(func, {
            "kind": "prompt",
            "id": section_id,
            "scope": scope,
            "order": order,
        })

    return decorator


def model_provider(name: str, *, provider: str = "") -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Declare a model/provider contribution. The handler returns its descriptor."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        return _mark(func, {"kind": "model", "name": name, "provider": provider})

    return decorator


def ui_panel(kind: str, panel_id: str, *, title: str = "", entry: Optional[str] = None,
             ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        return _mark(func, {"kind": "ui", "ui_kind": kind, "id": panel_id, "title": title, "entry": entry})

    return decorator


class Plugin:
    """Base class for a Python plugin.

    Lifecycle methods are optional and receive the `PluginContext`. `on_load`
    runs once when the plugin is loaded; `on_enable`/`on_disable` bracket the
    active period; `on_unload` runs on uninstall. All are best-effort from the
    manager's perspective but a raised exception in `on_enable` fails the enable.
    """

    #: Optional override; the manifest is authoritative and cross-checked.
    id: str = ""
    version: str = ""

    async def on_load(self, ctx: Any) -> None:  # pragma: no cover - default no-op
        return None

    async def on_enable(self, ctx: Any) -> None:  # pragma: no cover
        return None

    async def on_disable(self, ctx: Any) -> None:  # pragma: no cover
        return None

    async def on_unload(self, ctx: Any) -> None:  # pragma: no cover
        return None

    def provided_services(self) -> list[str]:
        """Service keys this plugin actually provides. Must match the manifest."""
        return []

    def contributions(self) -> list[dict[str, Any]]:
        """Every contribution declared on this instance, in declaration order."""
        found: list[dict[str, Any]] = []
        for attr_name in dir(self):
            if attr_name.startswith("__"):
                continue
            try:
                member = getattr(self, attr_name)
            except Exception:  # noqa: BLE001 - a property raising must not break discovery
                continue
            marks = getattr(member, CONTRIB_ATTR, None)
            if not marks:
                continue
            for mark in marks:
                found.append({**mark, "handler": member, "attr": attr_name})
        return found


__all__ = [
    "Plugin",
    "PluginToolError",
    "tool",
    "skill",
    "hook",
    "command",
    "event_handler",
    "worker",
    "webhook",
    "ui_panel",
    "data_provider",
    "research_provider",
    "agent_contribution",
    "workflow_contribution",
    "prompt_section",
    "model_provider",
    "SDK_VERSION",
    "CONTRIB_ATTR",
]
