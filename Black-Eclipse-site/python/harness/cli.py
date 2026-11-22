"""The Eclipse CLI — a thin, structured command surface over the Application Runtime.

Usage (run from `Black-Eclipse-site/python/` with the runtime interpreter):

    python -m harness.cli runtime detect
    python -m harness.cli runtime inspect [--root PATH]
    python -m harness.cli runtime start  [--root PATH] [--no-install] [--build] [--compose]
    python -m harness.cli runtime status RUNTIME_ID
    python -m harness.cli runtime stop   RUNTIME_ID
    python -m harness.cli runtime restart RUNTIME_ID
    python -m harness.cli runtime health RUNTIME_ID
    python -m harness.cli runtime logs   RUNTIME_ID [--tail N]
    python -m harness.cli runtime diagnose RUNTIME_ID
    python -m harness.cli runtime recover RUNTIME_ID
    python -m harness.cli runtime reconcile
    python -m harness.cli package detect|version|list|install|build|test [--root PATH]
    python -m harness.cli container ps|logs|exec|images ...
    python -m harness.cli compose up|down|ps|logs ...
    python -m harness.cli environment inspect|validate [--root PATH]
    python -m harness.cli port list|check PORT|find-free
    python -m harness.cli k8s get|logs|apply|delete ... [--authorized]
    python -m harness.cli terraform validate|plan|apply|destroy ... [--authorized]
    python -m harness.cli deploy run|status|logs|rollback ... [--authorized]

It is a REAL CLI, not a shell wrapper: it builds the SAME process-wide
`ApplicationRuntimeManager` the agent tools and the API use, so the CLI, the agent
and the UI can never disagree. Output is JSON by default (`--json`), a plain
one-line summary otherwise. Destructive commands print a structured refusal unless
`--authorized` is passed.

This module adds NO execution logic; it is a thin adapter over the runtime.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional


def _build_manager(root: Optional[str]) -> Any:
    """Build a standalone runtime manager for a CLI invocation."""
    from .execution.application.manager import ApplicationRuntimeManager, build_runtime_registry
    from .execution.application.store import ApplicationRuntimeStore
    from .persistence import AgentMemoryStore
    from .terminal.runtime import build_terminal_manager, set_terminal_manager

    store = AgentMemoryStore()
    terminal = build_terminal_manager(store=None, bus=_Bus())
    set_terminal_manager(terminal)
    manager = ApplicationRuntimeManager(
        registry=build_runtime_registry(), terminal=terminal,
        store=ApplicationRuntimeStore(store), bus=_Bus(),
    )
    return manager


class _Bus:
    """A no-op event bus: the CLI prints results directly; it has no SSE consumer."""

    def publish(self, _event: dict[str, Any]) -> None:
        return None


def _emit(args: argparse.Namespace, data: Any, summary: str) -> int:
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, default=str))
        return 0
    print(summary)
    return 0


def _refusal(code: str, reason: str) -> int:
    print(f"REFUSED ({code}): {reason}", file=sys.stderr)
    return 2


def _run(coro: Any) -> Any:
    """Run a coroutine from synchronous CLI code on a fresh loop.

    Avoids the deprecated implicit `asyncio.get_event_loop()` and works whether
    or not a loop happens to exist in the current thread.
    """
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _resolve_root(value: Optional[str]) -> Path:
    if value:
        return Path(value).resolve()
    return Path.cwd()


# ── command handlers ─────────────────────────────────────────────────────────────


def _cmd_runtime(args: argparse.Namespace, manager: Any) -> int:
    action = args.action
    root = _resolve_root(args.root)
    if action == "detect":
        from .execution.application.environment import EnvironmentDetector

        data = EnvironmentDetector().detect()
        available = [n for n, info in data.get("tools", {}).items() if info.get("available")]
        return _emit(args, data, f"{len(available)} tool(s) available")
    if action == "capabilities":
        data = manager.describe()
        return _emit(args, data, "capabilities reported")
    if action == "inspect":
        data = manager.inspect(root)
        desc = data.get("descriptor", {})
        return _emit(args, data, f"{desc.get('name')} — {desc.get('framework') or desc.get('language') or 'project'}")
    if action == "start":
        handle = manager.start(
            root, install=not args.no_install, build=bool(args.build),
            use_compose=(True if args.compose else None), wait_ready=not args.no_wait,
        )
        if handle.state.value == "FAILED":
            return _refusal((handle.last_error or {}).get("category", "FAILED"),
                            (handle.last_error or {}).get("message", "could not start"))
        return _emit(args, handle.to_dict(), f"started {handle.runtime_id} ({handle.state.value})")
    if action == "status" or action == "health":
        data = manager.status(args.runtime_id)
        if data.get("status") == "not_found":
            return _refusal("NOT_FOUND", "no such runtime")
        runtime = data.get("runtime", {})
        return _emit(args, data, f"{args.runtime_id}: {runtime.get('state')} health={runtime.get('health', {}).get('status')}")
    if action == "stop":
        data = manager.stop(args.runtime_id)
        return _emit(args, data, f"stopped {args.runtime_id}") if data.get("status") == "ok" else _refusal("NOT_FOUND", "no such runtime")
    if action == "restart":
        try:
            handle = manager.restart(args.runtime_id)
        except KeyError:
            return _refusal("NOT_FOUND", "no such runtime")
        return _emit(args, handle.to_dict(), f"restarted {handle.runtime_id}")
    if action == "cleanup":
        data = manager.cleanup(args.runtime_id)
        return _emit(args, data, f"cleaned {args.runtime_id}") if data.get("status") == "ok" else _refusal("NOT_FOUND", "no such runtime")
    if action == "logs":
        data = manager.logs(args.runtime_id, tail=args.tail)
        if data.get("status") == "not_found":
            return _refusal("NOT_FOUND", "no such runtime")
        text = "\n".join(s.get("text", "") for s in data.get("streams", []))
        return _emit(args, data, text or "no output")
    if action == "diagnose":
        data = manager.diagnose(args.runtime_id)
        if data.get("status") == "not_found":
            return _refusal("NOT_FOUND", "no such runtime")
        diag = data.get("diagnosis", {})
        return _emit(args, data, f"{diag.get('category')}: {diag.get('message')}")
    if action == "recover":
        data = manager.recover(args.runtime_id, max_attempts=args.max_attempts)
        if data.get("status") == "not_found":
            return _refusal("NOT_FOUND", "no such runtime")
        rec = data.get("recovery", {})
        return _emit(args, data, f"recovered={rec.get('recovered')} attempts={rec.get('attempts')}")
    if action == "reconcile":
        data = manager.reconcile()
        return _emit(args, data, f"reconciled {data.get('count', 0)} runtime(s)")
    if action == "list":
        data = manager.list()
        return _emit(args, {"runtimes": data}, f"{len(data)} runtime(s)")
    if action == "metrics":
        data = manager.metrics(args.runtime_id)
        if data.get("status") == "not_found":
            return _refusal("NOT_FOUND", "no such runtime")
        return _emit(args, data, f"{args.runtime_id}: uptime {int(data.get('uptimeS', 0))}s")
    if action == "metrics-history":
        data = manager.metrics_history(args.runtime_id, limit=args.tail)
        trend = data.get("trend", {})
        directions = ", ".join(f"{k}={v.get('direction')}" for k, v in trend.items())
        return _emit(args, data, f"{args.runtime_id} trend: {directions or 'no samples yet'}")
    return _refusal("UNKNOWN_ACTION", f"unknown runtime action {action!r}")


def _cmd_package(args: argparse.Namespace, manager: Any) -> int:
    from .execution.application.tools import _HANDLERS
    import asyncio

    root = _resolve_root(args.root)
    ctx = _cli_context(root, manager, args.authorized)
    mapping = {
        "detect": "package_detect", "version": "package_version", "list": "package_list",
        "install": "package_install", "add": "package_add", "remove": "package_remove",
        "update": "package_update", "build": "package_build", "test": "package_test",
        "audit": "package_audit", "run": "package_run",
    }
    name = mapping.get(args.action)
    if name is None:
        return _refusal("UNKNOWN_ACTION", f"unknown package action {args.action!r}")
    payload: dict[str, Any] = {}
    if args.manager:
        payload["manager"] = args.manager
    if getattr(args, "packages", None):
        payload["packages"] = args.packages
    if getattr(args, "script", None):
        payload["script"] = args.script
    if args.action in ("build", "test"):
        payload[args.action] = True
    text, data = _run(_HANDLERS[name](payload, ctx))
    if data.get("status") == "refused":
        return _refusal(data.get("code", "REFUSED"), data.get("reason", ""))
    return _emit(args, data, text)


def _cmd_environment(args: argparse.Namespace, manager: Any) -> int:
    import asyncio

    from .execution.application.tools import _HANDLERS

    root = _resolve_root(args.root)
    ctx = _cli_context(root, manager, args.authorized)
    mapping = {"inspect": "environment_inspect", "validate": "environment_validate",
               "load": "environment_load", "compare": "environment_compare",
               "set": "environment_set", "unset": "environment_unset"}
    name = mapping.get(args.action)
    if name is None:
        return _refusal("UNKNOWN_ACTION", f"unknown environment action {args.action!r}")
    payload: dict[str, Any] = {}
    for field_name in ("file", "name", "value", "left", "right"):
        value = getattr(args, field_name, None)
        if value:
            payload[field_name] = value
    text, data = _run(_HANDLERS[name](payload, ctx))
    if data.get("status") == "refused":
        return _refusal(data.get("code", "REFUSED"), data.get("reason", ""))
    return _emit(args, data, text)


def _cmd_workspace(args: argparse.Namespace, manager: Any) -> int:
    """The cross-application graph + ordered start/stop (a monorepo workflow)."""
    root = _resolve_root(args.root)
    if args.action == "graph":
        data = manager.app_graph(root)
        order = data.get("startupOrder", [])
        return _emit(args, data, f"{len(data.get('nodes', []))} app(s); start order: {', '.join(order)}")
    if args.action == "start":
        data = manager.start_workspace(root, install=not args.no_install, build=args.build)
        if data.get("status") == "failed":
            return _refusal("WORKSPACE_START_FAILED", data.get("reason", "an application could not start"))
        return _emit(args, data, f"started {len(data.get('started', []))} app(s)")
    if args.action == "stop":
        data = manager.stop_workspace(root)
        return _emit(args, data, f"stopped {len(data.get('stopped', []))} app(s)")
    return _refusal("UNKNOWN_ACTION", f"unknown workspace action {args.action!r}")


def _cmd_providers(args: argparse.Namespace, manager: Any) -> int:
    """The provider install/auth readiness report.

    With no `--root`, reports every provider. With `--root PATH`, adds the
    providers the project at PATH actually implies (docker for a compose file,
    kubectl/helm for a k8s/ or helm/ folder, terraform for *.tf).
    """
    root = _resolve_root(args.root) if getattr(args, "root", None) else None
    data = manager.readiness(root)
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, default=str))
        return 0
    state = "ready" if data.get("ready") else "not ready"
    print(f"{state}: {len(data.get('providers', []))} provider(s)")
    if data.get("missing"):
        print(f"  not installed: {', '.join(data['missing'])}")
    if data.get("unauthenticated"):
        print(f"  not signed in: {', '.join(data['unauthenticated'])}")
    for hint in data.get("hints", []):
        print(f"  - {hint}")
    return 0


def _cmd_port(args: argparse.Namespace, manager: Any) -> int:
    if args.action == "list":
        data = manager.ports.summary()
        return _emit(args, data, f"{data.get('count', 0)} listening port(s)")
    if args.action == "check":
        data = manager.ports.check(args.port)
        return _emit(args, data, f"port {args.port}: {'in use' if data.get('inUse') else 'free'}")
    if args.action == "find-free":
        record = manager.ports.allocate(preferred=args.preferred or 0, owner="cli")
        return _emit(args, record.to_dict(), f"free port: {record.port}")
    if args.action == "identify":
        conflict = manager.ports.identify(args.port)
        if conflict is None:
            return _emit(args, {"port": args.port, "inUse": False}, f"port {args.port} is free")
        return _emit(args, conflict.to_dict(), f"held by {conflict.owner}")
    return _refusal("UNKNOWN_ACTION", f"unknown port action {args.action!r}")


def _cmd_container(args: argparse.Namespace, manager: Any) -> int:
    engine = manager.container()
    if engine is None:
        return _refusal("ENGINE_UNAVAILABLE", "no container engine (Docker or Podman) is available")
    if args.action == "ps":
        result = engine.ps(all=not args.running)
    elif args.action == "logs":
        result = engine.logs(args.container, tail=args.tail)
    elif args.action == "exec":
        result = engine.exec(args.container, args.command)
    elif args.action == "images":
        result = engine.images()
    elif args.action == "inspect":
        result = engine.inspect(args.container)
    else:
        return _refusal("UNKNOWN_ACTION", f"unknown container action {args.action!r}")
    data = result.to_dict()
    if not result.ok:
        return _refusal(result.code, result.reason or result.output[-400:])
    return _emit(args, data, result.output[-400:] or "ok")


def _cmd_compose(args: argparse.Namespace, manager: Any) -> int:
    compose = manager.compose_adapter()
    if compose is None:
        return _refusal("ENGINE_UNAVAILABLE", "no compose engine is available")
    root = _resolve_root(args.root)
    from .execution.application._tool_common import default_compose_file

    compose_file = default_compose_file(root)
    if args.action == "up":
        result = compose.up(cwd=root, compose_file=compose_file, detach=True, build=args.build)
    elif args.action == "down":
        result = compose.down(cwd=root, compose_file=compose_file, volumes=args.volumes)
    elif args.action == "ps":
        result = compose.ps(cwd=root, compose_file=compose_file)
    elif args.action == "logs":
        result = compose.logs(cwd=root, compose_file=compose_file, service=args.service, tail=args.tail)
    else:
        return _refusal("UNKNOWN_ACTION", f"unknown compose action {args.action!r}")
    data = result.to_dict()
    if not result.ok:
        return _refusal(result.code, result.reason or result.output[-400:])
    return _emit(args, data, result.output[-400:] or "ok")


def _cmd_k8s(args: argparse.Namespace, manager: Any) -> int:
    operation = {"get": "get", "pods": "get_pods", "logs": "logs", "describe": "describe",
                 "apply": "apply", "delete": "delete", "scale": "scale", "exec": "exec"}.get(args.action)
    if operation is None:
        return _refusal("UNKNOWN_ACTION", f"unknown k8s action {args.action!r}")
    if operation in ("apply", "delete", "scale", "exec") and not args.authorized:
        return _refusal("AUTHORIZATION_REQUIRED", "this Kubernetes mutation needs --authorized")
    payload = {k: v for k, v in vars(args).items()
               if k in ("kind", "name", "namespace", "selector", "container", "file", "replicas", "command") and v}
    result = manager.run_orchestrator(operation, payload)
    if isinstance(result, dict):
        return _refusal(result.get("code", "REFUSED"), result.get("reason", ""))
    if not result.ok:
        return _refusal(result.code, result.reason or result.output[-400:])
    return _emit(args, result.to_dict(), result.output[-400:] or "ok")


def _cmd_terraform(args: argparse.Namespace, manager: Any) -> int:
    if args.action in ("apply", "destroy") and not args.authorized:
        return _refusal("AUTHORIZATION_REQUIRED", "this Terraform operation needs --authorized")
    payload = {"autoApprove": args.auto_approve}
    if args.dir:
        payload["dir"] = args.dir
    result = manager.run_infrastructure(args.action, payload)
    if isinstance(result, dict):
        return _refusal(result.get("code", "REFUSED"), result.get("reason", ""))
    if not result.ok:
        return _refusal(result.code, result.reason or result.output[-400:])
    return _emit(args, result.to_dict(), result.output[-400:] or "ok")


def _cmd_deploy(args: argparse.Namespace, manager: Any) -> int:
    root = _resolve_root(args.root)
    if args.action == "run":
        outcome = manager.deployment.deploy(
            root, provider=args.provider, environment=args.environment,
            service=args.service, install=not args.no_install, build=args.build,
            authorize=args.authorized,
        )
        data = outcome.to_dict()
        if outcome.status == "failed":
            return _refusal((outcome.error or {}).get("code", "DEPLOY_FAILED"),
                            (outcome.error or {}).get("message", "failed"))
        return _emit(args, data, f"deployment {outcome.status}")
    if args.action == "status":
        result = manager.deployment.status(provider=args.provider, service=args.service)
    elif args.action == "logs":
        result = manager.deployment.logs(provider=args.provider, service=args.service)
    elif args.action == "rollback":
        result = manager.deployment.rollback(provider=args.provider, service=args.service)
    else:
        return _refusal("UNKNOWN_ACTION", f"unknown deploy action {args.action!r}")
    if isinstance(result, dict):
        return _refusal(result.get("code", "REFUSED"), result.get("reason", ""))
    if not result.ok:
        return _refusal(result.code, result.reason or result.output[-400:])
    return _emit(args, result.to_dict(), result.output[-400:] or "ok")


def _cmd_computer(args: argparse.Namespace, manager: Any) -> int:
    """The Computer Use CLI: doctor/status/capabilities/windows/apps/sessions/replay.

    Builds the SAME process-wide `ComputerManager` the agent tools and the API use,
    so the CLI, the agent and the UI can never disagree. Read-only by default; a
    destructive desktop action is not offered here.
    """
    import asyncio

    from .computer.config import computer_config
    from .computer.manager import ComputerManager

    mgr = ComputerManager(config=computer_config(refresh=True))
    action = args.action
    if action == "replay":
        from .computer.replay import Replayer  # noqa: PLC0415

        trajectory = Replayer.load(args.path)
        return _emit(args, Replayer().inspect(trajectory).to_dict(), "replayed (inspect mode)")
    try:
        _run(mgr.start())
    except Exception as exc:  # noqa: BLE001 - a driver failure is a clean CLI error
        return _refusal("DRIVER_UNAVAILABLE", f"the desktop driver could not start: {type(exc).__name__}")

    if action == "doctor":
        report = mgr.doctor()
        from .computer.diagnostics import format_report  # noqa: PLC0415

        return _emit(args, report, format_report(report))
    if action == "status":
        return _emit(args, mgr.health(), f"driver={mgr.driver.id} sessions={len(mgr.list_sessions())}")
    if action == "capabilities":
        caps = mgr.capabilities()
        return _emit(args, caps, f"driver: {caps['driver']}")
    if action == "windows":
        windows = [w.to_dict() for w in mgr.driver.list_windows()]
        return _emit(args, {"windows": windows}, "\n".join(f"{w['id']} {w['application']} — {w['title']}" for w in windows) or "no windows")
    if action == "apps":
        apps = [a.to_dict() for a in mgr.driver.list_applications()]
        return _emit(args, {"applications": apps}, "\n".join(f"{a['name']} (pid {a['pid']})" for a in apps) or "no applications")
    if action == "displays":
        displays = [d.to_dict() for d in mgr.driver.list_displays()]
        return _emit(args, {"displays": displays}, f"{len(displays)} display(s)")
    if action == "sessions":
        sessions = mgr.list_sessions()
        return _emit(args, {"sessions": sessions}, f"{len(sessions)} session(s)")
    return _refusal("UNKNOWN_ACTION", f"unknown computer action {action!r}")


def _build_plugin_manager() -> Any:
    """Build a standalone PluginManager over the runtime's own store + registry.

    Uses the SAME manager the runtime uses (same DB, same plugin root), so the
    CLI and the running app can never disagree about installed plugins.
    """
    from .events import EventBus
    from .persistence import AgentMemoryStore
    from .plugins.manager import PluginManager
    from .tools import ToolRegistry

    store = AgentMemoryStore()
    return PluginManager(store, ToolRegistry(), EventBus(), None)


def _build_ui_service() -> Any:
    """Build a standalone UI service over the runtime's own agent-memory store.

    Uses the SAME DB the runtime uses (same blueprints, same catalog), so the CLI
    and the running app can never disagree about saved UI state.
    """
    from .persistence import AgentMemoryStore
    from .ui.catalog import UiCatalog
    from .ui.service import UiService
    from .ui.store import UiStore

    return UiService(UiStore(AgentMemoryStore()), UiCatalog())


def _cmd_ui(args: argparse.Namespace, _manager: Any) -> int:
    """The UI Engineering CLI: catalog + blueprint inspection, export/import."""
    import json as _json
    from pathlib import Path

    from .ui.model import CreatePageInput, RestoreRevisionInput

    service = _build_ui_service()
    action = args.action
    target = args.target or ""

    if action == "components":
        specs = service.list_components()
        return _emit(args, {"components": [s.model_dump(by_alias=True) for s in specs]}, f"{len(specs)} component(s)")
    if action == "capabilities":
        caps = service.get_capabilities()
        return _emit(args, caps, f"{caps['componentCount']} component(s)")
    if action == "blueprints":
        listing = service.list_blueprints()
        return _emit(args, listing, f"{listing['total']} blueprint(s)")
    if action == "show":
        blueprint = service.get_blueprint(target)
        return _emit(args, {"blueprint": blueprint.model_dump(by_alias=True)}, f"{blueprint.title} r{blueprint.revision}")
    if action == "history":
        return _emit(args, service.history(target), f"history for {target}")
    if action == "create":
        blueprint = service.create_page(CreatePageInput(title=target or "Untitled"))
        return _emit(args, {"blueprint": blueprint.model_dump(by_alias=True)}, f"created {blueprint.id}")
    if action == "classify":
        return _emit(args, service.classify_request(target), "classified")
    if action == "restore":
        if args.revision <= 0:
            return _refusal("MISSING_REVISION", "restore requires --revision <n>")
        blueprint = service.restore_revision(RestoreRevisionInput(blueprint_id=target, revision=args.revision))
        return _emit(args, {"blueprint": blueprint.model_dump(by_alias=True)}, f"restored to r{blueprint.revision}")
    if action == "export":
        data = service.export_blueprint(target)
        if args.path:
            Path(args.path).write_text(_json.dumps(data, indent=2), encoding="utf-8")
            return _emit(args, {"path": args.path}, f"exported to {args.path}")
        return _emit(args, data, "exported")
    if action == "import":
        if not target:
            return _refusal("MISSING_SOURCE", "import requires a file path")
        payload = _json.loads(Path(target).read_text(encoding="utf-8"))
        blueprint = service.import_blueprint(payload)
        return _emit(args, {"blueprint": blueprint.model_dump(by_alias=True)}, f"imported {blueprint.id}")
    return _refusal("UNKNOWN_ACTION", f"unknown ui action {action!r}")


def _cmd_plugin(args: argparse.Namespace, _manager: Any) -> int:
    """The plugin CLI: lifecycle + developer commands. Real, not a placeholder."""
    import asyncio
    import json as _json

    from .plugins.loader import checksum, read_manifest

    manager = _build_plugin_manager()
    action = args.action
    target = args.target or ""
    source = args.source or ""

    if action == "list":
        return _emit(args, {"plugins": manager.list(), "summary": manager.summary()},
                     f"{len(manager.list())} plugin(s)")
    if action == "available":
        return _emit(args, {"plugins": manager.available()}, f"{len(manager.available())} available")
    if action == "search":
        results = manager.search(target)
        return _emit(args, {"results": results}, f"{len(results)} match(es)")
    if action == "capabilities":
        caps = manager.capabilities()
        return _emit(args, {"capabilities": caps}, f"{len(caps)} capability(ies)")
    if action == "install":
        if not target:
            return _refusal("MISSING_TARGET", "install requires a source path or plugin id")
        try:
            result = _run(
                manager.install(target, enable=args.enable, operator=True, trust=args.trust, replace=True)
            )
        except Exception as exc:  # noqa: BLE001
            return _refusal(getattr(exc, "code", "INSTALL_FAILED"), str(exc))
        return _emit(args, result, f"installed {result.get('plugin', {}).get('name', target)}")
    if action == "uninstall":
        try:
            result = _run(manager.uninstall(target))
        except Exception as exc:  # noqa: BLE001
            return _refusal(getattr(exc, "code", "UNINSTALL_FAILED"), str(exc))
        return _emit(args, result, f"uninstalled {target}")
    if action == "enable":
        try:
            result = _run(manager.enable(target, operator=True))
        except Exception as exc:  # noqa: BLE001
            return _refusal(getattr(exc, "code", "ENABLE_FAILED"), str(exc))
        return _emit(args, result, f"enabled {target}")
    if action == "disable":
        try:
            result = _run(manager.disable(target))
        except Exception as exc:  # noqa: BLE001
            return _refusal(getattr(exc, "code", "DISABLE_FAILED"), str(exc))
        return _emit(args, result, f"disabled {target}")
    if action == "update":
        if not source:
            return _refusal("MISSING_SOURCE", "update requires <plugin-id> <source>")
        try:
            result = _run(manager.update(target, source, operator=True))
        except Exception as exc:  # noqa: BLE001
            return _refusal(getattr(exc, "code", "UPDATE_FAILED"), str(exc))
        return _emit(args, result, f"updated {target}")
    if action == "rollback":
        try:
            result = _run(manager.rollback(target, operator=True))
        except Exception as exc:  # noqa: BLE001
            return _refusal(getattr(exc, "code", "ROLLBACK_FAILED"), str(exc))
        return _emit(args, result, f"rolled back {target}")
    if action == "repair":
        try:
            result = _run(manager.repair(target, operator=True))
        except Exception as exc:  # noqa: BLE001
            return _refusal(getattr(exc, "code", "REPAIR_FAILED"), str(exc))
        return _emit(args, result, f"repaired {target}")
    if action == "inspect":
        try:
            data = manager.inspect(target)
        except Exception as exc:  # noqa: BLE001
            return _refusal(getattr(exc, "code", "NOT_FOUND"), str(exc))
        return _emit(args, data, f"{data['plugin']['name']} {data['plugin']['version']} [{data['plugin']['status']}]")
    if action == "health":
        if target:
            return _emit(args, manager.health_report(target), f"{target}: {manager.health_report(target)['status']}")
        from .plugins import health as health_mod

        data = health_mod.summarize(manager.store, manager.store.list_plugins())
        return _emit(args, data, f"{data['enabled']}/{data['installed']} enabled, {data['unhealthy']} unhealthy")
    if action == "doctor":
        report = manager.doctor(target)
        return _emit(args, report, report["summary"])
    if action == "logs":
        logs = manager.logs(target)
        return _emit(args, {"logs": logs}, "\n".join(f"{row['at']} {row['level']}: {row['message']}" for row in logs) or "no logs")
    if action == "config":
        if source:
            data = manager.set_config(target, _json.loads(source))
            return _emit(args, data, "config updated")
        return _emit(args, manager.config_view(target), "config")
    if action == "validate":
        path = Path(target)
        try:
            manifest, _raw = read_manifest(path)
        except Exception as exc:  # noqa: BLE001
            return _refusal(getattr(exc, "code", "MANIFEST_INVALID"), str(exc))
        return _emit(args, {"valid": True, "manifest": manifest.to_dict()},
                     f"valid: {manifest.id} {manifest.version}")
    if action == "create":
        return _scaffold_plugin(args, target)
    if action == "package":
        path = Path(target)
        try:
            manifest, _raw = read_manifest(path)
        except Exception as exc:  # noqa: BLE001
            return _refusal(getattr(exc, "code", "MANIFEST_INVALID"), str(exc))
        digest = checksum(path)
        payload = {"id": manifest.id, "version": manifest.version, "checksum": digest}
        (path / ".eclipse-plugin.json").write_text(_json.dumps(payload, indent=2), encoding="utf-8")
        return _emit(args, payload, f"packaged {manifest.id} ({digest[:12]})")
    if action == "dev":
        try:
            _run(
                manager.install(target, enable=True, operator=True, replace=True)
            )
        except Exception as exc:  # noqa: BLE001
            return _refusal(getattr(exc, "code", "DEV_FAILED"), str(exc))
        return _emit(args, manager.inspect(target), f"dev mode: {target} installed and enabled")
    if action == "auth":
        try:
            result = _run(manager.oauth_start(target))
        except Exception as exc:  # noqa: BLE001
            return _refusal(getattr(exc, "code", "AUTH_FAILED"), str(exc))
        summary = result.get("authorizationUrl") or (
            f"visit {result.get('verificationUri')} and enter code {result.get('userCode')}"
        )
        return _emit(args, result, f"authorize: {summary}")
    if action == "auth-status":
        try:
            data = manager.auth_status(target)
        except Exception as exc:  # noqa: BLE001
            return _refusal(getattr(exc, "code", "AUTH_FAILED"), str(exc))
        return _emit(args, data, f"{target}: authorized={data.get('authorized')}")
    if action == "auth-complete":
        try:
            result = _run(manager.oauth_complete(target, args.code or source, args.state))
        except Exception as exc:  # noqa: BLE001
            return _refusal(getattr(exc, "code", "AUTH_FAILED"), str(exc))
        return _emit(args, result, "authorization complete")
    if action == "auth-refresh":
        try:
            result = _run(manager.oauth_refresh(target))
        except Exception as exc:  # noqa: BLE001
            return _refusal(getattr(exc, "code", "AUTH_FAILED"), str(exc))
        return _emit(args, result, "token refreshed")
    if action == "logout":
        try:
            result = _run(manager.oauth_revoke(target))
        except Exception as exc:  # noqa: BLE001
            return _refusal(getattr(exc, "code", "AUTH_FAILED"), str(exc))
        return _emit(args, result, f"credentials cleared for {target}")
    if action == "registry-search":
        try:
            results = manager.registry_search(target, source=args.registry)
        except Exception as exc:  # noqa: BLE001
            return _refusal(getattr(exc, "code", "REGISTRY_FAILED"), str(exc))
        return _emit(args, {"results": results}, f"{len(results)} registry match(es)")
    if action == "registry-install":
        try:
            result = _run(manager.install_from_registry(
                target, source=args.registry, version=args.version, enable=args.enable, operator=True))
        except Exception as exc:  # noqa: BLE001
            return _refusal(getattr(exc, "code", "REGISTRY_FAILED"), str(exc))
        return _emit(args, result, f"installed {target} from registry")
    if action == "webhooks":
        return _emit(args, {"webhooks": manager.webhook_targets(target)}, "webhook targets")
    if action == "webhook-target":
        try:
            result = manager.set_webhook_target(target, args.webhook, args.url, signing_secret=args.secret)
        except Exception as exc:  # noqa: BLE001
            return _refusal(getattr(exc, "code", "WEBHOOK_FAILED"), str(exc))
        return _emit(args, result, f"target set for {target}/{args.webhook}")
    return _refusal("UNKNOWN_ACTION", f"unknown plugin action {action!r}")


def _scaffold_plugin(args: argparse.Namespace, target: str) -> int:
    """Write a starter plugin (manifest + entrypoint) to `target`."""
    import json as _json

    if not target:
        return _refusal("MISSING_TARGET", "create requires a target directory")
    root = Path(target)
    plugin_id = getattr(args, "id", None) or root.name.replace("_", "-").lower()
    root.mkdir(parents=True, exist_ok=True)
    manifest = f"""id: {plugin_id}
name: {plugin_id}
version: 0.1.0
description: A new BlackEclipse plugin.
runtime:
  type: python
  entrypoint: plugin:PluginImpl
permissions: []
contributes:
  tools:
    - name: hello
      description: Say hello.
"""
    (root / "plugin.yaml").write_text(manifest, encoding="utf-8")
    (root / "plugin.py").write_text(
        'from harness.plugins.sdk import Plugin, tool\n\n\n'
        "class PluginImpl(Plugin):\n"
        '    @tool(name="hello", description="Say hello.", schema={"type": "object", "properties": {}})\n'
        "    async def hello(self, args, ctx):\n"
        "        return {'hello': 'world'}\n\n\n"
        "plugin = PluginImpl\n",
        encoding="utf-8",
    )
    return _emit(args, {"created": str(root), "id": plugin_id}, f"created plugin at {root}")


def _cli_context(root: Path, manager: Any, authorized: bool) -> Any:
    from .code.policy import WorkspaceBinding
    from .code.workspace import ResolvedWorkspace
    from .tools import ToolContext

    binding = WorkspaceBinding(id="cli", root=root, base_revision="", strategy="direct")
    resolved = ResolvedWorkspace(id="cli", root=root, base_revision="", strategy="direct", binding=binding)
    ctx = ToolContext(run_id="", log=lambda *_: None, workspace=resolved,
                      autonomy="approve_external", direct=True)
    # A CLI invocation is an operator act: `--authorized` sets the operator bit the
    # tools check before a destructive operation.
    try:
        ctx.operator = bool(authorized)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    return ctx


# ── argument parsing ──────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eclipse", description="Eclipse application runtime CLI")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    sub = parser.add_subparsers(dest="group", required=True)

    # `--json` is accepted BOTH before the group and after the action, so
    # `eclipse runtime inspect --json` and `eclipse --json runtime inspect` agree.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    runtime = sub.add_parser("runtime", parents=[common], help="application runtime")
    runtime.add_argument("action", choices=[
        "detect", "capabilities", "inspect", "start", "status", "health", "stop",
        "restart", "cleanup", "logs", "diagnose", "recover", "reconcile", "list",
        "metrics", "metrics-history"])
    runtime.add_argument("runtime_id", nargs="?", default="")
    runtime.add_argument("--root")
    runtime.add_argument("--no-install", action="store_true")
    runtime.add_argument("--build", action="store_true")
    runtime.add_argument("--compose", action="store_true")
    runtime.add_argument("--no-wait", action="store_true")
    runtime.add_argument("--tail", type=int, default=200)
    runtime.add_argument("--max-attempts", type=int, default=None)

    package = sub.add_parser("package", parents=[common], help="package managers")
    package.add_argument("action", choices=["detect", "version", "list", "install", "add", "remove", "update", "build", "test", "audit", "run"])
    package.add_argument("--root")
    package.add_argument("--manager", default="")
    package.add_argument("--packages", nargs="*", default=[])
    package.add_argument("--script", default="")
    package.add_argument("--authorized", action="store_true")

    environment = sub.add_parser("environment", parents=[common], help="environment files")
    environment.add_argument("action", choices=["inspect", "validate", "load", "compare", "set", "unset"])
    environment.add_argument("--root")
    environment.add_argument("--file", default=".env")
    environment.add_argument("--name", default="")
    environment.add_argument("--value", default="")
    environment.add_argument("--left", default=".env")
    environment.add_argument("--right", default=".env.example")
    environment.add_argument("--authorized", action="store_true")

    port = sub.add_parser("port", parents=[common], help="ports")
    port.add_argument("action", choices=["list", "check", "find-free", "identify"])
    port.add_argument("port", nargs="?", type=int, default=0)
    port.add_argument("--preferred", type=int, default=0)

    providers = sub.add_parser("providers", parents=[common], help="provider readiness (installed + signed in)")
    providers.add_argument("--root")

    workspace = sub.add_parser("workspace", parents=[common], help="multi-application graph + ordered start/stop")
    workspace.add_argument("action", choices=["graph", "start", "stop"])
    workspace.add_argument("--root")
    workspace.add_argument("--no-install", action="store_true")
    workspace.add_argument("--build", action="store_true")

    container = sub.add_parser("container", parents=[common], help="Docker/Podman containers")
    container.add_argument("action", choices=["ps", "logs", "exec", "images", "inspect"])
    container.add_argument("container", nargs="?", default="")
    container.add_argument("command", nargs="*", default=[])
    container.add_argument("--tail", type=int, default=200)
    container.add_argument("--running", action="store_true")

    compose = sub.add_parser("compose", parents=[common], help="Docker Compose")
    compose.add_argument("action", choices=["up", "down", "ps", "logs"])
    compose.add_argument("--root")
    compose.add_argument("--service", default="")
    compose.add_argument("--tail", type=int, default=200)
    compose.add_argument("--build", action="store_true")
    compose.add_argument("--volumes", action="store_true")
    compose.add_argument("--authorized", action="store_true")

    k8s = sub.add_parser("k8s", parents=[common], help="Kubernetes / Helm / Kustomize")
    k8s.add_argument("action", choices=["get", "pods", "logs", "describe", "apply", "delete", "scale", "exec"])
    k8s.add_argument("--kind", default="")
    k8s.add_argument("--name", default="")
    k8s.add_argument("--namespace", default="")
    k8s.add_argument("--selector", default="")
    k8s.add_argument("--container", default="")
    k8s.add_argument("--file", default="")
    k8s.add_argument("--replicas", type=int, default=None)
    k8s.add_argument("command", nargs="*", default=[])
    k8s.add_argument("--authorized", action="store_true")

    terraform = sub.add_parser("terraform", parents=[common], help="Terraform")
    terraform.add_argument("action", choices=["init", "validate", "fmt", "plan", "apply", "destroy", "output", "state"])
    terraform.add_argument("--dir", default="")
    terraform.add_argument("--auto-approve", action="store_true")
    terraform.add_argument("--authorized", action="store_true")

    deploy = sub.add_parser("deploy", parents=[common], help="cloud deploy")
    deploy.add_argument("action", choices=["run", "status", "logs", "rollback"])
    deploy.add_argument("--root")
    deploy.add_argument("--provider", default="")
    deploy.add_argument("--environment", default="staging")
    deploy.add_argument("--service", default="")
    deploy.add_argument("--no-install", action="store_true")
    deploy.add_argument("--build", action="store_true")
    deploy.add_argument("--authorized", action="store_true")

    computer = sub.add_parser("computer", parents=[common], help="Computer Use desktop control")
    computer.add_argument("action", choices=["doctor", "status", "capabilities", "windows", "apps", "displays", "sessions", "replay"])
    computer.add_argument("--path", default="", help="trajectory JSON path for `replay`")

    plugin = sub.add_parser("plugin", parents=[common], help="plugin platform")
    plugin.add_argument("action", choices=[
        "list", "available", "search", "capabilities", "install", "uninstall", "enable",
        "disable", "update", "rollback", "repair", "inspect", "health", "doctor", "logs",
        "config", "validate", "create", "package", "dev",
        "auth", "auth-status", "auth-complete", "auth-refresh", "logout",
        "registry-search", "registry-install", "webhooks", "webhook-target",
    ])
    plugin.add_argument("target", nargs="?", default="", help="plugin id, path, or query")
    plugin.add_argument("source", nargs="?", default="", help="source path (update) or config JSON (config)")
    plugin.add_argument("--enable", action="store_true", help="enable after install")
    plugin.add_argument("--trust", default=None, help="trust level: trusted|verified|community|untrusted")
    plugin.add_argument("--id", default=None, help="plugin id (create)")
    plugin.add_argument("--code", default="", help="OAuth authorization code")
    plugin.add_argument("--state", default="", help="OAuth state")
    plugin.add_argument("--registry", default=None, help="registry URL or local path")
    plugin.add_argument("--version", default=None, help="registry version to install")
    plugin.add_argument("--url", default="", help="outbound webhook target URL")
    plugin.add_argument("--webhook", default="", help="webhook id")
    plugin.add_argument("--secret", default=None, help="webhook signing secret")

    kernel = sub.add_parser("kernel", parents=[common], help="plugin kernel composition")
    kernel.add_argument("action", choices=["dump", "diff", "doctor"])
    kernel.add_argument("--profile", default="", help="profile name under ~/.black-eclipse/profiles")
    kernel.add_argument("--profile-dir", default="", help="explicit profile directory")
    kernel.add_argument("--home", default="", help="Harness home (defaults to ~/.black-eclipse)")
    kernel.add_argument("--bundle-path", action="append", default=[], help="extra bundle search root")
    kernel.add_argument("--patch", action="append", default=[], help="ordered patch overlay")

    ui = sub.add_parser("ui", parents=[common], help="UI Engineering (catalog + blueprints)")
    ui.add_argument("action", choices=[
        "components", "capabilities", "blueprints", "show", "history",
        "create", "classify", "export", "import", "restore",
    ])
    ui.add_argument("target", nargs="?", default="", help="blueprint id, title, text, or file path")
    ui.add_argument("--path", default="", help="export output path")
    ui.add_argument("--revision", type=int, default=0, help="revision for restore")

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    # The `eval` group has its own argument surface and needs no application
    # manager, so it is dispatched before the shared parser (like `computer`).
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == "eval":
        from .evaluation.cli import run_cli
        return run_cli(raw[1:])
    # The `chat` group is the headless one-shot over the ONE runtime; it owns its
    # own arguments and needs no Application Runtime manager.
    if raw and raw[0] == "chat":
        from .chat_cli import run_cli as chat_cli
        return chat_cli(raw[1:])
    parser = build_parser()
    args = parser.parse_args(argv)
    # The `computer` group owns its own desktop runtime and needs no application
    # manager, so it never pays the Application Runtime boot cost.
    if getattr(args, "group", "") == "computer":
        return _cmd_computer(args, None)
    # The `plugin` group builds its own PluginManager over the runtime's store.
    if getattr(args, "group", "") == "plugin":
        return _cmd_plugin(args, None)
    # The `kernel` group reads profiles only; it never imports a plugin or
    # starts the runtime.
    if getattr(args, "group", "") == "kernel":
        from .kernel.cli import run_cli as kernel_cli
        argv = [args.action]
        if args.profile:
            argv += ["--profile", args.profile]
        if args.profile_dir:
            argv += ["--profile-dir", args.profile_dir]
        if args.home:
            argv += ["--home", args.home]
        for path in args.bundle_path:
            argv += ["--bundle-path", path]
        for path in args.patch:
            argv += ["--patch", path]
        if args.json:
            argv.append("--json")
        return kernel_cli(argv)
    # The `ui` group builds its own UI service over the runtime's store.
    if getattr(args, "group", "") == "ui":
        return _cmd_ui(args, None)
    try:
        manager = _build_manager(getattr(args, "root", None))
    except Exception as exc:  # noqa: BLE001 - a missing runtime is a clean CLI error
        return _refusal("RUNTIME_UNAVAILABLE", f"the application runtime could not start: {type(exc).__name__}")
    handlers = {
        "runtime": _cmd_runtime, "package": _cmd_package, "environment": _cmd_environment,
        "port": _cmd_port, "container": _cmd_container, "compose": _cmd_compose,
        "providers": _cmd_providers, "workspace": _cmd_workspace,
        "k8s": _cmd_k8s, "terraform": _cmd_terraform, "deploy": _cmd_deploy,
        "computer": _cmd_computer,
    }
    return handlers[args.group](args, manager)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
