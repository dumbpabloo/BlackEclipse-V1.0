"""Canvas lifecycle over the ONE artifact service.

Thin, deterministic and safe: content is read from artifact bytes, saved as a
new immutable artifact version, and guarded by the revision the editor loaded.
No second database, no second storage root, no second version table.
"""

from __future__ import annotations

import base64
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from ..artifacts.model import (
    AddVersionInput,
    Artifact,
    ArtifactCategory,
    ArtifactSourceType,
    ListArtifactsQuery,
    RegisterArtifactInput,
)
from ..artifacts.service import ArtifactError, ArtifactService
from .model import (
    CANVAS_METADATA_KEY,
    CANVAS_SCHEMA_VERSION,
    MAX_CANVAS_BYTES,
    CanvasConflict,
    CanvasError,
    CanvasKind,
    CanvasListItem,
    CanvasNotFound,
    CanvasSnapshot,
    CanvasVersionSummary,
    CreateCanvasInput,
    EditOperation,
    RestoreCanvasInput,
    SaveCanvasInput,
)
from .ops import OpError, apply_operations

_MIME: dict[CanvasKind, str] = {
    CanvasKind.DOCUMENT: "text/markdown",
    CanvasKind.CODE: "text/plain",
    CanvasKind.VISUAL: "application/vnd.eclipse.canvas.visual+json",
    CanvasKind.PRESENTATION: "application/vnd.eclipse.deck+json",
}
_CATEGORY: dict[CanvasKind, ArtifactCategory] = {
    CanvasKind.DOCUMENT: ArtifactCategory.DOCUMENT,
    CanvasKind.CODE: ArtifactCategory.CODE,
    CanvasKind.VISUAL: ArtifactCategory.REPORT,
    CanvasKind.PRESENTATION: ArtifactCategory.DOCUMENT,
}
_EXTENSION: dict[str, str] = {
    "python": ".py",
    "typescript": ".ts",
    "javascript": ".js",
    "tsx": ".tsx",
    "jsx": ".jsx",
    "sql": ".sql",
    "json": ".json",
    "html": ".html",
    "css": ".css",
    "bash": ".sh",
    "shell": ".sh",
    "yaml": ".yml",
    "markdown": ".md",
    "go": ".go",
    "rust": ".rs",
    "java": ".java",
    "c": ".c",
    "cpp": ".cpp",
    "text": ".txt",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CanvasService:
    def __init__(self, artifacts: ArtifactService) -> None:
        self._artifacts = artifacts
        self._lock = threading.Lock()

    # ── metadata ─────────────────────────────────────────────────────────────

    @staticmethod
    def canvas_meta(artifact: Artifact) -> dict[str, Any]:
        raw = artifact.metadata.get(CANVAS_METADATA_KEY) if isinstance(artifact.metadata, dict) else None
        return raw if isinstance(raw, dict) else {}

    @classmethod
    def is_canvas(cls, artifact: Artifact) -> bool:
        raw = cls.canvas_meta(artifact)
        try:
            CanvasKind(str(raw.get("kind")))
        except ValueError:
            return False
        return True

    @classmethod
    def kind_of(cls, artifact: Artifact) -> CanvasKind:
        return CanvasKind(str(cls.canvas_meta(artifact)["kind"]))

    @classmethod
    def language_of(cls, artifact: Artifact) -> Optional[str]:
        value = cls.canvas_meta(artifact).get("language")
        return str(value) if value else None

    @staticmethod
    def _mime_for(kind: CanvasKind, language: Optional[str]) -> str:
        if kind is CanvasKind.CODE and language:
            return {
                "python": "text/x-python",
                "sql": "text/x-sql",
                "html": "text/html",
                "css": "text/css",
                "json": "application/json",
                "javascript": "text/javascript",
                "typescript": "text/typescript",
                "tsx": "text/typescript",
                "jsx": "text/javascript",
                "bash": "text/x-shellscript",
                "shell": "text/x-shellscript",
                "yaml": "text/yaml",
            }.get(language, "text/plain")
        return _MIME[kind]

    # ── reads ────────────────────────────────────────────────────────────────

    def _artifact(self, artifact_id: str, owner_id: Optional[str]) -> Artifact:
        try:
            artifact = self._artifacts.get(artifact_id, owner_id=owner_id)
        except ArtifactError as exc:
            if exc.code == "ARTIFACT_NOT_FOUND":
                raise CanvasNotFound("canvas artifact not found") from exc
            raise CanvasError(exc.message, code=exc.code) from exc
        if not self.is_canvas(artifact):
            raise CanvasNotFound("artifact is not a canvas artifact")
        return artifact

    def _read(self, artifact_id: str, owner_id: Optional[str], *, version: Optional[int] = None) -> tuple[Artifact, str]:
        artifact = self._artifact(artifact_id, owner_id)
        try:
            handle, _artifact = self._artifacts.open_content(artifact_id, owner_id=owner_id, version=version)
        except ArtifactError as exc:
            raise CanvasError(exc.message, code=exc.code) from exc
        try:
            data = handle.read(MAX_CANVAS_BYTES + 1)
        finally:
            try:
                handle.close()
            except Exception:  # noqa: BLE001 - handles are best-effort closable
                pass
        if len(data) > MAX_CANVAS_BYTES:
            raise CanvasError("canvas content exceeds the size limit", code="CANVAS_TOO_LARGE")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CanvasError("canvas content is not UTF-8 text", code="CANVAS_NOT_TEXT") from exc
        return artifact, text

    def snapshot(self, artifact_id: str, *, owner_id: Optional[str] = None) -> CanvasSnapshot:
        artifact, content = self._read(artifact_id, owner_id)
        versions = []
        for item in self._artifacts.list_versions(artifact_id, owner_id=owner_id):
            versions.append(
                CanvasVersionSummary(
                    version_number=item.version_number,
                    change_note=item.change_note,
                    size_bytes=item.size_bytes,
                    created_at=item.created_at,
                    created_by=item.created_by,
                )
            )
        versions.sort(key=lambda item: item.version_number, reverse=True)
        return self._snapshot(artifact, content, versions[:50])

    def _snapshot(
        self, artifact: Artifact, content: str, versions: list[CanvasVersionSummary]
    ) -> CanvasSnapshot:
        return CanvasSnapshot(
            artifact_id=artifact.id,
            name=artifact.name,
            description=artifact.description,
            kind=self.kind_of(artifact),
            language=self.language_of(artifact),
            mime_type=artifact.mime_type,
            content=content,
            version=artifact.version_number,
            size_bytes=artifact.size_bytes,
            updated_at=artifact.updated_at,
            conversation_id=artifact.conversation_id,
            versions=versions,
        )

    def list(
        self,
        *,
        owner_id: Optional[str] = None,
        kind: Optional[CanvasKind] = None,
        query: Optional[str] = None,
        limit: int = 100,
    ) -> list[CanvasListItem]:
        page = self._artifacts.list(
            ListArtifactsQuery(limit=min(max(limit, 1), 200)), owner_id=owner_id
        )
        needle = (query or "").strip().casefold()
        items: list[CanvasListItem] = []
        for artifact in page.artifacts:
            if not self.is_canvas(artifact):
                continue
            if kind is not None and self.kind_of(artifact) is not kind:
                continue
            if needle and needle not in artifact.name.casefold() and needle not in (artifact.description or "").casefold():
                continue
            items.append(
                CanvasListItem(
                    artifact_id=artifact.id,
                    name=artifact.name,
                    kind=self.kind_of(artifact),
                    language=self.language_of(artifact),
                    description=artifact.description,
                    version=artifact.version_number,
                    size_bytes=artifact.size_bytes,
                    updated_at=artifact.updated_at,
                    conversation_id=artifact.conversation_id,
                )
            )
        items.sort(key=lambda item: item.updated_at, reverse=True)
        return items[:limit]

    # ── writes ───────────────────────────────────────────────────────────────

    def create(self, input_model: CreateCanvasInput, *, owner_id: Optional[str] = None) -> CanvasSnapshot:
        content = input_model.content or ""
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_CANVAS_BYTES:
            raise CanvasError("canvas content exceeds the size limit", code="CANVAS_TOO_LARGE")
        language = input_model.language if input_model.kind is CanvasKind.CODE else None
        name = self._with_extension(input_model.name, input_model.kind, language)
        metadata = {
            **(input_model.metadata or {}),
            CANVAS_METADATA_KEY: {
                "kind": input_model.kind.value,
                "language": language,
                "schemaVersion": CANVAS_SCHEMA_VERSION,
                "createdAt": _now(),
            },
        }
        try:
            artifact = self._artifacts.register(
                RegisterArtifactInput(
                    content_base64=base64.b64encode(encoded).decode("ascii"),
                    name=name,
                    description=input_model.description,
                    category=_CATEGORY[input_model.kind],
                    mime_type=self._mime_for(input_model.kind, language),
                    tags=list(input_model.tags),
                    source_type=ArtifactSourceType.USER,
                    source_tool="eclipse_canvas",
                    conversation_id=input_model.conversation_id,
                    collection_id=input_model.collection_id,
                    metadata=metadata,
                ),
                owner_id=owner_id or "local",
                actor="canvas",
            )
        except ArtifactError as exc:
            raise CanvasError(exc.message, code=exc.code) from exc
        if input_model.kind is CanvasKind.PRESENTATION:
            self._refresh_deck_thumbnail(artifact.id, content, owner_id)
        return self.snapshot(artifact.id, owner_id=owner_id)

    def save(
        self,
        artifact_id: str,
        input_model: SaveCanvasInput,
        *,
        owner_id: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> CanvasSnapshot:
        with self._lock:
            artifact, current = self._read(artifact_id, owner_id)
            if artifact.version_number != input_model.base_version:
                raise CanvasConflict(
                    "the artifact changed since it was loaded",
                    current_version=artifact.version_number,
                )
            if input_model.operations:
                if self.kind_of(artifact) is CanvasKind.PRESENTATION:
                    # Deck ops are a different vocabulary; a text op here would
                    # corrupt the JSON. Lazy import avoids an import cycle.
                    from ..presentations.model import DeckError  # noqa: PLC0415
                    from ..presentations.ops import DeckOp, apply_deck_ops  # noqa: PLC0415

                    try:
                        deck_ops = [DeckOp.model_validate(item) for item in input_model.operations]
                        new_content = apply_deck_ops(current, deck_ops)
                    except DeckError as exc:
                        raise CanvasError(exc.message, code=exc.code) from exc
                    except Exception as exc:  # noqa: BLE001 - a bad op body is a 400
                        raise CanvasError(
                            f"invalid deck operation: {exc}", code="CANVAS_INVALID_OPERATION"
                        ) from exc
                else:
                    new_content = apply_operations(
                        current,
                        [EditOperation.model_validate(item) for item in input_model.operations],
                        kind=self.kind_of(artifact).value,
                    )
            elif input_model.content is not None:
                new_content = input_model.content
            else:
                raise CanvasError("a save requires content or operations")
            if len(new_content.encode("utf-8")) > MAX_CANVAS_BYTES:
                raise CanvasError("canvas content exceeds the size limit", code="CANVAS_TOO_LARGE")
            note = input_model.change_note or (
                "canvas edit" if input_model.operations else "canvas save"
            )
            try:
                updated = self._artifacts.add_version(
                    artifact_id,
                    AddVersionInput(
                        content_base64=base64.b64encode(new_content.encode("utf-8")).decode("ascii"),
                        change_note=note,
                        mime_type=artifact.mime_type,
                        idempotency_key=input_model.idempotency_key,
                    ),
                    owner_id=owner_id,
                    actor=actor or "canvas",
                )
            except ArtifactError as exc:
                raise CanvasError(exc.message, code=exc.code) from exc
        if self.kind_of(artifact) is CanvasKind.PRESENTATION:
            self._refresh_deck_thumbnail(updated.id, new_content, owner_id)
        return self.snapshot(updated.id, owner_id=owner_id)

    def restore(
        self,
        artifact_id: str,
        input_model: RestoreCanvasInput,
        *,
        owner_id: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> CanvasSnapshot:
        with self._lock:
            artifact = self._artifact(artifact_id, owner_id)
            if artifact.version_number != input_model.base_version:
                raise CanvasConflict(
                    "the artifact changed since it was loaded",
                    current_version=artifact.version_number,
                )
            try:
                _artifact, content = self._read(artifact_id, owner_id, version=input_model.version)
                updated = self._artifacts.add_version(
                    artifact_id,
                    AddVersionInput(
                        content_base64=base64.b64encode(content.encode("utf-8")).decode("ascii"),
                        change_note=input_model.change_note
                        or f"restored version {input_model.version}",
                        mime_type=artifact.mime_type,
                        idempotency_key=input_model.idempotency_key,
                    ),
                    owner_id=owner_id,
                    actor=actor or "canvas",
                )
            except ArtifactError as exc:
                if exc.code == "ARTIFACT_VERSION_NOT_FOUND":
                    raise CanvasNotFound("canvas version not found") from exc
                raise CanvasError(exc.message, code=exc.code) from exc
        return self.snapshot(updated.id, owner_id=owner_id)

    # ── helpers used by the AI orchestrator ──────────────────────────────────

    def _refresh_deck_thumbnail(self, artifact_id: str, content: str,
                                owner_id: Optional[str]) -> None:
        """Best-effort first-slide thumbnail for a deck artifact.

        Lazy imports keep the canvas package free of a presentation import at
        module load; a missing Matplotlib or a bad spec simply leaves the
        outline preview in place.
        """
        try:
            from ..presentations.export_pdf import render_slide_png  # noqa: PLC0415
            from ..presentations.model import parse_deck  # noqa: PLC0415
            from ..presentations.render import render_deck  # noqa: PLC0415

            deck = parse_deck(content)
            png = render_slide_png(render_deck(deck), 0)
            self._artifacts.set_preview_thumbnail(artifact_id, png, owner_id=owner_id or "local")
        except Exception:  # noqa: BLE001 - a thumbnail never blocks the deck
            return

    def read_for_edit(
        self, artifact_id: str, base_version: int, *, owner_id: Optional[str] = None
    ) -> tuple[Artifact, str]:
        artifact, content = self._read(artifact_id, owner_id)
        if artifact.version_number != base_version:
            raise CanvasConflict(
                "the artifact changed since it was loaded",
                current_version=artifact.version_number,
            )
        return artifact, content

    def preview_operations(
        self,
        artifact_id: str,
        base_version: int,
        operations: list[EditOperation],
        *,
        owner_id: Optional[str] = None,
    ) -> tuple[Artifact, str, str]:
        artifact, current = self.read_for_edit(artifact_id, base_version, owner_id=owner_id)
        try:
            result = apply_operations(current, operations, kind=self.kind_of(artifact).value)
        except OpError as exc:
            raise CanvasError(exc.message, code=exc.code) from exc
        return artifact, current, result

    @staticmethod
    def _with_extension(name: str, kind: CanvasKind, language: Optional[str]) -> str:
        cleaned = name.strip()
        if kind is CanvasKind.PRESENTATION:
            return cleaned if cleaned.casefold().endswith(".deck.json") else f"{cleaned}.deck.json"
        if "." in cleaned.rsplit("/", 1)[-1]:
            return cleaned
        if kind is CanvasKind.DOCUMENT:
            return f"{cleaned}.md"
        if kind is CanvasKind.VISUAL:
            return f"{cleaned}.json"
        return f"{cleaned}{_EXTENSION.get(language or '', '.txt')}"


__all__ = ["CanvasService"]
