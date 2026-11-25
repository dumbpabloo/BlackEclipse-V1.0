"""The structured, isolated interface to the fframes render process.

The Rust worker is invoked as a fixed executable with fixed arguments — never a
shell, never concatenated input. Its machine contract (pinned with fframes
1.2.0 and verified against this repository's worker):

  render   ``fframes-worker render --json [--draft] -o <abs path>``
           stdout: one JSON doc ``{output, frames{start,end}, start_seconds,
           end_seconds, width, height, elapsed_seconds}``
           stderr: one JSON event per line (``start`` / ``progress``)
           exit 0 = rendered; non-zero = failed.
  inspect  ``fframes-worker inspect --json``
           stdout: ``{checked_frames, findings[]}``; exit 2 when problems exist.
  strip    ``fframes-worker strip <range> -n N --columns C --width W -o <png>``
           stdout: ``{output, frames[], diagnostics[]}`` — a labelled contact
           sheet of evenly spaced frames; diagnostics carry layout problems
           (clipped text, missing fonts) found while sampling.
  frame    ``fframes-worker frame <at>... -o <dir>``
           stdout: one JSON list ``[{spec, path, frame, seconds, scenes,
           diagnostics[]}, ...]`` — one PNG per time spec into the directory.

This module turns that into plain dicts, forwards frame progress to the Fabric,
enforces a hard timeout and kills the child on cancellation. A worker exit of 0
is never trusted on its own: every output file must exist and be non-empty, or
the run is reported failed (no false success states).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import deque
from pathlib import Path
from typing import Any, Callable, Optional

from .config import VideoConfig

log = logging.getLogger("harness.video.runner")

# The render child never shows a console window (Windows only flag).
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_MAX_LOG_LINES = 80

ProgressCallback = Callable[..., None]


class VideoRunner:
    def __init__(self, config: VideoConfig) -> None:
        self._config = config

    # ── environment ──────────────────────────────────────────────────────────

    def worker_argv(self) -> list[str]:
        return [str(self._config.resolve_worker_bin())]

    def environment_report(self) -> dict[str, Any]:
        """Honest readiness: binary present, FFmpeg DLL tree configured."""
        binary = self._config.resolve_worker_bin()
        ffdir = self._config.resolve_ffmpeg_dir()
        problems: list[str] = []
        warnings: list[str] = []
        binary_ok = binary.is_file()
        if not binary_ok:
            problems.append(
                f"the fframes worker binary is not built at {binary} "
                "(run `cargo build --release` in rust/fframes-worker)"
            )
        ffmpeg: dict[str, Any] = {
            "configured": ffdir is not None,
            "dir": str(ffdir) if ffdir is not None else None,
            "available": None,
        }
        if ffdir is not None:
            if not ffdir.is_dir():
                problems.append(f"FFMPEG_DIR points at a missing directory: {ffdir}")
            else:
                has_dlls = any(ffdir.glob("bin/avcodec*.dll"))
                ffmpeg["available"] = has_dlls
                if not has_dlls:
                    warnings.append(
                        f"no avcodec DLLs found under {ffdir} — the render process may fail to start"
                    )
        else:
            warnings.append(
                "FFMPEG_DIR is not set; the worker needs the FFmpeg DLLs reachable on PATH to run"
            )
        return {
            "ready": binary_ok,
            "workerBinary": {"path": str(binary), "exists": binary_ok},
            "ffmpeg": ffmpeg,
            "problems": problems,
            "warnings": warnings,
        }

    def _child_env(self) -> dict[str, str]:
        env = dict(os.environ)
        ffdir = self._config.resolve_ffmpeg_dir()
        if ffdir is not None:
            env["PATH"] = str(ffdir / "bin") + os.pathsep + env.get("PATH", "")
            env.setdefault("FFMPEG_DIR", str(ffdir))
        return env

    # ── public operations ────────────────────────────────────────────────────

    async def render(
        self,
        *,
        out_path: Path,
        draft: bool,
        timeout_ms: int,
        cancel_event: Optional[asyncio.Event] = None,
        progress: Optional[ProgressCallback] = None,
        progress_scale: float = 1.0,
        cwd: Optional[Path] = None,
        project_path: Optional[Path] = None,
    ) -> dict[str, Any]:
        workdir = Path(cwd) if cwd is not None else out_path.parent
        workdir.mkdir(parents=True, exist_ok=True)
        argv = self.worker_argv()
        if project_path is not None:
            argv += ["--project", str(project_path)]
        argv += ["render", "--json"]
        if draft:
            argv += ["--draft"]
        argv += ["-o", str(out_path)]

        def on_event(event: dict[str, Any]) -> None:
            if progress is None:
                return
            if event.get("event") == "start":
                progress(0.0, f"rendering {int(event.get('frames') or 0)} frames")
            elif event.get("event") == "progress":
                total = int(event.get("total") or 0)
                done = int(event.get("done") or 0)
                if total > 0:
                    progress(
                        min(1.0, progress_scale * (done / total)),
                        f"frame {done}/{total}",
                    )

        run = await self._execute(
            argv, cwd=workdir, timeout_ms=timeout_ms, cancel_event=cancel_event, on_event=on_event
        )
        result = self._render_result(run, out_path)
        # Playback hardening: move the MP4 index to the front so a player can
        # start and seek from the first bytes. Best-effort by contract — it is
        # reported, never allowed to turn a real render into a failure.
        if result.get("ok") and not draft:
            from .transcode import ensure_faststart  # noqa: PLC0415

            output = Path(str(result.get("outputPath") or out_path))
            result["faststart"] = await asyncio.to_thread(
                ensure_faststart, output, self._config.resolve_ffmpeg_dir()
            )
            if output.is_file():
                result["sizeBytes"] = output.stat().st_size
        return result

    async def inspect(
        self,
        *,
        timeout_ms: int,
        cancel_event: Optional[asyncio.Event] = None,
        cwd: Optional[Path] = None,
        project_path: Optional[Path] = None,
    ) -> dict[str, Any]:
        workdir = Path(cwd) if cwd is not None else Path.cwd()
        argv = self.worker_argv()
        if project_path is not None:
            argv += ["--project", str(project_path)]
        argv += ["inspect", "--json"]
        run = await self._execute(
            argv, cwd=workdir, timeout_ms=timeout_ms, cancel_event=cancel_event
        )
        if "spawnError" in run:
            return {
                "ok": False, "status": "failed",
                "error": f"could not start the video worker: {run['spawnError']}",
                "errorClass": "WORKER_UNAVAILABLE",
            }
        if run["timedOut"]:
            return {
                "ok": False, "status": "timed_out",
                "error": f"inspection exceeded {timeout_ms}ms", "errorClass": "TIMEOUT",
                "logTail": run["logTail"],
            }
        if run["cancelled"]:
            return {
                "ok": False, "status": "cancelled", "error": "cancelled by operator",
                "errorClass": "CANCELLED", "logTail": run["logTail"],
            }
        doc, unparsed = self._parse_json_doc(run["stdout"])
        if doc is None:
            tail = f"; tail: {unparsed}" if unparsed else ""
            return {
                "ok": False, "status": "failed",
                "error": f"the inspector returned no JSON document (exit {run['rc']}){tail}",
                "errorClass": "INSPECTION_FAILED", "logTail": run["logTail"],
            }
        findings = list(doc.get("findings") or [])
        ok = run["rc"] == 0 and not findings
        return {
            "ok": ok,
            "status": "inspected" if ok else "inspection_failed",
            "checkedFrames": int(doc.get("checked_frames") or 0),
            "findings": findings,
            "workerExitCode": run["rc"],
            "logTail": run["logTail"],
        }

    async def strip(
        self,
        *,
        out_path: Path,
        count: int = 8,
        columns: int = 4,
        width: int = 480,
        range_spec: str = "all",
        timeout_ms: int,
        cancel_event: Optional[asyncio.Event] = None,
        cwd: Optional[Path] = None,
        project_path: Optional[Path] = None,
    ) -> dict[str, Any]:
        """Render a labelled contact sheet of frames to a PNG (no video file)."""
        workdir = Path(cwd) if cwd is not None else out_path.parent
        workdir.mkdir(parents=True, exist_ok=True)
        argv = self.worker_argv()
        if project_path is not None:
            argv += ["--project", str(project_path)]
        argv += [
            "strip", "--json", str(range_spec), "-n", str(int(count)),
            "--columns", str(int(columns)), "--width", str(int(width)),
            "-o", str(out_path),
        ]
        run = await self._execute(
            argv, cwd=workdir, timeout_ms=timeout_ms, cancel_event=cancel_event
        )
        return self._sheet_result(run, out_path)

    async def frames(
        self,
        *,
        at_specs: list[str],
        out_dir: Path,
        timeout_ms: int,
        cancel_event: Optional[asyncio.Event] = None,
        cwd: Optional[Path] = None,
        project_path: Optional[Path] = None,
    ) -> dict[str, Any]:
        """Render one PNG per time spec into ``out_dir``."""
        workdir = Path(cwd) if cwd is not None else out_dir.parent
        workdir.mkdir(parents=True, exist_ok=True)
        argv = self.worker_argv()
        if project_path is not None:
            argv += ["--project", str(project_path)]
        if not at_specs:
            return {
                "ok": False, "status": "failed",
                "error": "no frames were requested", "errorClass": "INVALID_INPUT",
            }
        argv += ["frame", "--json", *[str(spec) for spec in at_specs], "-o", str(out_dir)]
        run = await self._execute(
            argv, cwd=workdir, timeout_ms=timeout_ms, cancel_event=cancel_event
        )
        return self._frames_result(run, out_dir)

    # ── process plumbing ─────────────────────────────────────────────────────

    async def _execute(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout_ms: int,
        cancel_event: Optional[asyncio.Event] = None,
        on_event: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> dict[str, Any]:
        log_tail: deque[str] = deque(maxlen=_MAX_LOG_LINES)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                env=self._child_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=_CREATE_NO_WINDOW,
            )
        except Exception as exc:  # noqa: BLE001 - a spawn failure is an environment problem
            return {"spawnError": str(exc), "logTail": list(log_tail)}

        watcher: Optional[asyncio.Task] = None
        if cancel_event is not None:
            watcher = asyncio.create_task(self._kill_on_cancel(cancel_event, proc))
        stdout_task = asyncio.create_task(proc.stdout.read())
        stderr_task = asyncio.create_task(self._pump_events(proc.stderr, on_event, log_tail))
        timed_out = False
        try:
            try:
                await asyncio.wait_for(proc.wait(), timeout=max(1.0, timeout_ms / 1000.0))
            except asyncio.TimeoutError:
                timed_out = True
                await self._stop(proc)
        except asyncio.CancelledError:
            await self._stop(proc)
            await self._drain(stdout_task, stderr_task)
            raise
        finally:
            if watcher is not None:
                watcher.cancel()
        raw = await stdout_task
        await stderr_task
        return {
            "rc": proc.returncode,
            "timedOut": timed_out,
            "cancelled": bool(cancel_event is not None and cancel_event.is_set()),
            "stdout": raw,
            "logTail": list(log_tail),
        }

    async def _stop(self, proc: Any) -> None:
        if proc.returncode is not None:
            return
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            await proc.wait()

    async def _kill_on_cancel(self, cancel_event: asyncio.Event, proc: Any) -> None:
        try:
            await cancel_event.wait()
        except asyncio.CancelledError:
            raise
        await self._stop(proc)

    async def _drain(self, *tasks: asyncio.Task) -> None:
        for task in tasks:
            try:
                await task
            except Exception:  # noqa: BLE001 - draining must never mask the cancellation
                pass

    async def _pump_events(
        self,
        stream: Any,
        on_event: Optional[Callable[[dict[str, Any]], None]],
        log_tail: deque[str],
    ) -> None:
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            event: Any = None
            if text.startswith("{"):
                try:
                    event = json.loads(text)
                except json.JSONDecodeError:
                    event = None
            if isinstance(event, dict):
                if on_event is not None:
                    try:
                        on_event(event)
                    except Exception:  # noqa: BLE001 - progress must never break the run
                        pass
            else:
                log_tail.append(text)

    # ── result shaping ───────────────────────────────────────────────────────

    def _render_result(self, run: dict[str, Any], out_path: Path) -> dict[str, Any]:
        if "spawnError" in run:
            return {
                "ok": False, "status": "failed",
                "error": f"could not start the video worker: {run['spawnError']}",
                "errorClass": "WORKER_UNAVAILABLE",
            }
        if run["timedOut"]:
            return {
                "ok": False, "status": "timed_out",
                "error": "the render exceeded its time budget", "errorClass": "TIMEOUT",
                "logTail": run["logTail"],
            }
        if run["cancelled"]:
            return {
                "ok": False, "status": "cancelled", "error": "cancelled by operator",
                "errorClass": "CANCELLED", "logTail": run["logTail"],
            }
        doc, unparsed = self._parse_json_doc(run["stdout"])
        if run["rc"] != 0 or doc is None:
            detail = f"exit code {run['rc']}" if run["rc"] != 0 else "no JSON document on stdout"
            if unparsed:
                detail += f"; tail: {unparsed}"
            return {
                "ok": False, "status": "failed",
                "error": f"the video worker failed ({detail})",
                "errorClass": "RENDER_FAILED", "logTail": run["logTail"], "workerJson": doc,
            }
        produced = Path(str(doc.get("output") or out_path))
        if not produced.is_file() or produced.stat().st_size <= 0:
            return {
                "ok": False, "status": "failed",
                "error": f"the worker reported success but no output exists at {produced}",
                "errorClass": "RENDER_OUTPUT_MISSING", "logTail": run["logTail"],
                "workerJson": doc,
            }
        frames = doc.get("frames") or {}
        return {
            "ok": True, "status": "rendered", "outputPath": str(produced),
            "width": doc.get("width"), "height": doc.get("height"),
            "frames": {"start": frames.get("start"), "end": frames.get("end")},
            "startSeconds": doc.get("start_seconds"), "endSeconds": doc.get("end_seconds"),
            "elapsedSeconds": doc.get("elapsed_seconds"),
            "sizeBytes": produced.stat().st_size, "logTail": run["logTail"],
        }

    def _sheet_result(self, run: dict[str, Any], out_path: Path) -> dict[str, Any]:
        failure = self._preview_failure(run, "the contact sheet")
        if failure is not None:
            return failure
        doc, unparsed = self._parse_json_doc(run["stdout"])
        if run["rc"] != 0 or doc is None:
            detail = f"exit code {run['rc']}" if run["rc"] != 0 else "no JSON document on stdout"
            if unparsed:
                detail += f"; tail: {unparsed}"
            return {
                "ok": False, "status": "failed",
                "error": f"the preview worker failed ({detail})",
                "errorClass": "PREVIEW_FAILED", "logTail": run["logTail"], "workerJson": doc,
            }
        produced = Path(str(doc.get("output") or out_path))
        if not produced.is_file() or produced.stat().st_size <= 0:
            return {
                "ok": False, "status": "failed",
                "error": f"the worker reported a contact sheet but no file exists at {produced}",
                "errorClass": "PREVIEW_OUTPUT_MISSING", "logTail": run["logTail"],
                "workerJson": doc,
            }
        return {
            "ok": True, "status": "rendered", "outputPath": str(produced),
            "frames": [int(frame) for frame in (doc.get("frames") or [])],
            "diagnostics": [
                report for report in (doc.get("diagnostics") or []) if isinstance(report, dict)
            ],
            "sizeBytes": produced.stat().st_size, "logTail": run["logTail"],
        }

    def _frames_result(self, run: dict[str, Any], out_dir: Path) -> dict[str, Any]:
        failure = self._preview_failure(run, "the preview frames")
        if failure is not None:
            return failure
        doc, unparsed = self._parse_json_value(run["stdout"])
        if run["rc"] != 0 or not isinstance(doc, list):
            detail = f"exit code {run['rc']}" if run["rc"] != 0 else "no JSON list on stdout"
            if unparsed:
                detail += f"; tail: {unparsed}"
            return {
                "ok": False, "status": "failed",
                "error": f"the preview worker failed ({detail})",
                "errorClass": "PREVIEW_FAILED", "logTail": run["logTail"],
            }
        frames: list[dict[str, Any]] = []
        for item in doc:
            if not isinstance(item, dict):
                continue
            produced = Path(str(item.get("path") or ""))
            if not produced.is_file() or produced.stat().st_size <= 0:
                return {
                    "ok": False, "status": "failed",
                    "error": f"the worker reported a frame but no file exists at {produced}",
                    "errorClass": "PREVIEW_OUTPUT_MISSING", "logTail": run["logTail"],
                    "workerJson": doc,
                }
            frames.append({
                "spec": str(item.get("spec") or ""),
                "path": str(produced),
                "frame": item.get("frame"),
                "seconds": item.get("seconds"),
                "scenes": list(item.get("scenes") or []),
                "diagnostics": [
                    report for report in (item.get("diagnostics") or []) if isinstance(report, dict)
                ],
                "sizeBytes": produced.stat().st_size,
            })
        if not frames:
            return {
                "ok": False, "status": "failed",
                "error": f"the worker returned no frames for {out_dir}",
                "errorClass": "PREVIEW_OUTPUT_MISSING", "logTail": run["logTail"],
            }
        return {"ok": True, "status": "rendered", "frames": frames, "logTail": run["logTail"]}

    def _preview_failure(self, run: dict[str, Any], what: str) -> Optional[dict[str, Any]]:
        """The shared spawn/timeout/cancel/exit failures of a preview command."""
        if "spawnError" in run:
            return {
                "ok": False, "status": "failed",
                "error": f"could not start the video worker: {run['spawnError']}",
                "errorClass": "WORKER_UNAVAILABLE",
            }
        if run["timedOut"]:
            return {
                "ok": False, "status": "timed_out",
                "error": f"rendering {what} exceeded its time budget", "errorClass": "TIMEOUT",
                "logTail": run["logTail"],
            }
        if run["cancelled"]:
            return {
                "ok": False, "status": "cancelled", "error": "cancelled by operator",
                "errorClass": "CANCELLED", "logTail": run["logTail"],
            }
        return None

    def _parse_json_doc(self, raw: bytes) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        value, tail = self._parse_json_value(raw)
        if isinstance(value, list):
            return None, tail or (raw or b"").decode("utf-8", errors="replace")[-500:]
        return value, tail

    def _parse_json_value(self, raw: bytes) -> tuple[Any, Optional[str]]:
        text = (raw or b"").decode("utf-8", errors="replace").strip()
        if not text:
            return None, None
        try:
            return json.loads(text), None
        except json.JSONDecodeError:
            return None, text[-500:]


__all__ = ["VideoRunner"]
