"""Presentation lifecycle: decks are canvas artifacts, exports are derivatives.

There is no deck store. A deck is an ordinary `Artifact` whose bytes are the
canonical `DeckSpec` JSON and whose versions are the Artifact Center's immutable
versions. This service composes `CanvasService` for create/save/restore, adds the
deck ops, and renders `.pptx` through the ONE exporter. Export always registers a
NEW artifact; the source deck is never modified.
"""

from __future__ import annotations

import io
from typing import Any, Optional

from ..artifacts.model import ArtifactCategory, ArtifactSourceType
from ..artifacts.service import ArtifactError, ArtifactService
from ..canvas.model import CanvasKind, CreateCanvasInput, SaveCanvasInput
from ..canvas.service import CanvasService
from ..documents.authoring import slug_filename
from ..documents.outputs import publish_copy
from .export_pdf import export_pdf, export_png_zip
from .export_pptx import export_pptx
from .model import (
    MAX_DECK_BYTES,
    DeckError,
    DeckSpec,
    dump_deck,
    parse_deck,
)
from .ops import DeckOp, apply_deck_ops
from .render import outline as deck_outline
from .render import render_deck

PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
PDF_MIME = "application/pdf"
ZIP_MIME = "application/zip"


class PresentationService:
    def __init__(self, artifacts: ArtifactService) -> None:
        self._artifacts = artifacts
        self._canvas = CanvasService(artifacts)

    # ── reads ────────────────────────────────────────────────────────────────

    def snapshot(self, artifact_id: str, *, owner_id: Optional[str] = None) -> Any:
        return self._canvas.snapshot(artifact_id, owner_id=owner_id)

    def read_deck(self, artifact_id: str, *, owner_id: Optional[str] = None) -> DeckSpec:
        snap = self._canvas.snapshot(artifact_id, owner_id=owner_id)
        return parse_deck(snap.content)

    def read_bytes(self, artifact_id: str, *, owner_id: Optional[str] = None) -> bytes:
        """Read an artifact's bytes (the import path). Bounded to 64 MB."""
        handle, _artifact = self._artifacts.open_content(artifact_id, owner_id=owner_id)
        try:
            return handle.read(64 * 1024 * 1024)
        finally:
            try:
                handle.close()
            except Exception:  # noqa: BLE001
                pass

    def outline(self, artifact_id: str, *, owner_id: Optional[str] = None) -> dict[str, Any]:
        snap = self._canvas.snapshot(artifact_id, owner_id=owner_id)
        deck = parse_deck(snap.content)
        return {
            "artifact": snap.model_dump(by_alias=True),
            "meta": deck.meta.model_dump(by_alias=True),
            "size": deck.size.value,
            "slides": deck_outline(deck),
        }

    # ── writes ───────────────────────────────────────────────────────────────

    def create(
        self,
        deck: DeckSpec,
        *,
        owner_id: str = "local",
        name: Optional[str] = None,
        description: str = "",
        conversation_id: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> Any:
        content = dump_deck(deck)
        if len(content.encode("utf-8")) > MAX_DECK_BYTES:
            raise DeckError("DECK_LIMIT", "the deck exceeds the 4 MB deck limit")
        snap = self._canvas.create(
            CreateCanvasInput(
                name=(name or deck.meta.title).strip() or "Untitled deck",
                kind=CanvasKind.PRESENTATION,
                content=content,
                description=description or f"{len(deck.slides)}-slide presentation",
                conversation_id=conversation_id,
                tags=list(tags or []),
            ),
            owner_id=owner_id,
        )
        return snap

    def save(
        self,
        artifact_id: str,
        *,
        base_version: int,
        operations: list[DeckOp],
        owner_id: Optional[str] = None,
        actor: str = "presentation",
        change_note: str = "",
    ) -> Any:
        snap = self._canvas.snapshot(artifact_id, owner_id=owner_id)
        if snap.version != base_version:
            from ..canvas.model import CanvasConflict

            raise CanvasConflict("the deck changed since it was loaded", current_version=snap.version)
        content = apply_deck_ops(snap.content, operations)
        if len(content.encode("utf-8")) > MAX_DECK_BYTES:
            raise DeckError("DECK_LIMIT", "the deck exceeds the 4 MB deck limit")
        snap = self._canvas.save(
            artifact_id,
            SaveCanvasInput(
                base_version=base_version,
                content=content,
                change_note=change_note or "deck edit",
            ),
            owner_id=owner_id,
            actor=actor,
        )
        return snap

    # ── export ───────────────────────────────────────────────────────────────

    def _image_resolver(self, owner_id: Optional[str]):
        def resolve(artifact_id: str) -> Optional[bytes]:
            try:
                handle, _artifact = self._artifacts.open_content(artifact_id, owner_id=owner_id)
                try:
                    return handle.read(16 * 1024 * 1024)
                finally:
                    try:
                        handle.close()
                    except Exception:  # noqa: BLE001
                        pass
            except ArtifactError:
                return None
            except Exception:  # noqa: BLE001
                return None

        return resolve

    def export(
        self,
        artifact_id: str,
        *,
        owner_id: Optional[str] = None,
        fmt: str = "pptx",
        save_to: Optional[str] = None,
        price_data: Optional[dict[str, dict[str, Any]]] = None,
        filename: Optional[str] = None,
    ) -> dict[str, Any]:
        key = (fmt or "pptx").strip().casefold()
        if key not in ("pptx", "pdf", "png"):
            raise DeckError(
                "PRESENTATION_FORMAT_UNSUPPORTED",
                f"deck export format {fmt!r} is not supported; use 'pptx', 'pdf' or 'png'",
            )
        snap = self._canvas.snapshot(artifact_id, owner_id=owner_id)
        deck = parse_deck(snap.content)

        def price_resolver(source: dict[str, Any]) -> Optional[dict[str, Any]]:
            if not price_data:
                return None
            lookup = f"{source.get('symbol')}|{source.get('range')}|{source.get('interval')}"
            return price_data.get(lookup)

        model = render_deck(
            deck,
            price_resolver=price_resolver,
            image_resolver=self._image_resolver(owner_id),
        )
        if key == "pdf":
            data, summary = export_pdf(deck, model, image_resolver=self._image_resolver(owner_id))
            extension, mime, label, tag = "pdf", PDF_MIME, "PDF export", "pdf"
        elif key == "png":
            data, summary = export_png_zip(deck, model, image_resolver=self._image_resolver(owner_id))
            extension, mime, label, tag = "zip", ZIP_MIME, "PNG slides export", "png"
        else:
            data, summary = export_pptx(deck, model, image_resolver=self._image_resolver(owner_id))
            extension, mime, label, tag = "pptx", PPTX_MIME, "PowerPoint export", "pptx"

        stem = f"{deck.meta.title} deck" if key != "png" else f"{deck.meta.title} slides"
        name = filename or slug_filename(stem, extension)
        if not name.casefold().endswith(f".{extension}"):
            name = f"{name}.{extension}"
        artifact = self._artifacts.register_bytes(
            data,
            name=name,
            owner_id=owner_id or "local",
            description=f"{label} of {snap.name} (v{snap.version})",
            category=ArtifactCategory.DOCUMENT,
            mime_type=mime,
            source_type=ArtifactSourceType.AGENT,
            source_tool="presentation_export",
            parent_artifact_id=artifact_id,
            tags=["deck", tag, "export"],
            metadata={
                "derivedFrom": {"artifactId": artifact_id, "version": snap.version},
                "format": tag,
                "slides": summary["slides"],
                "overflowSlides": summary["overflowSlides"],
            },
            actor="presentation",
        )
        path, note = publish_copy(data, name, save_to)
        return {
            "artifact": artifact.model_dump(by_alias=True),
            "path": path,
            "note": note,
            "warnings": summary["warnings"],
            "overflowSlides": summary["overflowSlides"],
            "slides": summary["slides"],
            "renderModel": model,
        }


def read_export_bytes(result: dict[str, Any]) -> bytes:  # pragma: no cover - test helper
    return result.get("bytes") or io.BytesIO().getvalue()


__all__ = ["PPTX_MIME", "PresentationService", "read_export_bytes"]
