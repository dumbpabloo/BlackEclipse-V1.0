"""Poster-frame extraction for video artifact thumbnails (Phase 7).

A video artifact's thumbnail is a REAL frame of the rendered MP4, extracted
with the SAME FFmpeg tree the renderer links against (the BtbN build carries
`ffmpeg.exe` next to the DLLs). No frame, no thumbnail: when the tree is absent
or the object cannot be read, the preview stays `UNSUPPORTED` — this module
never fabricates a poster and never pulls a frame from a different video.

Registered on the shared preview engine by `video/bootstrap.py`
(`set_video_poster_extractor`), so the artifacts layer holds no video import.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("harness.video.poster")

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_TIMEOUT_S = 30
#: Fallback sample points, tried in order when the duration is unknown. 1.5 s
#: clears the opening fade-in (scenes animate in over ~0.2-0.4 s); 0.5 s still
#: yields a real frame for a minimal one-scene render. A frame is never
#: fabricated: when no candidate produces one the preview stays UNSUPPORTED.
_POSTER_TIMES: tuple[float, ...] = (1.5, 0.5)
#: Where the poster is taken from when the duration IS known: just over a third
#: in, past the intro and before the outro — a representative content frame.
_POSTER_AT_FRACTION = 0.35
_POSTER_WIDTH = 512


def extract_video_poster(storage: Any, artifact: Any) -> Optional[bytes]:
    """PNG bytes of a representative frame of a video artifact, or None.

    Never raises: a missing FFmpeg tree, an unreadable object or a failed
    extraction all return None (the preview then reports UNSUPPORTED).
    """
    try:
        key = getattr(artifact, "storage_key", None)
        mime = str(getattr(artifact, "mime_type", "") or "").lower()
        if not key or not mime.startswith("video/"):
            return None
        ffmpeg = _ffmpeg_binary()
        if ffmpeg is None:
            return None
        with storage.open(key) as handle:
            source = getattr(handle, "name", None)
            if not source:
                return None
            with tempfile.TemporaryDirectory(prefix="ve-poster-") as tmp:
                for seconds in _candidate_times(ffmpeg, str(source)):
                    poster = _extract_at(ffmpeg, str(source), Path(tmp), seconds)
                    if poster is not None:
                        return poster
                return None
    except Exception:  # noqa: BLE001 - an absent poster is an honest None
        return None


def _candidate_times(ffmpeg: Path, source: str) -> tuple[float, ...]:
    """Duration-relative first, then the fixed fallbacks (deduped, in order).

    A 40 s recap would otherwise thumbnail its title card; sampling just over a
    third in shows actual content. Unknown duration (no ffprobe / unreadable
    file) keeps the proven fixed points.
    """
    duration = _probe_duration(ffmpeg, source)
    if duration is None or duration <= 0.2:
        return _POSTER_TIMES
    preferred = round(duration * _POSTER_AT_FRACTION, 2)
    preferred = min(max(preferred, 0.2), max(0.2, duration - 0.1))
    candidates = [preferred, *_POSTER_TIMES]
    seen: set[float] = set()
    ordered: list[float] = []
    for value in candidates:
        if value >= duration:
            continue
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return tuple(ordered) or (0.0,)


def _probe_duration(ffmpeg: Path, source: str) -> Optional[float]:
    """The container duration in seconds, or None when ffprobe cannot say."""
    name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    probe = ffmpeg.with_name(name)
    if not probe.is_file():
        return None
    try:
        completed = subprocess.run(
            [
                str(probe), "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                source,
            ],
            capture_output=True,
            timeout=_TIMEOUT_S,
            creationflags=_CREATE_NO_WINDOW,
        )
        if completed.returncode != 0:
            return None
        text = (completed.stdout or b"").decode("utf-8", errors="replace").strip()
        value = float(text)
        return value if value > 0 else None
    except Exception:  # noqa: BLE001 - the fixed fallbacks still work
        return None


def _extract_at(ffmpeg: Path, source: str, tmp: Path, seconds: float) -> Optional[bytes]:
    target = tmp / "poster.png"
    if target.exists():
        target.unlink()
    completed = subprocess.run(
        [
            str(ffmpeg), "-hide_banner", "-loglevel", "error",
            "-ss", f"{seconds}", "-i", source,
            "-frames:v", "1",
            "-vf", f"scale={_POSTER_WIDTH}:-2",
            "-y", str(target),
        ],
        capture_output=True,
        timeout=_TIMEOUT_S,
        creationflags=_CREATE_NO_WINDOW,
    )
    if completed.returncode != 0 or not target.is_file() or target.stat().st_size <= 0:
        return None
    return target.read_bytes()


def _ffmpeg_binary() -> Optional[Path]:
    """`ffmpeg.exe` under the configured FFmpeg tree, or None when absent."""
    try:
        from .config import get_video_config  # noqa: PLC0415

        ffdir = get_video_config().resolve_ffmpeg_dir()
        if ffdir is None:
            return None
        name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
        candidate = ffdir / "bin" / name
        return candidate if candidate.is_file() else None
    except Exception:  # noqa: BLE001
        return None


__all__ = ["extract_video_poster"]
