"""Plugin Platform routes.

Every mutation is operator-gated by transport/token (`require_operator`) — never
a body boolean, matching the MCP and jobs conventions. Reads are operator-gated
too, because a plugin installation path and its configuration are part of the
runtime's private surface.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..deps import require_operator, service

router = APIRouter()


def _manager() -> Any:
    manager = getattr(service, "plugins", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="the plugin platform is unavailable")
    return manager


def _error(exc: Exception) -> HTTPException:
    code = getattr(exc, "code", "PLUGIN_ERROR")
    status = {
        "PLUGIN_NOT_FOUND": 404,
        "PLUGIN_SOURCE_NOT_FOUND": 404,
        "PLUGIN_MANIFEST_MISSING": 400,
        "MANIFEST_INVALID": 400,
        "PLUGIN_ALREADY_INSTALLED": 409,
        "PLUGIN_PERMISSION_DENIED": 403,
    }.get(code, 400)
    return HTTPException(status_code=status, detail={"code": code, "message": str(exc)})


@router.get("/plugins")
def list_plugins(_: bool = Depends(require_operator)) -> dict[str, Any]:
    manager = _manager()
    return {"success": True, "plugins": manager.list(), "summary": manager.summary()}


@router.get("/plugins/available")
def available_plugins(_: bool = Depends(require_operator)) -> dict[str, Any]:
    manager = _manager()
    return {"success": True, "plugins": manager.available()}


@router.get("/plugins/search")
def search_plugins(q: str = "", category: Optional[str] = None, installed: bool = False,
                   _: bool = Depends(require_operator)) -> dict[str, Any]:
    manager = _manager()
    return {"success": True, "results": manager.search(q, category=category, installed_only=installed)}


@router.get("/plugins/capabilities")
def plugin_capabilities(_: bool = Depends(require_operator)) -> dict[str, Any]:
    return {"success": True, "capabilities": _manager().capabilities()}


@router.get("/plugins/registry")
def registry_index(source: Optional[str] = None, _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        index = _manager().registry_index(source)
    except Exception as exc:  # noqa: BLE001
        raise _error(exc) from exc
    return {"success": True, "registry": index.get("name"), "plugins": index.get("plugins", [])}


@router.get("/plugins/registry/search")
def registry_search(q: str = "", category: Optional[str] = None, source: Optional[str] = None,
                    _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        results = _manager().registry_search(q, category=category, source=source)
    except Exception as exc:  # noqa: BLE001
        raise _error(exc) from exc
    return {"success": True, "results": results}


class RegistryInstallRequest(BaseModel):
    pluginId: str
    source: Optional[str] = None
    version: Optional[str] = None
    enable: bool = False


@router.post("/plugins/registry/install")
async def registry_install(request: RegistryInstallRequest, _: bool = Depends(require_operator)) -> JSONResponse:
    try:
        result = await _manager().install_from_registry(
            request.pluginId, source=request.source, version=request.version,
            enable=request.enable, operator=True,
        )
    except Exception as exc:  # noqa: BLE001
        raise _error(exc) from exc
    return JSONResponse({"success": True, **result})


@router.get("/plugins/{plugin_id}")
def inspect_plugin(plugin_id: str, _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return {"success": True, **(_manager().inspect(plugin_id))}
    except Exception as exc:  # noqa: BLE001
        raise _error(exc) from exc


@router.get("/plugins/{plugin_id}/ui")
def list_plugin_ui(plugin_id: str, _: bool = Depends(require_operator)) -> dict[str, Any]:
    return {"success": True, "panels": _manager().ui_panels(plugin_id)}


@router.get("/plugins/{plugin_id}/ui/{panel_id}")
async def render_plugin_ui(plugin_id: str, panel_id: str, _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return {"success": True, **await _manager().render_ui(plugin_id, panel_id)}
    except Exception as exc:  # noqa: BLE001
        raise _error(exc) from exc


@router.get("/plugins/{plugin_id}/health")
def plugin_health(plugin_id: str, _: bool = Depends(require_operator)) -> dict[str, Any]:
    return {"success": True, **(_manager().health_report(plugin_id))}


@router.get("/plugins/{plugin_id}/doctor")
def plugin_doctor(plugin_id: str, _: bool = Depends(require_operator)) -> dict[str, Any]:
    return {"success": True, **(_manager().doctor(plugin_id))}


@router.get("/plugins/{plugin_id}/logs")
def plugin_logs(plugin_id: str, limit: int = 200, _: bool = Depends(require_operator)) -> dict[str, Any]:
    return {"success": True, "logs": _manager().logs(plugin_id, limit)}


class InstallRequest(BaseModel):
    source: Optional[str] = None
    manifest: Optional[dict[str, Any]] = None
    files: Optional[dict[str, str]] = None
    trust: Optional[str] = None
    enable: bool = False


@router.post("/plugins/install")
async def install_plugin(request: InstallRequest, _: bool = Depends(require_operator)) -> JSONResponse:
    manager = _manager()
    source: Any
    if request.manifest is not None:
        source = {"manifest": request.manifest, "files": request.files or {}}
    elif request.source:
        source = request.source
    else:
        raise HTTPException(status_code=400, detail="either 'source' or 'manifest' is required")
    try:
        result = await manager.install(source, trust=request.trust, enable=request.enable, operator=True, replace=True)
    except Exception as exc:  # noqa: BLE001
        raise _error(exc) from exc
    return JSONResponse({"success": True, **result})


class LifecycleRequest(BaseModel):
    source: Optional[str] = None
    manifest: Optional[dict[str, Any]] = None
    files: Optional[dict[str, str]] = None


@router.post("/plugins/{plugin_id}/enable")
async def enable_plugin(plugin_id: str, _: bool = Depends(require_operator)) -> JSONResponse:
    try:
        return JSONResponse(await _manager().enable(plugin_id, operator=True))
    except Exception as exc:  # noqa: BLE001
        raise _error(exc) from exc


@router.post("/plugins/{plugin_id}/disable")
async def disable_plugin(plugin_id: str, _: bool = Depends(require_operator)) -> JSONResponse:
    try:
        return JSONResponse(await _manager().disable(plugin_id))
    except Exception as exc:  # noqa: BLE001
        raise _error(exc) from exc


@router.post("/plugins/{plugin_id}/reload")
async def reload_plugin(plugin_id: str, _: bool = Depends(require_operator)) -> JSONResponse:
    try:
        return JSONResponse(await _manager().reload(plugin_id, operator=True))
    except Exception as exc:  # noqa: BLE001
        raise _error(exc) from exc


@router.post("/plugins/{plugin_id}/update")
async def update_plugin(plugin_id: str, request: LifecycleRequest, _: bool = Depends(require_operator)) -> JSONResponse:
    manager = _manager()
    source: Any = {"manifest": request.manifest, "files": request.files or {}} if request.manifest else request.source
    if source is None:
        raise HTTPException(status_code=400, detail="either 'source' or 'manifest' is required")
    try:
        return JSONResponse(await manager.update(plugin_id, source, operator=True))
    except Exception as exc:  # noqa: BLE001
        raise _error(exc) from exc


@router.post("/plugins/{plugin_id}/rollback")
async def rollback_plugin(plugin_id: str, _: bool = Depends(require_operator)) -> JSONResponse:
    try:
        return JSONResponse(await _manager().rollback(plugin_id, operator=True))
    except Exception as exc:  # noqa: BLE001
        raise _error(exc) from exc


@router.post("/plugins/{plugin_id}/repair")
async def repair_plugin(plugin_id: str, _: bool = Depends(require_operator)) -> JSONResponse:
    try:
        return JSONResponse(await _manager().repair(plugin_id, operator=True))
    except Exception as exc:  # noqa: BLE001
        raise _error(exc) from exc


@router.delete("/plugins/{plugin_id}")
async def uninstall_plugin(plugin_id: str, _: bool = Depends(require_operator)) -> JSONResponse:
    try:
        return JSONResponse(await _manager().uninstall(plugin_id))
    except Exception as exc:  # noqa: BLE001
        raise _error(exc) from exc


@router.get("/plugins/{plugin_id}/config")
def get_plugin_config(plugin_id: str, _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return {"success": True, **(_manager().config_view(plugin_id))}
    except Exception as exc:  # noqa: BLE001
        raise _error(exc) from exc


@router.put("/plugins/{plugin_id}/config")
def set_plugin_config(plugin_id: str, payload: dict[str, Any], _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _manager().set_config(plugin_id, payload.get("values") or payload)
    except Exception as exc:  # noqa: BLE001
        raise _error(exc) from exc


@router.post("/plugins/{plugin_id}/secrets")
def set_plugin_secret(plugin_id: str, payload: dict[str, Any], _: bool = Depends(require_operator)) -> dict[str, Any]:
    name = str(payload.get("name") or "")
    if not name:
        raise HTTPException(status_code=400, detail="secret name is required")
    try:
        return _manager().set_secret(plugin_id, name, str(payload.get("value") or ""))
    except Exception as exc:  # noqa: BLE001
        raise _error(exc) from exc


@router.delete("/plugins/{plugin_id}/secrets/{name}")
def delete_plugin_secret(plugin_id: str, name: str, _: bool = Depends(require_operator)) -> dict[str, Any]:
    return _manager().delete_secret(plugin_id, name)


class CommandRequest(BaseModel):
    command: str
    arguments: dict[str, Any] = {}


@router.post("/plugins/{plugin_id}/command")
async def run_plugin_command(plugin_id: str, request: CommandRequest, _: bool = Depends(require_operator)) -> JSONResponse:
    try:
        return JSONResponse(await _manager().run_command(plugin_id, request.command, request.arguments))
    except Exception as exc:  # noqa: BLE001
        raise _error(exc) from exc


@router.post("/plugins/{plugin_id}/webhook/{webhook_id}")
async def plugin_webhook(plugin_id: str, webhook_id: str, request: Request) -> JSONResponse:
    """Inbound webhook: authenticated by HMAC against the plugin's stored secret,
    not by the operator token (a webhook caller is an external service)."""
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    try:
        result = await _manager().handle_webhook(plugin_id, webhook_id, payload, dict(request.headers))
    except Exception as exc:  # noqa: BLE001
        raise _error(exc) from exc
    return JSONResponse({"success": True, **result})


# ── OAuth ────────────────────────────────────────────────────────────────────


@router.get("/plugins/{plugin_id}/auth")
def plugin_auth_status(plugin_id: str, _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return {"success": True, **(_manager().auth_status(plugin_id))}
    except Exception as exc:  # noqa: BLE001
        raise _error(exc) from exc


class OAuthStartRequest(BaseModel):
    clientId: Optional[str] = None  # noqa: N815
    scope: str = ""


class OAuthCompleteRequest(BaseModel):
    code: str
    state: str = ""
    clientId: Optional[str] = None  # noqa: N815


@router.post("/plugins/{plugin_id}/oauth/start")
async def oauth_start(plugin_id: str, request: OAuthStartRequest, _: bool = Depends(require_operator)) -> JSONResponse:
    try:
        result = await _manager().oauth_start(plugin_id, client_id=request.clientId, scope=request.scope)
    except Exception as exc:  # noqa: BLE001
        raise _error(exc) from exc
    return JSONResponse({"success": True, **result})


@router.post("/plugins/{plugin_id}/oauth/poll")
async def oauth_poll(plugin_id: str, payload: Optional[dict[str, Any]] = None,
                     _: bool = Depends(require_operator)) -> JSONResponse:
    payload = payload or {}
    try:
        result = await _manager().oauth_poll(plugin_id, client_id=payload.get("clientId"))
    except Exception as exc:  # noqa: BLE001
        raise _error(exc) from exc
    return JSONResponse({"success": True, **result})


@router.post("/plugins/{plugin_id}/oauth/complete")
async def oauth_complete(plugin_id: str, request: OAuthCompleteRequest,
                         _: bool = Depends(require_operator)) -> JSONResponse:
    try:
        result = await _manager().oauth_complete(plugin_id, request.code, request.state, client_id=request.clientId)
    except Exception as exc:  # noqa: BLE001
        raise _error(exc) from exc
    return JSONResponse({"success": True, **result})


@router.post("/plugins/{plugin_id}/oauth/refresh")
async def oauth_refresh(plugin_id: str, _: bool = Depends(require_operator)) -> JSONResponse:
    try:
        result = await _manager().oauth_refresh(plugin_id)
    except Exception as exc:  # noqa: BLE001
        raise _error(exc) from exc
    return JSONResponse({"success": True, **result})


@router.delete("/plugins/{plugin_id}/oauth")
async def oauth_revoke(plugin_id: str, _: bool = Depends(require_operator)) -> JSONResponse:
    try:
        result = await _manager().oauth_revoke(plugin_id)
    except Exception as exc:  # noqa: BLE001
        raise _error(exc) from exc
    return JSONResponse({"success": True, **result})


# ── outbound webhooks ────────────────────────────────────────────────────────


class WebhookTargetRequest(BaseModel):
    webhook: str
    url: str
    signingSecret: Optional[str] = None  # noqa: N815


@router.post("/plugins/{plugin_id}/webhooks/target")
def set_webhook_target(plugin_id: str, request: WebhookTargetRequest,
                       _: bool = Depends(require_operator)) -> dict[str, Any]:
    try:
        return _manager().set_webhook_target(
            plugin_id, request.webhook, request.url, signing_secret=request.signingSecret,
        )
    except Exception as exc:  # noqa: BLE001
        raise _error(exc) from exc


@router.get("/plugins/{plugin_id}/webhooks")
def list_webhooks(plugin_id: str, _: bool = Depends(require_operator)) -> dict[str, Any]:
    return {"success": True, "webhooks": _manager().webhook_targets(plugin_id)}


@router.get("/plugins/{plugin_id}/webhooks/deliveries")
def list_webhook_deliveries(plugin_id: str, limit: int = 100,
                            _: bool = Depends(require_operator)) -> dict[str, Any]:
    return {"success": True, "deliveries": _manager().list_webhook_deliveries(plugin_id, limit)}
