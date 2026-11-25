"""The video job layer: projects, renders, previews, inspection and repair.

Rendering is slow and CPU-bound, so every operation that touches the Rust
worker is a QUEUED Execution-Fabric job (type ``video``) — never inline. This
service is the ONE door to those jobs: it resolves projects, owns the
per-job workspace layout, maps render progress into Fabric progress, runs the
post-render inspection gate, registers final renders into the Artifact Center,
renders PNG previews (contact sheet + per-scene frames), and bounds the repair
loop around failed renders (`video/repair.py`).

The Fabric worker handler in ``video/fabric.py`` calls
``run_render_job`` / ``run_inspect_job`` / ``run_preview_job`` with the job's
cancel event and progress callback; the Fabric owns durability, retries,
cancellation and recovery around it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable, Optional

from ..persistence import now_iso
from . import repair
from .limits import check_queue_depth
from .config import VideoConfig, get_video_config
from .data import BINDING_FIELDS, BINDING_INTERVALS, has_bindings, resolve_storyboard
from .models import SUPPORTED_PROJECT_KINDS, VIDEO_JOB_TYPE, VideoError
from .projects import VideoProjectStore
from .repair import MAX_REPAIR_ATTEMPTS
from .runner import VideoRunner
from .spec import SCHEMA_VERSION as STORYBOARD_SCHEMA_VERSION

log = logging.getLogger("harness.video")

_MODE_DRAFT = "draft"
_MODE_FINAL = "final"


class VideoService:
    def __init__(
        self,
        *,
        config: Optional[VideoConfig] = None,
        runner: Optional[VideoRunner] = None,
        projects: Optional[VideoProjectStore] = None,
        artifact_provider: Optional[Callable[[], Any]] = None,
        manager_provider: Optional[Callable[[], Any]] = None,
        data_provider: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._config = config or get_video_config()
        self._runner = runner or VideoRunner(self._config)
        self._projects = projects or VideoProjectStore(
            self._config.resolve_video_root() / "projects"
        )
        self._artifact_provider = artifact_provider
        self._manager_provider = manager_provider
        self._data_provider_factory = data_provider

    # ── dependencies ─────────────────────────────────────────────────────────

    def _manager(self) -> Any:
        if self._manager_provider is not None:
            manager = self._manager_provider()
        else:
            from ..fabric.tools import get_fabric_manager  # noqa: PLC0415

            manager = get_fabric_manager()
        if manager is None:
            raise VideoError(
                "RUNTIME_UNAVAILABLE",
                "the Execution Fabric is unavailable; video jobs cannot be queued",
            )
        return manager

    def _artifact_service(self) -> Any:
        if self._artifact_provider is None:
            return None
        try:
            return self._artifact_provider()
        except Exception:  # noqa: BLE001 - a missing artifact layer is honest
            return None

    def _data_provider(self) -> Any:
        """The market-data provider for bound scenes, or None when absent.

        The bootstrap factory is the seam: it returns a `MarketGatewayProvider`
        over the EXISTING Market Data Gateway, or None when that layer did not
        install. Nothing here creates a second provider or cache."""
        if self._data_provider_factory is None:
            return None
        try:
            return self._data_provider_factory()
        except Exception:  # noqa: BLE001 - a missing market layer is honest
            return None

    # ── projects ─────────────────────────────────────────────────────────────

    def create_project(
        self, *, name: str, kind: str = "title_card", spec: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        return self._projects.create(name=name, kind=kind, spec=spec)

    def update_project(
        self, project_id: str, *, name: Optional[str] = None,
        spec: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Revise a project (bumping its revision) without rebuilding anything."""
        return self._projects.update(project_id, name=name, spec=spec)

    async def resolve_project_data(self, project_id: str) -> dict[str, Any]:
        """Resolve a storyboard's data bindings NOW (no render): the evidence
        manifest — per-figure source, retrieval time, units and freshness — an
        operator or agent can inspect before spending a render."""
        project = self.get_project(project_id)
        if str(project.get("kind") or "") != "storyboard":
            raise VideoError("NO_BINDINGS", "only storyboard projects can carry data bindings")
        spec = project.get("spec") or {}
        if not has_bindings(spec):
            raise VideoError("NO_BINDINGS", "this storyboard has no data bindings")
        provider = self._data_provider()
        if provider is None:
            raise VideoError(
                "DATA_UNAVAILABLE",
                "the market data layer is unavailable; bound scenes cannot be resolved",
            )
        _resolved, manifest = await resolve_storyboard(spec, provider)
        return manifest

    def get_project(self, project_id: str) -> dict[str, Any]:
        project = self._projects.get(project_id)
        if project is None:
            raise VideoError("PROJECT_NOT_FOUND", f"project {project_id!r} was not found")
        return project

    def list_projects(self) -> list[dict[str, Any]]:
        return self._projects.list()

    def validate_project(self, project_id: str) -> dict[str, Any]:
        """Spec problems + environment readiness in one honest report."""
        project = self.get_project(project_id)
        problems = self._projects.validate(project)
        environment = self._runner.environment_report()
        problems = problems + list(environment["problems"])
        return {
            "valid": not problems,
            "problems": problems,
            "warnings": list(environment["warnings"]),
            "environment": environment,
            "project": project,
        }

    def capabilities(self) -> dict[str, Any]:
        """What the pipeline can honestly do right now (readiness + limits)."""
        environment = self._runner.environment_report()
        return {
            "enabled": bool(self._config.enabled),
            "ready": bool(environment["ready"]),
            "projectKinds": list(SUPPORTED_PROJECT_KINDS),
            "storyboardSchemaVersion": STORYBOARD_SCHEMA_VERSION,
            "dataBindings": {
                "fields": list(BINDING_FIELDS),
                "intervals": list(BINDING_INTERVALS),
                "providerAvailable": self._data_provider() is not None,
            },
            "environment": environment,
            "preview": {
                "contactSheet": True,
                "frames": True,
                "artifactCategory": "image",
            },
            "repair": {
                "maxAttempts": MAX_REPAIR_ATTEMPTS,
                "transientRetries": self._config.transient_retries,
                "transientClasses": list(repair.TRANSIENT_ERROR_CLASSES),
            },
            "limits": {
                "maxConcurrent": self._config.max_concurrent,
                "maxPendingPerProject": self._config.max_pending_per_project,
                "draftTimeoutMs": self._config.draft_timeout_ms,
                "finalTimeoutMs": self._config.final_timeout_ms,
                "inspectTimeoutMs": self._config.inspect_timeout_ms,
                "previewTimeoutMs": self._config.preview_timeout_ms,
                "gcKeepJobs": self._config.gc_keep_jobs,
                "gcMaxTotalMb": self._config.gc_max_total_mb,
            },
        }

    # ── job API ──────────────────────────────────────────────────────────────


    async def render_draft(
        self, project_id: str, *, owner: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return await self._submit_render(
            project_id, mode=_MODE_DRAFT, owner=owner, idempotency_key=idempotency_key,
            context=context,
        )

    async def render_final(
        self, project_id: str, *, owner: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return await self._submit_render(
            project_id, mode=_MODE_FINAL, owner=owner, idempotency_key=idempotency_key,
            context=context,
        )

    async def _submit_render(
        self, project_id: str, *, mode: str, owner: Optional[str],
        idempotency_key: Optional[str],
        repair_of: Optional[str] = None,
        repair_attempt: Optional[int] = None,
        repair_diagnostics: Optional[dict[str, Any]] = None,
        context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        project = self.get_project(project_id)
        check_queue_depth(
            self._manager(), project["id"],
            cap=self._config.max_pending_per_project,
        )
        timeout_ms = (
            self._config.final_timeout_ms if mode == _MODE_FINAL else self._config.draft_timeout_ms
        )
        payload = {
            "action": "render",
            "mode": mode,
            "projectId": project["id"],
            "projectName": project["name"],
            "projectKind": project["kind"],
            "projectRevision": int(project.get("revision") or 1),
            "owner": owner,
        }
        if isinstance(context, dict) and context:
            payload["context"] = dict(context)
        kind = f"render_{mode}"
        if repair_of:
            payload["repairOf"] = str(repair_of)
            payload["repairAttempt"] = int(repair_attempt or 1)
            if repair_diagnostics:
                payload["repairDiagnostics"] = repair_diagnostics
            kind = "repair"
        handle = await self._manager().submit(
            VIDEO_JOB_TYPE,
            payload,
            timeout_ms=timeout_ms,
            idempotency_key=idempotency_key,
            metadata={"source": "video", "kind": kind, "projectId": project["id"]},
        )
        handle["projectId"] = project["id"]
        handle["projectName"] = project["name"]
        handle["mode"] = mode
        return handle

    async def preview_project(
        self, project_id: str, *, owner: Optional[str] = None,
        count: Optional[int] = None, columns: Optional[int] = None,
        width: Optional[int] = None, frames: bool = True,
        idempotency_key: Optional[str] = None,
        context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Queue a PNG preview: a labelled contact sheet (plus one frame per
        scene for storyboards), registered as image artifacts — so layout
        problems (clipped text, missing fonts) are visible to the agent/user
        BEFORE any expensive render, and never as a fake "video"."""
        project = self.get_project(project_id)
        check_queue_depth(
            self._manager(), project["id"],
            cap=self._config.max_pending_per_project,
        )
        payload: dict[str, Any] = {
            "action": "preview",
            "projectId": project["id"],
            "projectName": project["name"],
            "projectKind": project["kind"],
            "projectRevision": int(project.get("revision") or 1),
            "frames": bool(frames),
            "owner": owner,
        }
        if isinstance(context, dict) and context:
            payload["context"] = dict(context)
        if count is not None:
            payload["count"] = int(count)
        if columns is not None:
            payload["columns"] = int(columns)
        if width is not None:
            payload["width"] = int(width)
        handle = await self._manager().submit(
            VIDEO_JOB_TYPE,
            payload,
            timeout_ms=self._config.preview_timeout_ms,
            idempotency_key=idempotency_key,
            metadata={"source": "video", "kind": "preview", "projectId": project["id"]},
        )
        handle["projectId"] = project["id"]
        handle["projectName"] = project["name"]
        handle["kind"] = "preview"
        return handle

    async def render_repair(
        self, project_id: str, *, of_job_id: str, spec: Optional[dict[str, Any]] = None,
        name: Optional[str] = None, mode: str = _MODE_DRAFT, owner: Optional[str] = None,
        context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """One BOUNDED repair of a failed render: optionally revise the spec
        (revision bump), then queue a replacement render linked to the failure
        (`repairOf` + the diagnostics it responds to).

        The loop is capped: after `repair.MAX_REPAIR_ATTEMPTS` repairs of one
        failure line this refuses with `REPAIR_LIMIT_REACHED` — the explicit
        terminal state, never an endless retry. A non-transient failure with no
        spec/name change is refused (`REPAIR_REQUIRES_CHANGE`): repeating the
        same request would just repeat the failure.
        """
        parent = self.get_job(str(of_job_id or ""))
        repair.validate_repair_target(parent, str(project_id or ""))
        attempt = repair.check_repair_allowed(self.list_jobs(limit=200), str(of_job_id))
        clean_mode = str(mode or _MODE_DRAFT).strip().lower()
        if clean_mode not in (_MODE_DRAFT, _MODE_FINAL):
            raise VideoError("INVALID_INPUT", f"unknown render mode {clean_mode!r}")
        changed = spec is not None or name is not None
        if not changed and not repair.is_transient((parent or {}).get("errorClass")):
            raise VideoError(
                "REPAIR_REQUIRES_CHANGE",
                "this failure is not transient; revise the spec (or name) so the retry is a "
                "different request instead of a repeat of the same failure",
            )
        if changed:
            self.update_project(project_id, name=name, spec=spec)
        handle = await self._submit_render(
            project_id, mode=clean_mode, owner=owner, idempotency_key=None,
            repair_of=str(of_job_id), repair_attempt=attempt,
            repair_diagnostics=repair.diagnostics_from_job(
                parent, self._read_job_report(str(of_job_id))
            ),
            context=context,
        )
        handle["repair"] = {
            "ofJobId": str(of_job_id),
            "attempt": attempt,
            "maxAttempts": MAX_REPAIR_ATTEMPTS,
        }
        return handle

    async def inspect_video(
        self, *, job_id: Optional[str] = None, project_id: Optional[str] = None,
        owner: Optional[str] = None,
    ) -> dict[str, Any]:
        """Queue a frame inspection of a completed render.

        Pass the render's ``job_id``, or a ``project_id`` to inspect its latest
        successful render.
        """
        if job_id:
            source = self.get_job(job_id)
            if source is None:
                raise VideoError("JOB_NOT_FOUND", f"job {job_id!r} was not found")
            if (source.get("input") or {}).get("action") != "render":
                raise VideoError("INVALID_STATE", "inspect needs a render job id")
            if source.get("status") != "SUCCEEDED":
                raise VideoError(
                    "INVALID_STATE",
                    f"job {job_id!r} is {source.get('status')}, not a completed render",
                )
        elif project_id:
            self.get_project(project_id)
            source = self._latest_render_job(project_id)
            if source is None:
                raise VideoError(
                    "INVALID_STATE",
                    f"project {project_id!r} has no successful render to inspect yet",
                )
        else:
            raise VideoError("INVALID_INPUT", "pass a job_id or a project_id")
        output_path = str(((source.get("output") or {}).get("outputPath")) or "")
        if not output_path:
            raise VideoError("INVALID_STATE", "the render job recorded no output path")
        confined = self._confined(output_path)
        project_path: Optional[Path] = None
        if (source.get("input") or {}).get("projectKind") == "storyboard":
            source_job_id = str(source.get("id") or source.get("jobId") or "")
            candidate = self._config.resolve_video_root() / "jobs" / source_job_id / "spec.json"
            if not candidate.is_file():
                raise VideoError(
                    "INVALID_STATE",
                    "the render's project spec is missing; re-render the storyboard before inspecting",
                )
            project_path = candidate
        payload = {
            "action": "inspect",
            "outputPath": str(confined),
            "projectPath": str(project_path) if project_path is not None else None,
            "sourceJobId": source.get("id") or source.get("jobId"),
            "projectId": (source.get("input") or {}).get("projectId"),
            "owner": owner,
        }
        handle = await self._manager().submit(
            VIDEO_JOB_TYPE,
            payload,
            timeout_ms=self._config.inspect_timeout_ms,
            metadata={"source": "video", "kind": "inspect", "projectId": payload.get("projectId")},
        )
        return handle

    def get_job(self, job_id: str) -> Optional[dict[str, Any]]:
        return self._manager().get(job_id)

    def list_jobs(
        self, *, project_id: Optional[str] = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        jobs = self._manager().list_jobs(type=VIDEO_JOB_TYPE, limit=int(limit))
        if project_id:
            jobs = [
                j for j in jobs
                if (j.get("input") or {}).get("projectId") == project_id
            ]
        return jobs

    async def cancel_job(self, job_id: str) -> dict[str, Any]:
        return await self._manager().cancel(job_id)

    def get_artifact(self, artifact_id: str) -> dict[str, Any]:
        artifacts = self._artifact_service()
        if artifacts is None:
            raise VideoError("RUNTIME_UNAVAILABLE", "the Artifact Center is unavailable")
        from ..artifacts.service import ArtifactError  # noqa: PLC0415

        try:
            artifact = artifacts.get(artifact_id)
        except ArtifactError as exc:
            raise VideoError(exc.code, exc.message) from exc
        if artifact is None:
            raise VideoError("ARTIFACT_NOT_FOUND", f"artifact {artifact_id!r} was not found")
        if hasattr(artifact, "to_dict"):
            return artifact.to_dict()
        return _artifact_summary(artifact)

    # ── worker-side execution (called by the Fabric handler) ─────────────────

    async def run_render_job(
        self,
        payload: dict[str, Any],
        *,
        cancel_event: Optional[asyncio.Event] = None,
        progress: Optional[Callable[..., None]] = None,
    ) -> dict[str, Any]:
        mode = str(payload.get("mode") or _MODE_DRAFT).strip().lower()
        project_id = str(payload.get("projectId") or "")
        job_id = str(payload.get("jobId") or "manual")
        workspace = self._config.resolve_video_root() / "jobs" / job_id
        result: dict[str, Any] = {
            "status": "failed", "action": "render", "mode": mode,
            "projectId": project_id or None,
        }
        repair_of = str(payload.get("repairOf") or "").strip() or None
        if repair_of:
            result["repair"] = {
                "ofJobId": repair_of,
                "attempt": int(payload.get("repairAttempt") or 1),
                "maxAttempts": MAX_REPAIR_ATTEMPTS,
                "diagnostics": payload.get("repairDiagnostics")
                or repair.diagnostics_from_job(None),
            }
        details: dict[str, Any] = {}
        try:
            if mode not in (_MODE_DRAFT, _MODE_FINAL):
                raise VideoError("INVALID_INPUT", f"unknown render mode {mode!r}")
            project = self._projects.get(project_id) if project_id else None
            if project is None:
                raise VideoError("PROJECT_NOT_FOUND", f"project {project_id!r} was not found")
            problems = self._projects.validate(project)
            if problems:
                raise VideoError("INVALID_PROJECT", "; ".join(problems))

            workspace.mkdir(parents=True, exist_ok=True)
            out_path = workspace / ("final.mp4" if mode == _MODE_FINAL else "draft.mp4")
            attempts_budget = 1 + max(0, int(self._config.transient_retries))
            retries: list[dict[str, Any]] = []
            attempt = 0
            outcome: dict[str, Any] = {}
            while True:
                attempt += 1
                outcome = await self._attempt_render(
                    project, mode=mode, workspace=workspace, out_path=out_path,
                    cancel_event=cancel_event, progress=progress, attempt=attempt,
                    attempts_budget=attempts_budget,
                )
                if outcome.get("ok"):
                    break
                if not (
                    outcome.get("status") == "failed"
                    and repair.is_transient(outcome.get("errorClass"))
                    and attempt < attempts_budget
                ):
                    break
                retries.append({
                    "attempt": attempt,
                    "errorClass": outcome.get("errorClass"),
                    "error": str(outcome.get("error") or "")[:300],
                })
                self._progress(
                    progress, 0.05,
                    f"transient failure ({outcome.get('errorClass')}); retrying "
                    f"(attempt {attempt + 1} of {attempts_budget})",
                )
            if retries:
                result["retries"] = retries
            details = {
                key: outcome[key]
                for key in ("render", "inspection")
                if outcome.get(key) is not None
            }
            if not outcome.get("ok"):
                result.update(
                    status=outcome.get("status") or "failed",
                    error=outcome.get("error"),
                    errorClass=outcome.get("errorClass"),
                )
                if outcome.get("inspectionSummary") is not None:
                    result["inspection"] = outcome["inspectionSummary"]
                return self._finish_report(workspace, payload, result, details)
            result.update(
                outputPath=outcome["outputPath"], width=outcome.get("width"),
                height=outcome.get("height"), frames=outcome.get("frames"),
                durationSeconds=outcome.get("durationSeconds"),
                renderSeconds=outcome.get("renderSeconds"),
                sizeBytes=outcome.get("sizeBytes"),
            )
            if outcome.get("evidence") is not None:
                result["evidence"] = outcome["evidence"]
            if outcome.get("inspectionSummary") is not None:
                result["inspection"] = outcome["inspectionSummary"]

            artifact = None
            if mode == _MODE_FINAL:
                self._progress(progress, 0.97, "registering the video artifact")
                try:
                    artifact = self._register_artifact(
                        project=project, out_path=Path(outcome["outputPath"]), mode=mode,
                        job_id=job_id, render=outcome["render"], inspection=outcome["inspection"],
                        owner=payload.get("owner"), evidence=outcome.get("evidence"),
                        context=_artifact_context(payload),
                    )
                except VideoError as exc:
                    result.update(
                        status="failed",
                        error=f"the video rendered but could not be registered: {exc.message}",
                        errorClass="ARTIFACT_REGISTRATION_FAILED",
                    )
                    return self._finish_report(workspace, payload, result, details)
                result["review"] = self._review_for(
                    project["id"], payload.get("projectRevision"), job_id
                )
            result.update(
                status="completed",
                artifact=_artifact_summary(artifact) if artifact is not None else None,
            )
            self._progress(progress, 1.0, f"{mode} render complete")
            return self._finish_report(workspace, payload, result, details)
        except VideoError as exc:
            result.update(status="failed", error=exc.message, errorClass=exc.code)
        except Exception as exc:  # noqa: BLE001 - a worker failure is data
            result.update(status="failed", error=str(exc)[:1500], errorClass="VIDEO_ERROR")
        return self._finish_report(workspace, payload, result, details)

    async def _attempt_render(
        self, project: dict[str, Any], *, mode: str, workspace: Path, out_path: Path,
        cancel_event: Optional[asyncio.Event], progress: Optional[Callable[..., None]],
        attempt: int, attempts_budget: int,
    ) -> dict[str, Any]:
        """One resolve → render → inspect attempt (never registers artifacts).

        The returned outcome carries the raw render/inspection dicts so the
        caller can both report the LAST attempt and decide (from `errorClass`)
        whether the failure is transient and deserves a bounded retry."""
        suffix = f" (attempt {attempt} of {attempts_budget})" if attempt > 1 else ""
        outcome: dict[str, Any] = {"ok": False, "evidence": None}
        try:
            project_path, evidence = await self._prepare_project_spec(project, workspace, progress)
        except VideoError as exc:
            outcome.update(status="failed", error=exc.message, errorClass=exc.code)
            return outcome
        outcome["evidence"] = evidence
        self._progress(progress, 0.0, f"rendering the {mode} video{suffix}")
        render = await self._runner.render(
            out_path=out_path, draft=(mode == _MODE_DRAFT),
            timeout_ms=(
                self._config.final_timeout_ms if mode == _MODE_FINAL
                else self._config.draft_timeout_ms
            ),
            cancel_event=cancel_event, progress=progress, progress_scale=0.9,
            cwd=workspace, project_path=project_path,
        )
        outcome["render"] = render
        if not render["ok"]:
            outcome.update(
                status=render["status"], error=render.get("error"),
                errorClass=render.get("errorClass"),
            )
            return outcome
        # Isolation (Phase 8): the ONLY file a render may be is the one inside
        # this job's confined workspace. A worker that reports a path elsewhere
        # is refused before anything is inspected or registered.
        try:
            produced = self._confined(str(render["outputPath"]))
        except VideoError as exc:
            outcome.update(
                status="failed", errorClass="INVALID_STATE",
                error=f"the worker reported a render outside the confined workspace: {exc.message}",
            )
            return outcome
        outcome.update(
            outputPath=str(produced), width=render.get("width"),
            height=render.get("height"), frames=render.get("frames"),
            durationSeconds=render.get("endSeconds"),
            renderSeconds=render.get("elapsedSeconds"),
            sizeBytes=render.get("sizeBytes"),
        )
        if cancel_event is not None and cancel_event.is_set():
            outcome.update(status="cancelled", error="cancelled by operator",
                           errorClass="CANCELLED")
            return outcome

        self._progress(progress, 0.92, f"inspecting the rendered frames{suffix}")
        inspection = await self._runner.inspect(
            timeout_ms=self._config.inspect_timeout_ms, cancel_event=cancel_event,
            cwd=workspace, project_path=project_path,
        )
        findings = list(inspection.get("findings") or [])
        outcome["inspection"] = inspection
        outcome["inspectionSummary"] = {
            "ok": bool(inspection.get("ok")),
            "checkedFrames": inspection.get("checkedFrames"),
            "findings": findings[:20],
            "findingsTotal": len(findings),
        }
        if not inspection["ok"]:
            status, error, error_class = self._inspection_failure(inspection, findings)
            outcome.update(status=status, error=error, errorClass=error_class)
            return outcome
        outcome["ok"] = True
        return outcome

    async def _prepare_project_spec(
        self, project: dict[str, Any], workspace: Path,
        progress: Optional[Callable[..., None]] = None,
    ) -> tuple[Optional[Path], Optional[dict[str, Any]]]:
        """Write the exact spec a render/preview will use (bindings resolved).

        Returns ``(project_path, evidence)``; ``(None, None)`` for kinds that
        render built-in content (`title_card`). Held in ONE place so a preview
        and a render can never disagree about what the video is."""
        if str(project.get("kind") or "") != "storyboard":
            return None, None
        storyboard = project.get("spec") or {}
        evidence: Optional[dict[str, Any]] = None
        if has_bindings(storyboard):
            provider = self._data_provider()
            if provider is None:
                raise VideoError(
                    "DATA_UNAVAILABLE",
                    "the market data layer is unavailable; bound scenes cannot be resolved",
                )
            self._progress(progress, 0.02, "resolving market data for the storyboard")
            storyboard, evidence = await resolve_storyboard(storyboard, provider)
        project_path = workspace / "spec.json"
        _write_json(project_path, storyboard)
        return project_path, evidence

    def _read_job_report(self, job_id: str) -> Optional[dict[str, Any]]:
        """A job's persisted `report.json`, or None — the audit record the
        Fabric keeps when a failed job has no output row."""
        try:
            path = self._config.resolve_video_root() / "jobs" / str(job_id) / "report.json"
            if not path.is_file():
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:  # noqa: BLE001 - a missing report is an honest None
            return None

    def _review_for(
        self, project_id: str, revision: Any, exclude_job_id: str,
    ) -> Optional[dict[str, Any]]:
        """The latest SUCCEEDED draft/preview of the SAME revision — the honest
        answer to "did anyone get to review this cut before the final?"."""
        if revision is None:
            return None
        try:
            jobs = self._manager().list_jobs(type=VIDEO_JOB_TYPE, limit=200)
        except Exception:  # noqa: BLE001 - review provenance is an aid, never a failure
            return None
        candidates: list[dict[str, Any]] = []
        for job in jobs:
            if str(job.get("id") or job.get("jobId") or "") == exclude_job_id:
                continue
            if str(job.get("status") or "") != "SUCCEEDED":
                continue
            job_input = job.get("input") or {}
            if str(job_input.get("projectId") or "") != project_id:
                continue
            stored_revision = job_input.get("projectRevision")
            if str(stored_revision if stored_revision is not None else "") != str(revision):
                continue
            action = str(job_input.get("action") or "")
            if action == "preview":
                kind = "preview"
            elif action == "render" and str(job_input.get("mode") or "") == "draft":
                kind = "draft"
            else:
                continue
            candidates.append({
                "jobId": job.get("id") or job.get("jobId"),
                "kind": kind,
                "projectRevision": revision,
                "createdAt": job.get("createdAt") or 0,
            })
        if not candidates:
            return None
        candidates.sort(key=lambda candidate: candidate.get("createdAt") or 0)
        return candidates[-1]

    async def run_inspect_job(
        self,
        payload: dict[str, Any],
        *,
        cancel_event: Optional[asyncio.Event] = None,
        progress: Optional[Callable[..., None]] = None,
    ) -> dict[str, Any]:
        job_id = str(payload.get("jobId") or "manual")
        workspace = self._config.resolve_video_root() / "jobs" / job_id
        result: dict[str, Any] = {
            "status": "failed", "action": "inspect",
            "sourceJobId": payload.get("sourceJobId"),
        }
        try:
            output_path = str(payload.get("outputPath") or "")
            if not output_path:
                raise VideoError("INVALID_INPUT", "an inspect job needs an outputPath")
            confined = self._confined(output_path)
            project_path: Optional[Path] = None
            project_path_raw = str(payload.get("projectPath") or "")
            if project_path_raw:
                project_path = self._confined(project_path_raw)
            self._progress(progress, 0.1, "inspecting the rendered frames")
            inspection = await self._runner.inspect(
                timeout_ms=self._config.inspect_timeout_ms, cancel_event=cancel_event,
                cwd=confined.parent, project_path=project_path,
            )
            findings = list(inspection.get("findings") or [])
            result.update(
                checkedFrames=inspection.get("checkedFrames"),
                findings=findings[:20],
                findingsTotal=len(findings),
                ok=bool(inspection.get("ok")),
            )
            if inspection["ok"]:
                result["status"] = "completed"
                self._progress(progress, 1.0, "inspection clean")
            else:
                self._apply_inspection_failure(result, inspection, findings)
            return self._finish_report(workspace, payload, result, {"inspection": inspection})
        except VideoError as exc:
            result.update(status="failed", error=exc.message, errorClass=exc.code)
        except Exception as exc:  # noqa: BLE001
            result.update(status="failed", error=str(exc)[:1500], errorClass="VIDEO_ERROR")
        return self._finish_report(workspace, payload, result, {})

    # ── previews (Phase 6) ───────────────────────────────────────────────────

    async def run_preview_job(
        self,
        payload: dict[str, Any],
        *,
        cancel_event: Optional[asyncio.Event] = None,
        progress: Optional[Callable[..., None]] = None,
    ) -> dict[str, Any]:
        """Render PNG review material WITHOUT producing a video for the user:
        a labelled contact sheet plus one frame per scene (storyboards), with
        the frame diagnostics the worker found while sampling. The PNGs are
        registered as image artifacts so the agent (and the chat surface) can
        actually look at the layout before anyone pays for a render."""
        job_id = str(payload.get("jobId") or "manual")
        workspace = self._config.resolve_video_root() / "jobs" / job_id
        result: dict[str, Any] = {
            "status": "failed", "action": "preview",
            "projectId": (payload.get("projectId") or None),
        }
        details: dict[str, Any] = {}
        try:
            project_id = str(payload.get("projectId") or "")
            project = self._projects.get(project_id) if project_id else None
            if project is None:
                raise VideoError("PROJECT_NOT_FOUND", f"project {project_id!r} was not found")
            problems = self._projects.validate(project)
            if problems:
                raise VideoError("INVALID_PROJECT", "; ".join(problems))

            workspace.mkdir(parents=True, exist_ok=True)
            project_path, evidence = await self._prepare_project_spec(project, workspace, progress)
            previews_dir = workspace / "previews"
            count = _clamp_int(payload.get("count"), default=8, minimum=2, maximum=24)
            columns = _clamp_int(payload.get("columns"), default=4, minimum=1, maximum=8)
            width = _clamp_int(payload.get("width"), default=480, minimum=160, maximum=960)

            self._progress(progress, 0.1, "rendering the contact sheet")
            strip = await self._runner.strip(
                out_path=previews_dir / "strip.png", count=count, columns=columns,
                width=width, timeout_ms=self._config.preview_timeout_ms,
                cancel_event=cancel_event, cwd=workspace, project_path=project_path,
            )
            details["strip"] = strip
            if not strip["ok"]:
                result.update(status=strip["status"], error=strip.get("error"),
                              errorClass=strip.get("errorClass"))
                return self._finish_report(workspace, payload, result, details)
            findings: list[Any] = [
                diagnostic
                for report in strip.get("diagnostics") or []
                if isinstance(report, dict)
                for diagnostic in (report.get("diagnostics") or [])
            ]
            previews: list[dict[str, Any]] = [{
                "kind": "strip",
                "name": Path(strip["outputPath"]).name,
                "path": str(self._confined(strip["outputPath"])),
                "frames": len(strip.get("frames") or []),
            }]

            cancelled = cancel_event is not None and cancel_event.is_set()
            want_frames = bool(payload.get("frames", True)) and project.get("kind") == "storyboard"
            if want_frames and not cancelled:
                self._progress(progress, 0.45, "rendering one frame per scene")
                frames = await self._runner.frames(
                    at_specs=_scene_frame_specs(project.get("spec") or {}),
                    out_dir=previews_dir / "frames",
                    timeout_ms=self._config.preview_timeout_ms,
                    cancel_event=cancel_event, cwd=workspace, project_path=project_path,
                )
                details["frames"] = frames
                if not frames["ok"]:
                    result.update(status=frames["status"], error=frames.get("error"),
                                  errorClass=frames.get("errorClass"))
                    return self._finish_report(workspace, payload, result, details)
                for entry in frames["frames"]:
                    previews.append({
                        "kind": "frame",
                        "name": Path(entry["path"]).name,
                        "path": str(self._confined(entry["path"])),
                        "seconds": entry.get("seconds"),
                        "scene": (entry.get("scenes") or [None])[0],
                    })
                    findings.extend(entry.get("diagnostics") or [])
            if cancelled:
                result.update(status="cancelled", error="cancelled by operator",
                              errorClass="CANCELLED")
                return self._finish_report(workspace, payload, result, details)

            self._progress(progress, 0.85, "registering the preview images")
            registered = self._register_previews(
                project=project, job_id=job_id, previews=previews,
                owner=payload.get("owner"), revision=payload.get("projectRevision"),
                context=_artifact_context(payload),
            )
            summary = _summarize_findings(findings)
            result.update(
                status="completed",
                previews=registered,
                findings=summary["items"],
                findingsTotal=summary["total"],
                findingsErrors=summary["errors"],
                findingsWarnings=summary["warnings"],
            )
            if evidence is not None:
                result["evidence"] = evidence
            if summary["errors"]:
                self._progress(progress, 1.0,
                               f"preview complete with {summary['errors']} error-level finding(s)")
            else:
                self._progress(progress, 1.0, "preview complete")
            return self._finish_report(workspace, payload, result, details)
        except VideoError as exc:
            result.update(status="failed", error=exc.message, errorClass=exc.code)
        except Exception as exc:  # noqa: BLE001
            result.update(status="failed", error=str(exc)[:1500], errorClass="VIDEO_ERROR")
        return self._finish_report(workspace, payload, result, details)

    def _register_previews(
        self, *, project: dict[str, Any], job_id: str, previews: list[dict[str, Any]],
        owner: Optional[str], revision: Any, context: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        """Register each preview PNG as an image artifact — best-effort: the
        file on disk is real either way, so a missing catalog is recorded
        per-file, never a false failure (or a false artifact)."""
        registered: list[dict[str, Any]] = []
        for preview in previews:
            entry = dict(preview)
            artifact, error = self._register_preview_file(
                project=project, job_id=job_id, path=Path(str(preview.get("path"))),
                kind=str(preview.get("kind") or "strip"), seconds=preview.get("seconds"),
                owner=owner, revision=revision, context=context,
            )
            entry["artifactId"] = artifact.get("id") if artifact else None
            entry["registered"] = artifact is not None
            if error:
                entry["registrationError"] = error
            registered.append(entry)
        return registered

    def _register_preview_file(
        self, *, project: dict[str, Any], job_id: str, path: Path, kind: str,
        seconds: Any, owner: Optional[str], revision: Any,
        context: Optional[dict[str, Any]] = None,
    ) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        artifacts = self._artifact_service()
        if artifacts is None:
            return None, "the Artifact Center is unavailable"
        from ..artifacts.model import ArtifactCategory, ArtifactSourceType  # noqa: PLC0415

        suffix = f"frame {float(seconds):.2f}s" if kind == "frame" and seconds is not None else kind
        try:
            artifact = artifacts.register_path(
                path,
                name=f"{project['name']} - preview {suffix}.png",
                owner_id=owner or "local",
                description=f"Preview {kind} of video project {project['id']}.",
                category=ArtifactCategory.IMAGE,
                mime_type="image/png",
                source_type=ArtifactSourceType.JOB,
                source_tool="video",
                idempotency_key=f"video-preview:{job_id}:{path.name}",
                tags=["video", "fframes", "preview"],
                metadata={
                    "videoJobId": job_id,
                    "projectId": project["id"],
                    "projectRevision": revision,
                    "kind": kind,
                    **({"seconds": seconds} if seconds is not None else {}),
                },
                **_context_kwargs(context),
            )
        except Exception as exc:  # noqa: BLE001 - a catalog failure is recorded, not fatal
            return None, str(exc)[:300]
        return _artifact_summary(artifact), None

    # ── internals ────────────────────────────────────────────────────────────

    @staticmethod
    def _inspection_failure(
        inspection: dict[str, Any], findings: list[Any]
    ) -> tuple[str, str, str]:
        """Map a failed inspection to (status, error, errorClass)."""
        status = inspection.get("status")
        if status == "timed_out":
            return "timed_out", inspection.get("error") or "inspection timed out", "TIMEOUT"
        if status == "cancelled":
            return "cancelled", "cancelled by operator", "CANCELLED"
        detail = ""
        if findings:
            first = findings[0]
            if isinstance(first, dict):
                detail = str(first.get("message") or first.get("detail") or first)[:300]
            else:
                detail = str(first)[:300]
        return "failed", (
            f"the rendered video failed inspection: {detail}"
            if detail else
            f"the rendered video failed inspection: {inspection.get('error') or 'problems reported'}"
        ), "INSPECTION_FAILED"

    def _apply_inspection_failure(
        self, result: dict[str, Any], inspection: dict[str, Any], findings: list[Any]
    ) -> None:
        status, error, error_class = self._inspection_failure(inspection, findings)
        result.update(status=status, error=error, errorClass=error_class)

    def _register_artifact(
        self, *, project: dict[str, Any], out_path: Path, mode: str, job_id: str,
        render: dict[str, Any], inspection: dict[str, Any], owner: Optional[str],
        evidence: Optional[dict[str, Any]] = None,
        context: Optional[dict[str, Any]] = None,
    ) -> Any:
        artifacts = self._artifact_service()
        if artifacts is None:
            raise VideoError(
                "RUNTIME_UNAVAILABLE",
                "the Artifact Center is unavailable; the final render cannot be registered",
            )
        from ..artifacts.model import ArtifactSourceType  # noqa: PLC0415

        findings = list(inspection.get("findings") or [])
        metadata = {
            "videoJobId": job_id,
            "projectId": project["id"],
            "projectKind": project["kind"],
            "mode": mode,
            "width": render.get("width"),
            "height": render.get("height"),
            "frames": render.get("frames"),
            "durationSeconds": render.get("endSeconds"),
            "elapsedSeconds": render.get("elapsedSeconds"),
            "worker": "fframes",
            "workerVersion": "1.2.0",
            "inspection": {
                "ok": bool(inspection.get("ok")),
                "checkedFrames": inspection.get("checkedFrames"),
                "findings": len(findings),
            },
        }
        if evidence is not None:
            metadata["evidence"] = evidence
        return artifacts.register_path(
            out_path,
            name=f"{project['name']}.mp4",
            owner_id=owner or "local",
            description=(
                f"Rendered by the fframes video worker (project {project['id']}, {mode})."
            ),
            source_type=ArtifactSourceType.JOB,
            source_tool="video",
            idempotency_key=f"video-artifact:{job_id}",
            tags=["video", "fframes"],
            metadata=metadata,
            **_context_kwargs(context),
        )

    def _latest_render_job(self, project_id: str) -> Optional[dict[str, Any]]:
        try:
            jobs = self._manager().list_jobs(type=VIDEO_JOB_TYPE, limit=200)
        except VideoError:
            return None
        candidates = [
            j for j in jobs
            if (j.get("input") or {}).get("projectId") == project_id
            and (j.get("input") or {}).get("action") == "render"
            and j.get("status") == "SUCCEEDED"
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda j: j.get("createdAt") or 0)
        return candidates[-1]

    def _confined(self, path_str: str) -> Path:
        root = self._config.resolve_video_root().resolve()
        try:
            path = Path(path_str).resolve()
        except Exception as exc:  # noqa: BLE001
            raise VideoError("INVALID_INPUT", f"unresolvable path {path_str!r}") from exc
        if not (path == root or root in path.parents):
            raise VideoError(
                "INVALID_INPUT", "refusing to touch a file outside the video workspace"
            )
        if not path.is_file():
            raise VideoError("NOT_FOUND", f"no rendered file at {path}")
        return path

    def _finish_report(
        self, workspace: Path, payload: dict[str, Any], result: dict[str, Any],
        details: dict[str, Any],
    ) -> dict[str, Any]:
        report_path = workspace / "report.json"
        result["reportPath"] = str(report_path)
        try:
            workspace.mkdir(parents=True, exist_ok=True)
            report = {
                "request": dict(payload),
                "result": dict(result),
                "details": details,
                "writtenAt": now_iso(),
            }
            tmp = report_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
            os.replace(tmp, report_path)
        except Exception:  # noqa: BLE001 - a report is an audit aid, never a failure source
            pass
        return result

    @staticmethod
    def _progress(callback: Optional[Callable[..., None]], fraction: float,
                  message: Optional[str] = None) -> None:
        if callback is None:
            return
        try:
            callback(fraction, message)
        except Exception:  # noqa: BLE001
            pass


def _artifact_context(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The execution context a job was queued with (conversation/run/agent)."""
    context = payload.get("context")
    return dict(context) if isinstance(context, dict) and context else None


def _context_kwargs(context: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Registration kwargs that associate a produced artifact with the
    conversation/run/agent it came from (all optional, all validated by the
    Artifact Center's own input model)."""
    if not isinstance(context, dict):
        return {}
    kwargs: dict[str, Any] = {}
    conversation_id = str(context.get("conversationId") or "").strip()
    if conversation_id:
        kwargs["conversation_id"] = conversation_id[:200]
    run_id = str(context.get("runId") or "").strip()
    if run_id:
        kwargs["run_id"] = run_id[:200]
    agent_id = str(context.get("agentId") or "").strip()
    if agent_id:
        kwargs["agent_id"] = agent_id[:200]
    return kwargs


def _artifact_summary(artifact: Any) -> dict[str, Any]:
    category = getattr(artifact, "category", None)
    return {
        "id": getattr(artifact, "id", None),
        "name": getattr(artifact, "name", None),
        "mimeType": getattr(artifact, "mime_type", None),
        "sizeBytes": getattr(artifact, "size_bytes", None),
        "category": getattr(category, "value", category),
        "checksum": getattr(artifact, "checksum", None),
        "thumbnailKey": getattr(artifact, "thumbnail_key", None),
    }


def _clamp_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value) if value is not None else default
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _scene_frame_specs(spec: dict[str, Any]) -> list[str]:
    """`#0@50%`, `#1@50%`, ... — the middle frame of every scene, addressed by
    index so fades/overlaps cannot shift the target."""
    scenes = spec.get("scenes") if isinstance(spec, dict) else None
    if not isinstance(scenes, list) or not scenes:
        return ["50%"]
    return [f"#{index}@50%" for index in range(len(scenes))]


_SEVERITY_RANK = {"error": 0, "warning": 1, "info": 2}


def _summarize_findings(diagnostics: list[Any]) -> dict[str, Any]:
    """Deduplicate frame diagnostics: the same key across frames is ONE finding."""
    by_key: dict[str, dict[str, Any]] = {}
    for diagnostic in diagnostics:
        if not isinstance(diagnostic, dict):
            continue
        key = str(diagnostic.get("key") or diagnostic.get("message") or "finding")
        if key not in by_key:
            by_key[key] = {
                "key": key,
                "severity": str(diagnostic.get("severity") or "info"),
                "kind": diagnostic.get("kind"),
                "message": str(diagnostic.get("message") or "")[:300],
            }
    items = sorted(
        by_key.values(),
        key=lambda item: (_SEVERITY_RANK.get(item["severity"], 3), str(item["key"])),
    )
    return {
        "items": items[:20],
        "total": len(items),
        "errors": sum(1 for item in items if item["severity"] == "error"),
        "warnings": sum(1 for item in items if item["severity"] == "warning"),
    }


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


_default_service: Optional[VideoService] = None


def set_video_service(service: Optional[VideoService]) -> None:
    global _default_service
    _default_service = service


def get_video_service() -> Optional[VideoService]:
    return _default_service


__all__ = ["VideoService", "get_video_service", "set_video_service"]
