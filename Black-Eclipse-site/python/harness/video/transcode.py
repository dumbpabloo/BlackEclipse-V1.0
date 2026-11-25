"""Playback hardening for a rendered MP4 (post-render, best-effort).

`fframes` writes the MP4 through FFmpeg's default muxer, which leaves the `moov`
atom at the END of the file. A player can still open such a file, but only if it
can seek to the tail — and a media element that falls back to a plain (non-range)
response cannot. Remuxing with `-movflags +faststart` moves the index to the
front, so the first bytes are enough to start playback and seek.

Rules:
  - this NEVER decides whether a render succeeded; a missing FFmpeg binary or a
    failed remux keeps the original file and is reported as skipped;
  - the stream is copied (`-c copy`), never re-encoded: bytes and quality are
    unchanged;
  - the remux is atomic (write a sibling temp file, then replace the original).
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("harness.video.transcode")

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_TIMEOUT_S = 120


def ffmpeg_binary(ffmpeg_dir: Optional[Path]) -> Optional[Path]:
    """`ffmpeg` under the configured tree, or None when it is not installed."""
    if ffmpeg_dir is None:
        return None
    name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    candidate = Path(ffmpeg_dir) / "bin" / name
    if candidate.is_file():
        return candidate
    fallback = Path(ffmpeg_dir) / name
    return fallback if fallback.is_file() else None


def ensure_faststart(path: Path, ffmpeg_dir: Optional[Path]) -> dict[str, Any]:
    """Remux `path` to a faststart MP4 in place, best-effort.

    Returns `{"applied": bool, "reason": str | None}` and never raises.
    """
    target = Path(path)
    if not target.is_file() or target.stat().st_size <= 0:
        return {"applied": False, "reason": "RENDER_OUTPUT_MISSING"}
    ffmpeg = ffmpeg_binary(ffmpeg_dir)
    if ffmpeg is None:
        return {"applied": False, "reason": "FFMPEG_UNAVAILABLE"}
    temp = target.with_name(f"{target.stem}.faststart{target.suffix}")
    try:
        if temp.exists():
            temp.unlink()
        completed = subprocess.run(
            [
                str(ffmpeg), "-hide_banner", "-loglevel", "error",
                "-y", "-i", str(target),
                "-map", "0", "-c", "copy",
                "-movflags", "+faststart",
                str(temp),
            ],
            capture_output=True,
            timeout=_TIMEOUT_S,
            creationflags=_CREATE_NO_WINDOW,
        )
        if completed.returncode != 0 or not temp.is_file() or temp.stat().st_size <= 0:
            detail = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
            return {"applied": False, "reason": detail[:400] or "FFMPEG_REMUX_FAILED"}
        os.replace(temp, target)
        return {"applied": True, "reason": None}
    except Exception as exc:  # noqa: BLE001 - playback hardening must never fail a render
        log.warning("faststart remux skipped: %s", exc)
        return {"applied": False, "reason": str(exc)[:400]}
    finally:
        try:
            if temp.exists():
                temp.unlink()
        except OSError:
            pass


__all__ = ["ensure_faststart", "ffmpeg_binary"]
