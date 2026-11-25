"""Artifact Center route group.

Registered by `main.py` via `app.include_router(...)`. Shared state comes from
`deps.py`; this module never imports `main`.

EVERY route carries `require_operator` — reads AND mutations — exactly as the
tasks/jobs/MCP route groups are hardened. A directly reachable runtime must not
expose the artifact catalog to an unauthenticated caller.

Routes are a thin shell over `ArtifactService`; they never mutate state directly,
so the lifecycle contract applies to HTTP callers exactly as to the agent tools.
"""

from __future__ import annotations

import mimetypes
from typing import Any, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from ..artifacts.model import (
    AddVersionInput,
    ArtifactCategory,
    ArtifactSourceType,
    ArtifactStatus,
    BulkActionInput,
    CollectionInput,
    ListArtifactsQuery,
    RegisterArtifactInput,
    UpdateArtifactInput,
)
from ..artifacts.service import ArtifactError, ArtifactService
from ..deps import require_operator, service

router = APIRouter(tags=["artifacts"])


def _service() -> ArtifactService:
    artifact_service = getattr(service, "artifacts", None)
    if artifact_service is None:
        from ..artifacts.bootstrap import install_artifact_layer  # noqa: PLC0415

        install_artifact_layer(service)
        artifact_service = getattr(service, "artifacts", None)
    if artifact_service is None:
        raise HTTPException(status_code=503, detail="Artifact runtime unavailable")
    return artifact_service


def _owner(request: Request) -> str:
    """The artifact owner.

    A VERIFIED identity (the front door's signed `X-Harness-Identity` JWT) wins;
    only when no identity is present (single-user local mode) does this fall back
    to the local owner. A caller-supplied `x-harness-user` is never authoritative
    on its own: it used to select an arbitrary owner and therefore another user's
    artifacts.
    """
    from ..security.identity import resolve_identity

    identity = resolve_identity(request)
    if identity.user_id:
        return identity.user_id
    return "local"


def _raise(exc: Exception) -> None:
    if isinstance(exc, ArtifactError):
        code = 404 if exc.code.endswith("NOT_FOUND") else 400
        if exc.code in ("ARTIFACT_SOURCE_DENIED", "ARTIFACT_PATH_DENIED"):
            code = 403
        raise HTTPException(status_code=code, detail={"code": exc.code, "message": exc.message})
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc))
    raise HTTPException(status_code=500, detail=str(exc))


# ── Request bodies ────────────────────────────────────────────────────────────


class RegisterBody(RegisterArtifactInput):
    pass


class UpdateBody(UpdateArtifactInput):
    pass


class VersionBody(AddVersionInput):
    pass


class CollectionBody(CollectionInput):
    pass


class BulkBody(BulkActionInput):
    pass


class ReconcileBody(BaseModel):
    verifyChecksums: bool = False  # noqa: N815
    discover: bool = True
    purgeExpired: bool = False  # noqa: N815
    retentionDays: int = 30  # noqa: N815


# ── Literal routes (declared before `/{artifact_id}`) ────────────────────────


@router.get("/artifacts")
def list_artifacts(
    request: Request,
    q: Optional[str] = None,
    category: Optional[ArtifactCategory] = None,
    status: Optional[ArtifactStatus] = None,
    tags: Optional[list[str]] = Query(default=None),
    projectId: Optional[str] = None,  # noqa: N803
    agentId: Optional[str] = None,  # noqa: N803
    taskId: Optional[str] = None,  # noqa: N803
    runId: Optional[str] = None,  # noqa: N803
    conversationId: Optional[str] = None,  # noqa: N803
    collectionId: Optional[str] = None,  # noqa: N803
    sourceType: Optional[ArtifactSourceType] = None,  # noqa: N803
    mimeType: Optional[str] = None,  # noqa: N803
    extension: Optional[str] = None,
    favorite: Optional[bool] = None,
    includeArchived: bool = False,  # noqa: N803
    includeDeleted: bool = False,  # noqa: N803
    createdAfter: Optional[str] = None,  # noqa: N803
    createdBefore: Optional[str] = None,  # noqa: N803
    sort: str = "newest",
    limit: int = 50,
    offset: int = 0,
    _: bool = Depends(require_operator),
) -> dict[str, Any]:
    try:
        query = ListArtifactsQuery(
            q=q, category=category, status=status, tags=tags or [],
            project_id=projectId, agent_id=agentId, task_id=taskId, run_id=runId,
            conversation_id=conversationId, collection_id=collectionId,
            source_type=sourceType, mime_type=mimeType, extension=extension,
            favorite=favorite, include_archived=includeArchived,
            include_deleted=includeDeleted, created_after=createdAfter,
            created_before=createdBefore, sort=sort, limit=limit, offset=offset,
        )
        page = _service().list(query, owner_id=_owner(request))
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"success": True, **page.model_dump(by_alias=True)}


@router.post("/artifacts")
def register_artifact(
    request: Request, body: RegisterBody, _: bool = Depends(require_operator)
) -> dict[str, Any]:
    try:
        artifact = _service().register(body, owner_id=_owner(request))
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"success": True, "artifact": artifact.model_dump(by_alias=True)}


@router.get("/artifacts/stats")
def artifact_stats(
    request: Request, _: bool = Depends(require_operator)
) -> dict[str, Any]:
    try:
        stats = _service().stats(owner_id=_owner(request))
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"success": True, "stats": stats}


@router.get("/artifacts/tags")
def artifact_tags(
    request: Request, _: bool = Depends(require_operator)
) -> dict[str, Any]:
    try:
        tags = _service().tags(owner_id=_owner(request))
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"success": True, "tags": tags}


@router.get("/artifacts/collections")
def list_collections(
    request: Request, _: bool = Depends(require_operator)
) -> dict[str, Any]:
    try:
        collections = _service().list_collections(owner_id=_owner(request))
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"success": True, "collections": [c.model_dump(by_alias=True) for c in collections]}


@router.post("/artifacts/collections")
def create_collection(
    request: Request, body: CollectionBody, _: bool = Depends(require_operator)
) -> dict[str, Any]:
    try:
        collection = _service().create_collection(body, owner_id=_owner(request))
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"success": True, "collection": collection.model_dump(by_alias=True)}


@router.delete("/artifacts/collections/{collection_id}")
def delete_collection(
    collection_id: str, request: Request, _: bool = Depends(require_operator)
) -> dict[str, Any]:
    try:
        removed = _service().delete_collection(collection_id, owner_id=_owner(request))
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"success": True, "removed": removed}


@router.post("/artifacts/bulk")
def bulk_action(
    request: Request, body: BulkBody, _: bool = Depends(require_operator)
) -> dict[str, Any]:
    try:
        result = _service().bulk(body, owner_id=_owner(request))
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"success": True, **result}


@router.post("/artifacts/reconcile")
def reconcile(
    body: ReconcileBody, request: Request, _: bool = Depends(require_operator)
) -> dict[str, Any]:
    try:
        result = _service().reconcile(
            verify_checksums=body.verifyChecksums, owner_id=_owner(request),
            discover=body.discover, purge_expired=body.purgeExpired,
            retention_days=body.retentionDays,
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"success": True, "result": result}


# ── One artifact ──────────────────────────────────────────────────────────────


@router.get("/artifacts/{artifact_id}")
def get_artifact(
    artifact_id: str, request: Request, _: bool = Depends(require_operator)
) -> dict[str, Any]:
    try:
        artifact = _service().get(artifact_id, owner_id=_owner(request), touch=True)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"success": True, "artifact": artifact.model_dump(by_alias=True)}


@router.patch("/artifacts/{artifact_id}")
def update_artifact(
    artifact_id: str, request: Request, body: UpdateBody, _: bool = Depends(require_operator)
) -> dict[str, Any]:
    try:
        artifact = _service().update(artifact_id, body, owner_id=_owner(request))
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"success": True, "artifact": artifact.model_dump(by_alias=True)}


@router.delete("/artifacts/{artifact_id}")
def delete_artifact(
    artifact_id: str, request: Request, _: bool = Depends(require_operator)
) -> dict[str, Any]:
    try:
        artifact = _service().delete(artifact_id, owner_id=_owner(request))
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"success": True, "artifact": artifact.model_dump(by_alias=True)}


@router.post("/artifacts/{artifact_id}/archive")
def archive_artifact(
    artifact_id: str, request: Request, _: bool = Depends(require_operator)
) -> dict[str, Any]:
    try:
        artifact = _service().archive(artifact_id, owner_id=_owner(request))
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"success": True, "artifact": artifact.model_dump(by_alias=True)}


@router.post("/artifacts/{artifact_id}/restore")
def restore_artifact(
    artifact_id: str, request: Request, _: bool = Depends(require_operator)
) -> dict[str, Any]:
    try:
        artifact = _service().restore(artifact_id, owner_id=_owner(request))
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"success": True, "artifact": artifact.model_dump(by_alias=True)}


@router.post("/artifacts/{artifact_id}/purge")
def purge_artifact(
    artifact_id: str, request: Request, _: bool = Depends(require_operator)
) -> dict[str, Any]:
    try:
        purged = _service().purge(artifact_id, owner_id=_owner(request))
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"success": True, "purged": purged}


@router.get("/artifacts/{artifact_id}/versions")
def list_versions(
    artifact_id: str, request: Request, _: bool = Depends(require_operator)
) -> dict[str, Any]:
    try:
        versions = _service().list_versions(artifact_id, owner_id=_owner(request))
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"success": True, "versions": [v.model_dump(by_alias=True) for v in versions]}


@router.post("/artifacts/{artifact_id}/versions")
def add_version(
    artifact_id: str, request: Request, body: VersionBody, _: bool = Depends(require_operator)
) -> dict[str, Any]:
    try:
        artifact = _service().add_version(artifact_id, body, owner_id=_owner(request))
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"success": True, "artifact": artifact.model_dump(by_alias=True)}


@router.get("/artifacts/{artifact_id}/events")
def artifact_events(
    artifact_id: str, request: Request, _: bool = Depends(require_operator)
) -> dict[str, Any]:
    try:
        events = _service().events(artifact_id, owner_id=_owner(request))
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"success": True, "events": [e.model_dump(by_alias=True) for e in events]}


@router.get("/artifacts/{artifact_id}/preview")
def artifact_preview(
    artifact_id: str, request: Request, _: bool = Depends(require_operator)
) -> dict[str, Any]:
    try:
        preview = _service().preview(artifact_id, owner_id=_owner(request))
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"success": True, "preview": preview}


def _content_disposition(name: str, disposition: str = "attachment") -> str:
    safe = name.replace('"', "").replace("\r", "").replace("\n", "")
    fallback = "".join(ch if ch.isascii() and ch.isalnum() or ch in "._- " else "_" for ch in safe)
    return (
        f'{disposition}; filename="{fallback or "artifact"}"; '
        f"filename*=UTF-8''{quote(safe)}"
    )


def _parse_byte_range(header: str, size: int) -> Optional[tuple[int, int]]:
    """One `bytes=a-b` / `bytes=a-` / `bytes=-n` range (inclusive), or None.

    Multi-range requests are refused (None) rather than half-served: this is a
    single-object stream, and a client can retry plainly.
    """
    value = (header or "").strip()
    if not value.lower().startswith("bytes=") or "," in value:
        return None
    spec = value[len("bytes="):].strip()
    start_text, _, end_text = spec.partition("-")
    try:
        if start_text:
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
        elif end_text:
            length = int(end_text)
            start = max(0, size - length)
            end = size - 1
        else:
            return None
    except ValueError:
        return None
    if size <= 0 or start < 0 or start >= size or end < start:
        return None
    return start, min(end, size - 1)


def _read_exactly(handle: Any, length: int):
    """Yield at most `length` bytes from a binary handle, then close it."""
    remaining = length
    try:
        while remaining > 0:
            chunk = handle.read(min(64 * 1024, remaining))
            if not chunk:
                return
            remaining -= len(chunk)
            yield chunk
    finally:
        try:
            handle.close()
        except Exception:  # noqa: BLE001 - closing a stream must never raise
            pass


@router.get("/artifacts/{artifact_id}/download")
def download_artifact(
    artifact_id: str, request: Request, version: Optional[int] = None,
    inline: bool = False,
    _: bool = Depends(require_operator),
) -> StreamingResponse:
    """Stream the original bytes.

    `inline=1` serves `Content-Disposition: inline` so a `<video>`/`<audio>`
    element can play it; `Range` requests are honored (`206` + `Content-Range`)
    so players can SEEK — without it, scrubbing a long render is impossible.
    The bytes are identical and authorization is unchanged either way.
    """
    try:
        handle, artifact = _service().open_content(
            artifact_id, owner_id=_owner(request), version=version
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    media_type = artifact.mime_type or mimetypes.guess_type(artifact.name)[0] or "application/octet-stream"
    headers = {
        "Content-Disposition": _content_disposition(
            artifact.original_name or artifact.name,
            "inline" if inline else "attachment",
        ),
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
        "Accept-Ranges": "bytes",
    }
    if artifact.checksum:
        headers["ETag"] = f'"{artifact.checksum[:32]}"'

    size = artifact.size_bytes
    if size is None:
        try:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(0)
        except Exception:  # noqa: BLE001 - an unseekable handle is servable as-is
            size = None
    range_header = request.headers.get("range") or ""
    # `If-Range`: a client resuming a partial download only wants bytes if the
    # object is unchanged. A mismatch means "send the whole thing", never a
    # 206 fragment from a different version.
    if_range = (request.headers.get("if-range") or "").strip()
    etag = headers.get("ETag")
    if range_header and if_range and etag and if_range != etag:
        range_header = ""
    if size and range_header:
        parsed = _parse_byte_range(range_header, int(size))
        if parsed is None:
            try:
                handle.close()
            except Exception:  # noqa: BLE001
                pass
            return Response(
                status_code=416,
                headers={"Content-Range": f"bytes */{int(size)}", "Accept-Ranges": "bytes"},
            )
        start, end = parsed
        try:
            handle.seek(start)
        except Exception:  # noqa: BLE001 - fall back to the full object
            pass
        else:
            headers["Content-Range"] = f"bytes {start}-{end}/{int(size)}"
            headers["Content-Length"] = str(end - start + 1)
            return StreamingResponse(
                _read_exactly(handle, end - start + 1),
                status_code=206,
                media_type=media_type,
                headers=headers,
            )
    # A player needs a known length even for a plain 200: without it Chromium
    # treats the media as unseekable and can refuse to start at all.
    if size is not None:
        headers["Content-Length"] = str(int(size))
    return StreamingResponse(handle, media_type=media_type, headers=headers)


@router.get("/artifacts/{artifact_id}/thumbnail")
def artifact_thumbnail(
    artifact_id: str, request: Request, _: bool = Depends(require_operator)
):
    try:
        found = _service().open_thumbnail(artifact_id, owner_id=_owner(request))
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    if found is None:
        raise HTTPException(status_code=404, detail={"code": "ARTIFACT_THUMBNAIL_MISSING"})
    handle, _artifact = found
    return StreamingResponse(
        handle, media_type="image/png", headers={"Cache-Control": "private, max-age=300"}
    )
