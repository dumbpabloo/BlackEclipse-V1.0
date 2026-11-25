"""Presentation routes -- deck themes and export.

Deck CRUD lives on `/canvas/artifacts*` (a deck IS a canvas artifact). This
module adds only what is specific to decks: the theme tokens the browser
renderer uses, and the export that turns a deck into a real `.pptx`.
"""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from ..canvas.model import CanvasConflict, CanvasError
from ..deps import require_operator, service
from ..presentations.model import DeckError
from ..presentations.service import PresentationService
from ..presentations.themes import all_themes

router = APIRouter(tags=["presentations"])


class ExportBody(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    format: Literal["pptx", "pdf", "png"] = "pptx"
    save_to: Optional[str] = Field(default=None, max_length=1_000)
    filename: Optional[str] = Field(default=None, max_length=200)


def _artifacts():
    artifact_service = getattr(service, "artifacts", None)
    if artifact_service is None:
        from ..artifacts.bootstrap import install_artifact_layer  # noqa: PLC0415

        install_artifact_layer(service)
        artifact_service = getattr(service, "artifacts", None)
    if artifact_service is None:
        raise HTTPException(status_code=503, detail="Artifact runtime unavailable")
    return artifact_service


def _service() -> PresentationService:
    return PresentationService(_artifacts())


def _owner(request: Request) -> str:
    return (request.headers.get("x-harness-user") or "local").strip() or "local"


def _raise(exc: Exception) -> None:
    if isinstance(exc, CanvasConflict):
        raise HTTPException(
            status_code=409,
            detail={
                "code": exc.code,
                "message": exc.message,
                "currentVersion": exc.current_version,
            },
        )
    if isinstance(exc, DeckError):
        status = 503 if exc.code == "PRESENTATION_LIBRARY_MISSING" else 400
        raise HTTPException(status_code=status, detail={"code": exc.code, "message": exc.message})
    if isinstance(exc, CanvasError):
        raise HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message})
    raise HTTPException(status_code=500, detail=str(exc))


@router.get("/presentations/themes")
def list_presentation_themes(_: bool = Depends(require_operator)):
    return {"themes": all_themes()}


@router.post("/presentations/{artifact_id}/export")
def export_presentation(
    artifact_id: str,
    body: ExportBody,
    request: Request,
    _: bool = Depends(require_operator),
) -> dict:
    try:
        result = _service().export(
            artifact_id,
            owner_id=_owner(request),
            fmt=body.format,
            save_to=body.save_to,
            filename=body.filename,
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    # The render model is useful to the browser (preview parity) but heavy; the
    # export result keeps only what the caller needs.
    result.pop("renderModel", None)
    return result


__all__ = ["router"]
