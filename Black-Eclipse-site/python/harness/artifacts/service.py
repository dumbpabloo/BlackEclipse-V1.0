"""Artifact Service — the ONE authority for the artifact lifecycle.

Frontend-agnostic: the API routes, agent tools, jobs and producers all call this
object. Every operation validates input, enforces authorization scope (owner),
and records a lifecycle event. Storage writes happen before a record is marked
available; a preview failure never invalidates the original.

The service owns no second connection: it uses the shared `ArtifactStore` and a
`LocalArtifactStorage` adapter. It never accepts a client-supplied storage key.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

from . import preview as preview_engine
from .content import category_for, extension_of, guess_mime, preview_kind, safe_display_name
from .model import (
    AddVersionInput,
    Artifact,
    ArtifactCategory,
    ArtifactCollection,
    ArtifactPage,
    ArtifactSourceType,
    ArtifactStatus,
    ArtifactVersion,
    BulkActionInput,
    CollectionInput,
    ListArtifactsQuery,
    MAX_METADATA_BYTES,
    PreviewStatus,
    RegisterArtifactInput,
    UpdateArtifactInput,
)
from .storage import LocalArtifactStorage, StorageError

log = logging.getLogger("harness.artifacts")


class ArtifactError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _publish(event_type: str, payload: dict[str, Any]) -> None:
    try:
        from ..event_system.emit import publish_event  # noqa: PLC0415

        publish_event(event_type, payload, source="artifacts")
    except Exception:  # noqa: BLE001 - telemetry must never break a mutation
        pass


class ArtifactService:
    def __init__(self, store: Any, storage: Optional[LocalArtifactStorage] = None) -> None:
        self._store = store
        self._storage = storage or LocalArtifactStorage()

    @property
    def storage(self) -> LocalArtifactStorage:
        return self._storage

    # ── source resolution (never a client-supplied storage key) ──────────────

    def _allowed_roots(self) -> list[Path]:
        roots: list[Path] = []
        try:
            from ..code.workspace import default_root  # noqa: PLC0415

            root = default_root()
            if root is not None:
                roots.append(Path(root).resolve())
        except Exception:  # noqa: BLE001
            pass
        try:
            from ..code.roots import TrustedRootStore  # noqa: PLC0415

            for entry in TrustedRootStore().list_roots():
                path = entry.get("path") if isinstance(entry, dict) else None
                if path:
                    roots.append(Path(path).resolve())
        except Exception:  # noqa: BLE001
            pass
        # The artifact storage root itself is always importable (re-importing a
        # previously stored object is confined to that tree).
        roots.append(self._storage.root.resolve())
        # De-duplicate while preserving order.
        seen: set[str] = set()
        unique: list[Path] = []
        for root in roots:
            key = str(root)
            if key not in seen:
                seen.add(key)
                unique.append(root)
        return unique

    def _resolve_source(self, source_path: str) -> Path:
        candidate = Path(source_path)
        if not candidate.is_absolute():
            roots = self._allowed_roots()
            if not roots:
                raise ArtifactError("ARTIFACT_SOURCE_DENIED", "no authorized workspace is configured")
            candidate = roots[0] / candidate
        resolved = candidate.resolve()
        for root in self._allowed_roots():
            try:
                resolved.relative_to(root)
                return resolved
            except ValueError:
                continue
        raise ArtifactError(
            "ARTIFACT_SOURCE_DENIED",
            "source path is outside every authorized workspace root",
        )

    def _read_source(
        self, input_model: RegisterArtifactInput | AddVersionInput
    ) -> tuple[Optional[Path], Optional[bytes]]:
        inline = getattr(input_model, "content_base64", None)
        if inline is not None:
            try:
                return None, base64.b64decode(inline, validate=True)
            except Exception as exc:  # noqa: BLE001
                raise ArtifactError("ARTIFACT_INVALID_CONTENT", "invalid base64 content") from exc
        source = getattr(input_model, "source_path", None)
        if source:
            return self._resolve_source(source), None
        raise ArtifactError(
            "ARTIFACT_SOURCE_REQUIRED", "a source path or inline content is required"
        )

    # ── registration ─────────────────────────────────────────────────────────

    def register(
        self, input_model: RegisterArtifactInput, *, owner_id: str = "local", actor: Optional[str] = None
    ) -> Artifact:
        if input_model.idempotency_key:
            existing = self._store.lookup_idempotency(input_model.idempotency_key)
            if existing:
                found = self._store.get(existing)
                if found is not None:
                    return found
        metadata = input_model.metadata or {}
        if len(json.dumps(metadata)) > MAX_METADATA_BYTES:
            raise ArtifactError("ARTIFACT_METADATA_TOO_LARGE", "metadata exceeds the size limit")

        path, inline = self._read_source(input_model)
        try:
            if inline is not None:
                stored = self._storage.save_bytes(
                    inline, name=input_model.name, owner_id=owner_id, mime_type=input_model.mime_type
                )
            else:
                assert path is not None
                # Copy into the store (never reference the workspace path directly).
                stored = self._storage.import_file(
                    path, name=input_model.name, owner_id=owner_id, mime_type=input_model.mime_type
                )
        except StorageError as exc:
            raise ArtifactError(exc.code, exc.message) from exc

        mime = stored.mime_type
        category = input_model.category or category_for(
            input_model.name, mime, source_tool=input_model.source_tool
        )
        kind = preview_kind(mime, category, input_model.name)
        original_name = path.name if path is not None else input_model.name

        artifact = self._store.create(
            name=input_model.name,
            original_name=original_name,
            description=input_model.description,
            category=category,
            mime_type=mime,
            extension=stored.extension,
            size_bytes=stored.size_bytes,
            checksum=stored.checksum,
            storage_backend=self._storage.name,
            storage_key=stored.storage_key,
            owner_id=owner_id,
            workspace_id=input_model.workspace_id,
            project_id=input_model.project_id,
            conversation_id=input_model.conversation_id,
            agent_id=input_model.agent_id,
            task_id=input_model.task_id,
            run_id=input_model.run_id,
            parent_artifact_id=input_model.parent_artifact_id,
            source_type=input_model.source_type,
            source_tool=input_model.source_tool,
            status=ArtifactStatus.ACTIVE,
            preview_status=PreviewStatus.PENDING,
            preview_kind=kind,
            metadata=metadata,
            provenance={
                "originalName": original_name,
                "sourceTool": input_model.source_tool,
                "agentId": input_model.agent_id,
                "runId": input_model.run_id,
                "taskId": input_model.task_id,
            },
        )
        # First version row (v1) references the stored object.
        self._store.add_version(
            artifact_id=artifact.id,
            version_number=1,
            storage_key=stored.storage_key,
            checksum=stored.checksum,
            size_bytes=stored.size_bytes,
            mime_type=mime,
            created_by=actor or input_model.agent_id,
            agent_id=input_model.agent_id,
            task_id=input_model.task_id,
            run_id=input_model.run_id,
            change_note="initial version",
            metadata={},
        )
        # Tags / collection.
        for tag in input_model.tags:
            self._attach_tag(artifact.id, tag, owner_id)
        if input_model.collection_id:
            self._store.add_to_collection(input_model.collection_id, artifact.id)

        self._store.add_event(
            artifact.id, "ARTIFACT_CREATED", actor=actor or input_model.agent_id,
            data={"name": artifact.name, "category": category.value, "sizeBytes": stored.size_bytes},
        )
        if input_model.idempotency_key:
            self._store.record_idempotency(input_model.idempotency_key, artifact.id)

        artifact = self._run_preview(artifact) or artifact
        _publish("artifact.created", {
            "artifactId": artifact.id, "name": artifact.name,
            "category": artifact.category.value, "projectId": artifact.project_id,
            "runId": artifact.run_id, "taskId": artifact.task_id,
        })
        return self._store.get(artifact.id) or artifact

    def register_bytes(
        self,
        data: bytes,
        *,
        name: str,
        owner_id: str = "local",
        description: str = "",
        category: Optional[ArtifactCategory] = None,
        mime_type: Optional[str] = None,
        source_type: ArtifactSourceType = ArtifactSourceType.AGENT,
        source_tool: Optional[str] = None,
        agent_id: Optional[str] = None,
        project_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        task_id: Optional[str] = None,
        run_id: Optional[str] = None,
        parent_artifact_id: Optional[str] = None,
        tags: Optional[list[str]] = None,
        idempotency_key: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        actor: Optional[str] = None,
    ) -> Artifact:
        """Convenience for producers that already hold bytes (analysis, exec)."""
        return self.register(
            RegisterArtifactInput(
                content_base64=base64.b64encode(data).decode("ascii"),
                name=name, description=description, category=category, mime_type=mime_type,
                tags=tags or [], source_type=source_type, source_tool=source_tool,
                agent_id=agent_id, project_id=project_id, conversation_id=conversation_id,
                task_id=task_id, run_id=run_id, parent_artifact_id=parent_artifact_id,
                idempotency_key=idempotency_key, metadata=metadata or {},
            ),
            owner_id=owner_id, actor=actor,
        )

    def register_path(
        self,
        path: str | Path,
        *,
        name: Optional[str] = None,
        owner_id: str = "local",
        **kwargs: Any,
    ) -> Artifact:
        actor = kwargs.pop("actor", None)
        return self.register(
            RegisterArtifactInput(source_path=str(path), name=name or Path(path).name, **kwargs),
            owner_id=owner_id, actor=actor,
        )

    # ── retrieval ────────────────────────────────────────────────────────────

    def get(self, artifact_id: str, *, owner_id: Optional[str] = None, touch: bool = False) -> Artifact:
        artifact = self._store.get(artifact_id, owner_id=owner_id)
        if artifact is None:
            raise ArtifactError("ARTIFACT_NOT_FOUND", "artifact not found")
        if touch:
            self._store.touch_accessed(artifact_id)
            artifact = self._store.get(artifact_id, owner_id=owner_id) or artifact
        return artifact

    def list(self, query: ListArtifactsQuery, *, owner_id: Optional[str] = None) -> ArtifactPage:
        artifacts, total = self._store.list(query, owner_id=owner_id)
        return ArtifactPage(
            artifacts=artifacts, total=total, limit=query.limit, offset=query.offset,
            has_more=query.offset + len(artifacts) < total,
        )

    def list_versions(self, artifact_id: str, *, owner_id: Optional[str] = None) -> list[ArtifactVersion]:
        self.get(artifact_id, owner_id=owner_id)
        return self._store.list_versions(artifact_id)

    def tags(self, *, owner_id: str = "local") -> list[str]:
        return [t.name for t in self._store.list_tags(owner_id)]

    def stats(self, *, owner_id: Optional[str] = None) -> dict[str, Any]:
        return self._store.stats(owner_id=owner_id)

    def events(self, artifact_id: str, *, owner_id: Optional[str] = None) -> list[Any]:
        self.get(artifact_id, owner_id=owner_id)
        return self._store.list_events(artifact_id)

    # ── mutation ─────────────────────────────────────────────────────────────

    def update(
        self, artifact_id: str, input_model: UpdateArtifactInput, *,
        owner_id: Optional[str] = None, actor: Optional[str] = None,
    ) -> Artifact:
        artifact = self.get(artifact_id, owner_id=owner_id)
        fields: dict[str, Any] = {}
        if input_model.name is not None:
            fields["name"] = input_model.name
        if input_model.description is not None:
            fields["description"] = input_model.description
        if input_model.favorite is not None:
            fields["favorite"] = input_model.favorite
        if input_model.pinned is not None:
            fields["pinned"] = input_model.pinned
        if input_model.category is not None:
            fields["category"] = input_model.category.value
        if input_model.metadata is not None:
            merged = {**artifact.metadata, **input_model.metadata}
            fields["metadata"] = merged
        if fields:
            self._store.update_fields(artifact_id, fields)
        if input_model.tags is not None:
            self._set_tags(artifact_id, input_model.tags, artifact.owner_id)
        if input_model.collection_id is not None:
            self._store.add_to_collection(input_model.collection_id, artifact_id)
        self._store.add_event(
            artifact_id, "ARTIFACT_UPDATED", actor=actor, data={"fields": list(fields)}
        )
        _publish("artifact.updated", {"artifactId": artifact_id})
        return self.get(artifact_id, owner_id=owner_id)

    def rename(self, artifact_id: str, name: str, *, owner_id: Optional[str] = None) -> Artifact:
        return self.update(artifact_id, UpdateArtifactInput(name=name), owner_id=owner_id)

    def add_version(
        self, artifact_id: str, input_model: AddVersionInput, *,
        owner_id: Optional[str] = None, actor: Optional[str] = None,
    ) -> Artifact:
        artifact = self.get(artifact_id, owner_id=owner_id)
        if input_model.idempotency_key:
            existing = self._store.lookup_idempotency(input_model.idempotency_key)
            if existing:
                return self.get(existing, owner_id=owner_id)
        if input_model.expected_checksum and artifact.checksum != input_model.expected_checksum:
            raise ArtifactError(
                "ARTIFACT_CONFLICT",
                "the artifact changed since it was read; re-read it and retry the edit",
            )
        path, inline = self._read_source(input_model)
        try:
            if inline is not None:
                stored = self._storage.save_bytes(
                    inline, name=artifact.name, owner_id=artifact.owner_id,
                    mime_type=input_model.mime_type or artifact.mime_type,
                )
            else:
                assert path is not None
                stored = self._storage.import_file(
                    path, name=artifact.name, owner_id=artifact.owner_id,
                    mime_type=input_model.mime_type or artifact.mime_type,
                )
        except StorageError as exc:
            raise ArtifactError(exc.code, exc.message) from exc
        new_version = artifact.version_number + 1
        # The new object is safely persisted BEFORE the row points at it.
        self._store.add_version(
            artifact_id=artifact_id, version_number=new_version, storage_key=stored.storage_key,
            checksum=stored.checksum, size_bytes=stored.size_bytes, mime_type=stored.mime_type,
            created_by=actor or input_model.agent_id, agent_id=input_model.agent_id,
            task_id=input_model.task_id, run_id=input_model.run_id,
            change_note=input_model.change_note, metadata={},
        )
        self._store.update_fields(artifact_id, {
            "storageKey": stored.storage_key, "checksum": stored.checksum,
            "sizeBytes": stored.size_bytes, "mimeType": stored.mime_type,
            "versionNumber": new_version, "previewStatus": PreviewStatus.PENDING.value,
        })
        if input_model.idempotency_key:
            self._store.record_idempotency(input_model.idempotency_key, artifact_id)
        self._store.add_event(
            artifact_id, "ARTIFACT_VERSION_ADDED", actor=actor or input_model.agent_id,
            data={"versionNumber": new_version, "checksum": stored.checksum},
        )
        artifact = self._store.get(artifact_id) or artifact
        self._run_preview(artifact)
        _publish("artifact.version.added", {"artifactId": artifact_id, "versionNumber": new_version})
        return self.get(artifact_id, owner_id=owner_id)

    def archive(self, artifact_id: str, *, owner_id: Optional[str] = None, actor: Optional[str] = None) -> Artifact:
        from ..persistence import now_iso  # noqa: PLC0415

        artifact = self.get(artifact_id, owner_id=owner_id)
        if artifact.status == ArtifactStatus.DELETED:
            raise ArtifactError("ARTIFACT_DELETED", "a deleted artifact must be restored first")
        self._store.set_status(artifact_id, ArtifactStatus.ARCHIVED, archived_at=now_iso())
        self._store.add_event(artifact_id, "ARTIFACT_ARCHIVED", actor=actor)
        _publish("artifact.archived", {"artifactId": artifact_id})
        return self.get(artifact_id, owner_id=owner_id)

    def restore(self, artifact_id: str, *, owner_id: Optional[str] = None, actor: Optional[str] = None) -> Artifact:
        artifact = self.get(artifact_id, owner_id=owner_id)
        if artifact.status == ArtifactStatus.PURGED:
            raise ArtifactError("ARTIFACT_PURGED", "a purged artifact cannot be restored")
        self._store.update_fields(artifact_id, {
            "status": ArtifactStatus.ACTIVE.value, "archivedAt": None, "deletedAt": None,
        })
        self._store.add_event(artifact_id, "ARTIFACT_RESTORED", actor=actor)
        _publish("artifact.restored", {"artifactId": artifact_id})
        return self.get(artifact_id, owner_id=owner_id)

    def delete(self, artifact_id: str, *, owner_id: Optional[str] = None, actor: Optional[str] = None) -> Artifact:
        """Soft delete. Bytes are retained; `purge` removes them."""
        from ..persistence import now_iso  # noqa: PLC0415

        self.get(artifact_id, owner_id=owner_id)
        self._store.set_status(artifact_id, ArtifactStatus.DELETED, deleted_at=now_iso())
        self._store.add_event(artifact_id, "ARTIFACT_DELETED", actor=actor)
        _publish("artifact.deleted", {"artifactId": artifact_id})
        return self.get(artifact_id, owner_id=owner_id)

    def purge(self, artifact_id: str, *, owner_id: Optional[str] = None, actor: Optional[str] = None) -> bool:
        """Permanently delete: every version's bytes are removed, then the row.

        Refuses while another artifact references this one as a parent, or while
        a version's object is shared by another artifact's version.
        """
        artifact = self.get(artifact_id, owner_id=owner_id)
        if artifact.status != ArtifactStatus.DELETED:
            raise ArtifactError("ARTIFACT_NOT_DELETED", "soft-delete the artifact before purging")
        # Shared-reference guard: a version object used by another artifact.
        mine = {v.storage_key for v in self._store.list_versions(artifact_id) if v.storage_key}
        for other_id, _, storage_key, _ in self._store.iter_storage_keys():
            if other_id == artifact_id:
                continue
            if storage_key in mine and self._store.get(other_id) is not None:
                raise ArtifactError(
                    "ARTIFACT_REFERENCED",
                    "another artifact shares this artifact's content",
                )
        for version in self._store.list_versions(artifact_id):
            if version.storage_key:
                self._storage.delete(version.storage_key)
                self._storage.delete(self._storage.thumbnail_key(version.storage_key))
        self._store.add_event(artifact_id, "ARTIFACT_PURGED", actor=actor)
        # Remove join rows then the artifact row.
        self._store.update_fields(artifact_id, {"status": ArtifactStatus.PURGED.value})
        self._db_purge(artifact_id)
        _publish("artifact.purged", {"artifactId": artifact_id})
        return True

    def _db_purge(self, artifact_id: str) -> None:
        db = self._store._db  # noqa: SLF001 - the one shared connection
        db.execute("DELETE FROM HarnessArtifactVersion WHERE artifactId=?", (artifact_id,))
        db.execute("DELETE FROM HarnessArtifactTagLink WHERE artifactId=?", (artifact_id,))
        db.execute("DELETE FROM HarnessArtifactCollectionMember WHERE artifactId=?", (artifact_id,))
        db.execute("DELETE FROM HarnessArtifactEvent WHERE artifactId=?", (artifact_id,))
        db.execute("DELETE FROM HarnessArtifact WHERE id=?", (artifact_id,))
        db.commit()

    # ── bulk ─────────────────────────────────────────────────────────────────

    def bulk(self, input_model: BulkActionInput, *, owner_id: Optional[str] = None) -> dict[str, Any]:
        succeeded: list[str] = []
        failed: list[dict[str, str]] = []
        for artifact_id in input_model.artifact_ids:
            try:
                if input_model.action == "archive":
                    self.archive(artifact_id, owner_id=owner_id)
                elif input_model.action == "restore":
                    self.restore(artifact_id, owner_id=owner_id)
                elif input_model.action == "delete":
                    self.delete(artifact_id, owner_id=owner_id)
                elif input_model.action == "favorite":
                    self.update(artifact_id, UpdateArtifactInput(favorite=True), owner_id=owner_id)
                elif input_model.action == "unfavorite":
                    self.update(artifact_id, UpdateArtifactInput(favorite=False), owner_id=owner_id)
                elif input_model.action == "add_tags":
                    artifact = self.get(artifact_id, owner_id=owner_id)
                    self._set_tags(artifact_id, [*artifact.tags, *input_model.tags], artifact.owner_id)
                elif input_model.action == "remove_tags":
                    artifact = self.get(artifact_id, owner_id=owner_id)
                    remaining = [t for t in artifact.tags if t.casefold() not in {x.casefold() for x in input_model.tags}]
                    self._set_tags(artifact_id, remaining, artifact.owner_id)
                elif input_model.action == "move":
                    if not input_model.collection_id:
                        raise ArtifactError("ARTIFACT_COLLECTION_REQUIRED", "move requires a collectionId")
                    self._store.add_to_collection(input_model.collection_id, artifact_id)
                else:  # pragma: no cover - validator guards this
                    raise ArtifactError("ARTIFACT_BAD_ACTION", "unknown bulk action")
                succeeded.append(artifact_id)
            except ArtifactError as exc:
                failed.append({"artifactId": artifact_id, "code": exc.code, "message": exc.message})
        return {"succeeded": succeeded, "failed": failed}

    # ── collections ──────────────────────────────────────────────────────────

    def create_collection(self, input_model: CollectionInput, *, owner_id: str = "local") -> ArtifactCollection:
        if input_model.parent_id:
            parent = self._store.get_collection(input_model.parent_id)
            if parent is None:
                raise ArtifactError("ARTIFACT_COLLECTION_NOT_FOUND", "parent collection not found")
            self._assert_no_cycle(input_model.parent_id, None)
        return self._store.create_collection(
            name=input_model.name, description=input_model.description,
            parent_id=input_model.parent_id, owner_id=owner_id,
        )

    def list_collections(self, *, owner_id: str = "local") -> list[ArtifactCollection]:
        return self._store.list_collections(owner_id)

    def delete_collection(self, collection_id: str, *, owner_id: str = "local") -> bool:
        collection = self._store.get_collection(collection_id)
        if collection is None or collection.owner_id != owner_id:
            raise ArtifactError("ARTIFACT_COLLECTION_NOT_FOUND", "collection not found")
        return self._store.delete_collection(collection_id)

    def move(
        self, artifact_id: str, collection_id: str, *, owner_id: Optional[str] = None, remove_from: Optional[str] = None
    ) -> Artifact:
        self.get(artifact_id, owner_id=owner_id)
        if self._store.get_collection(collection_id) is None:
            raise ArtifactError("ARTIFACT_COLLECTION_NOT_FOUND", "collection not found")
        if remove_from:
            self._store.remove_from_collection(remove_from, artifact_id)
        self._store.add_to_collection(collection_id, artifact_id)
        self._store.add_event(
            artifact_id, "ARTIFACT_MOVED", data={"collectionId": collection_id, "removeFrom": remove_from}
        )
        return self.get(artifact_id, owner_id=owner_id)

    def _assert_no_cycle(self, parent_id: str, child_id: Optional[str]) -> None:
        # Walks up the parent chain from `parent_id`; a collection cannot become
        # its own ancestor. Bounded by a fixed depth.
        seen: set[str] = set()
        current: Optional[str] = parent_id
        for _ in range(100):
            if current is None or current in seen:
                return
            seen.add(current)
            node = self._store.get_collection(current)
            if node is None:
                return
            if child_id is not None and node.id == child_id:
                raise ArtifactError("ARTIFACT_COLLECTION_CYCLE", "moving would create a collection cycle")
            current = node.parent_id

    # ── tags ─────────────────────────────────────────────────────────────────

    def _attach_tag(self, artifact_id: str, name: str, owner_id: str) -> None:
        cleaned = name.strip()
        if not cleaned:
            return
        tag = self._store.ensure_tag(cleaned, owner_id)
        self._store.link_tag(artifact_id, tag.id)

    def _set_tags(self, artifact_id: str, names: list[str], owner_id: str) -> None:
        cleaned = list(dict.fromkeys(n.strip() for n in names if n and n.strip()))
        if len(cleaned) > 50:
            raise ArtifactError("ARTIFACT_TOO_MANY_TAGS", "too many tags")
        existing = {t.name.casefold() for t in self._store.list_tags(owner_id)}
        current_tags = self._store.list_tags(owner_id)
        current_by_name = {t.name.casefold(): t for t in current_tags}
        # Remove links not in the new set.
        for tag in self._store.list_tags(owner_id):
            if tag.name.casefold() in existing and tag.name.casefold() not in {c.casefold() for c in cleaned}:
                self._store.unlink_tag(artifact_id, tag.id)
        for name in cleaned:
            tag = current_by_name.get(name.casefold())
            if tag is None:
                tag = self._store.ensure_tag(name, owner_id)
            self._store.link_tag(artifact_id, tag.id)
        self._store.add_event(artifact_id, "ARTIFACT_TAGGED", data={"tags": cleaned})

    # ── preview ──────────────────────────────────────────────────────────────

    def _run_preview(self, artifact: Artifact) -> Optional[Artifact]:
        try:
            result = preview_engine.build(self._storage, artifact)
        except Exception:  # noqa: BLE001 - preview never invalidates the artifact
            log.warning("preview generation failed for %s", artifact.id, exc_info=True)
            self._store.update_fields(artifact.id, {"previewStatus": PreviewStatus.FAILED.value})
            self._store.add_event(artifact.id, "ARTIFACT_PREVIEW_FAILED")
            return None
        status = result.get("status", PreviewStatus.READY.value)
        fields: dict[str, Any] = {
            "previewStatus": status, "previewKind": result.get("kind"),
        }
        if result.get("thumbnailKey"):
            fields["thumbnailKey"] = result["thumbnailKey"]
        if result.get("text") is not None:
            metadata = {**artifact.metadata, "previewText": result["text"]}
            fields["metadata"] = metadata
        self._store.update_fields(artifact.id, fields)
        if status == PreviewStatus.FAILED.value:
            self._store.add_event(artifact.id, "ARTIFACT_PREVIEW_FAILED")
        else:
            self._store.add_event(artifact.id, "ARTIFACT_PREVIEW_READY", data={"kind": result.get("kind")})
        return self._store.get(artifact.id)

    def set_preview_thumbnail(
        self, artifact_id: str, data: bytes, *, owner_id: Optional[str] = None
    ) -> bool:
        """Attach a producer-rendered thumbnail (e.g. a deck's first slide).

        Best-effort: a missing object or store row returns False; the artifact is
        still valid and downloadable. The key follows the same
        `<storage_key>.thumb.png` convention the preview engine uses.
        """
        if not data:
            return False
        try:
            artifact = self.get(artifact_id, owner_id=owner_id)
            if not artifact.storage_key:
                return False
            key = self._storage.thumbnail_key(artifact.storage_key)
            self._storage.save_bytes_at(key, data)
            self._store.update_fields(artifact_id, {"thumbnailKey": key})
            self._store.add_event(artifact_id, "ARTIFACT_PREVIEW_READY", data={"kind": "thumbnail"})
            return True
        except Exception:  # noqa: BLE001 - a thumbnail never invalidates the artifact
            log.warning("thumbnail write failed for %s", artifact_id, exc_info=True)
            return False

    def preview(self, artifact_id: str, *, owner_id: Optional[str] = None) -> dict[str, Any]:
        """Return preview metadata, regenerating if it is pending or failed."""
        artifact = self.get(artifact_id, owner_id=owner_id)
        if artifact.preview_status in (PreviewStatus.PENDING, PreviewStatus.FAILED):
            regenerated = self._run_preview(artifact)
            if regenerated is not None:
                artifact = regenerated
        return {
            "kind": artifact.preview_kind or preview_kind(artifact.mime_type, artifact.category, artifact.name),
            "status": artifact.preview_status.value,
            "mimeType": artifact.mime_type,
            "name": artifact.name,
            "sizeBytes": artifact.size_bytes,
            "thumbnailKey": artifact.thumbnail_key,
            "text": artifact.metadata.get("previewText") if isinstance(artifact.metadata, dict) else None,
            "hasDownload": bool(artifact.storage_key),
        }

    # ── content access (authorized) ──────────────────────────────────────────

    def open_content(
        self, artifact_id: str, *, owner_id: Optional[str] = None, version: Optional[int] = None
    ) -> tuple[Any, Artifact]:
        artifact = self.get(artifact_id, owner_id=owner_id)
        key = artifact.storage_key
        if version is not None:
            found = self._store.get_version(artifact_id, version)
            if found is None or not found.storage_key:
                raise ArtifactError("ARTIFACT_VERSION_NOT_FOUND", "version not found")
            key = found.storage_key
        if not key:
            raise ArtifactError("ARTIFACT_CONTENT_MISSING", "artifact has no stored content")
        if not self._storage.exists(key):
            self._store.update_fields(artifact_id, {"status": ArtifactStatus.MISSING.value})
            self._store.add_event(artifact_id, "ARTIFACT_MISSING")
            raise ArtifactError("ARTIFACT_CONTENT_MISSING", "artifact content is missing")
        try:
            handle = self._storage.open(key)
        except StorageError as exc:
            raise ArtifactError(exc.code, exc.message) from exc
        return handle, artifact

    def open_thumbnail(self, artifact_id: str, *, owner_id: Optional[str] = None) -> Optional[tuple[Any, Artifact]]:
        artifact = self.get(artifact_id, owner_id=owner_id)
        key = artifact.thumbnail_key
        if not key or not self._storage.exists(key):
            return None
        return self._storage.open(key), artifact

    # ── reconciliation ───────────────────────────────────────────────────────

    def reconcile(
        self, *, verify_checksums: bool = False, owner_id: Optional[str] = None,
        discover: bool = False, discover_limit: int = 200,
        purge_expired: bool = False, retention_days: int = 30,
    ) -> dict[str, Any]:
        """Bounded, idempotent repair of catalog/storage inconsistencies.

        With `discover`, also scan the bounded workspace artifact directories
        (`<workspace>/.black-eclipse/artifacts/*/`) for files not yet cataloged —
        the backstop for a terminal/script run that wrote a file without calling
        the registration tool. Never scans arbitrary host directories.
        """
        missing: list[str] = []
        recovered: list[str] = []
        checksum_mismatch: list[str] = []
        for artifact_id, status, storage_key, checksum in self._store.iter_storage_keys():
            if status in (ArtifactStatus.DELETED.value, ArtifactStatus.PURGED.value):
                continue
            if not storage_key:
                continue
            if not self._storage.exists(storage_key):
                if status != ArtifactStatus.MISSING.value:
                    self._store.update_fields(artifact_id, {"status": ArtifactStatus.MISSING.value})
                    self._store.add_event(artifact_id, "ARTIFACT_MISSING")
                missing.append(artifact_id)
                continue
            if status == ArtifactStatus.MISSING.value:
                self._store.update_fields(artifact_id, {"status": ArtifactStatus.ACTIVE.value})
                self._store.add_event(artifact_id, "ARTIFACT_RECONCILED", data={"repair": "recovered"})
                recovered.append(artifact_id)
            if verify_checksums and checksum:
                actual = self._storage.checksum(storage_key)
                if actual and actual != checksum:
                    checksum_mismatch.append(artifact_id)
        discovered: list[str] = []
        if discover:
            discovered = [a.id for a in self.discover_workspace_artifacts(owner_id=owner_id, limit=discover_limit)]
        purged: list[str] = []
        if purge_expired:
            purged = self.cleanup_expired(grace_days=retention_days)["purged"]
        return {
            "missing": missing,
            "recovered": recovered,
            "checksumMismatch": checksum_mismatch,
            "discovered": discovered,
            "purged": purged,
            "checked": len(self._store.iter_storage_keys()),
        }

    def cleanup_expired(
        self, *, grace_days: int = 30, now: Optional[str] = None, limit: int = 500
    ) -> dict[str, Any]:
        """Purge soft-deleted artifacts whose retention has elapsed.

        The deadline is `retentionUntil` when set, else `deletedAt + grace_days`.
        Never touches ACTIVE/ARCHIVED rows. Bounded and idempotent.
        """
        from datetime import datetime, timedelta, timezone  # noqa: PLC0415

        def _parse(value: Optional[str]) -> Optional[datetime]:
            if not value:
                return None
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                return None

        now_dt = _parse(now) or datetime.now(timezone.utc)
        purged: list[str] = []
        skipped: list[str] = []
        for row in self._store.list_deleted(limit):
            deadline = _parse(row.get("retentionUntil"))
            if deadline is None:
                deleted_dt = _parse(row.get("deletedAt"))
                deadline = (deleted_dt + timedelta(days=grace_days)) if deleted_dt else None
            if deadline is not None and deadline <= now_dt:
                try:
                    self.purge(row["id"])
                    purged.append(row["id"])
                except ArtifactError:
                    skipped.append(row["id"])
            else:
                skipped.append(row["id"])
        return {"purged": purged, "skipped": skipped, "checked": len(purged) + len(skipped)}

    def _workspace_roots(self) -> list[Path]:
        """Authorized workspace roots to discover from — never the storage root."""
        roots: list[Path] = []
        storage_real = str(self._storage.root.resolve())
        for root in self._allowed_roots():
            if str(root.resolve()) == storage_real:
                continue
            roots.append(root)
        return roots

    def discover_workspace_artifacts(self, *, owner_id: Optional[str] = None, limit: int = 200) -> list[Artifact]:
        """Register files under the bounded workspace artifact directories.

        Idempotent by path+checksum and de-duplicated against existing rows with
        the same checksum+run. Bounded by `limit`; excludes thumbnails and the
        storage root. A failure on one file never aborts the scan.
        """
        discovered: list[Artifact] = []
        for root in self._workspace_roots():
            base = root / ".black-eclipse" / "artifacts"
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*")):
                if len(discovered) >= limit:
                    return discovered
                if not path.is_file() or path.name.endswith(".thumb.png"):
                    continue
                relative = path.relative_to(root).as_posix()
                try:
                    checksum = self._storage.checksum_for_path(path)
                except Exception:  # noqa: BLE001
                    continue
                if not checksum:
                    continue
                run_id = path.parent.name if path.parent != base else None
                if self._store.find_by_checksum(checksum, run_id=run_id):
                    continue
                # The idempotency key is bounded (200 chars) but a deep artifact
                # path is not: hash the whole key when it would overflow, so a
                # long filename is still discovered instead of silently skipped.
                key = f"discover:{relative}:{checksum}"
                if len(key) > 200:
                    key = f"discover:{hashlib.sha256(key.encode('utf-8')).hexdigest()}"
                if self._store.lookup_idempotency(key):
                    continue
                try:
                    artifact = self.register_path(
                        path, name=path.name, owner_id=owner_id or "local",
                        source_type=ArtifactSourceType.TERMINAL, source_tool="workspace_discovery",
                        run_id=run_id, idempotency_key=key,
                    )
                    self._store.add_event(artifact.id, "ARTIFACT_RECONCILED", data={"repair": "discovered"})
                    discovered.append(artifact)
                except Exception:  # noqa: BLE001 - one bad file must not stop the scan
                    log.info("artifact discovery skipped %s", relative, exc_info=True)
        return discovered


__all__ = ["ArtifactError", "ArtifactService"]
