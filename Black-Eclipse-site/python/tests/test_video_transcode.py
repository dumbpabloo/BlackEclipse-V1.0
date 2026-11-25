"""Post-render playback hardening (`video/transcode.py`).

The remux is best-effort by contract: it must never turn a real render into a
failure, and it must copy (never re-encode) the stream. These tests pin the
outcome contract, the argv, and the atomic replace.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.video import transcode


def _ffmpeg_tree(tmp_path: Path) -> Path:
    tree = tmp_path / "ffmpeg"
    (tree / "bin").mkdir(parents=True)
    binary = tree / "bin" / ("ffmpeg.exe" if __import__("os").name == "nt" else "ffmpeg")
    binary.write_bytes(b"fake")
    return tree


def test_missing_output_is_reported_not_raised(tmp_path: Path) -> None:
    result = transcode.ensure_faststart(tmp_path / "nope.mp4", _ffmpeg_tree(tmp_path))
    assert result == {"applied": False, "reason": "RENDER_OUTPUT_MISSING"}


def test_no_ffmpeg_tree_keeps_the_original(tmp_path: Path) -> None:
    video = tmp_path / "final.mp4"
    video.write_bytes(b"original")
    result = transcode.ensure_faststart(video, None)
    assert result["applied"] is False
    assert result["reason"] == "FFMPEG_UNAVAILABLE"
    assert video.read_bytes() == b"original"


def test_successful_remux_replaces_atomically(tmp_path: Path, monkeypatch) -> None:
    tree = _ffmpeg_tree(tmp_path)
    video = tmp_path / "final.mp4"
    video.write_bytes(b"original")
    seen: list[list[str]] = []

    def fake_run(argv, **kwargs):  # noqa: ANN001 - mirrors subprocess.run
        seen.append(list(argv))
        Path(argv[-1]).write_bytes(b"faststart-bytes")
        return type("Done", (), {"returncode": 0, "stderr": b""})()

    monkeypatch.setattr(transcode.subprocess, "run", fake_run)
    result = transcode.ensure_faststart(video, tree)

    assert result == {"applied": True, "reason": None}
    assert video.read_bytes() == b"faststart-bytes"
    argv = seen[0]
    assert "-movflags" in argv and "+faststart" in argv
    assert "-c" in argv and "copy" in argv
    # No sibling temp file is left behind.
    assert not list(tmp_path.glob("*.faststart*"))


def test_failed_remux_keeps_the_original(tmp_path: Path, monkeypatch) -> None:
    tree = _ffmpeg_tree(tmp_path)
    video = tmp_path / "final.mp4"
    video.write_bytes(b"original")

    def fake_run(argv, **kwargs):  # noqa: ANN001
        return type("Done", (), {"returncode": 1, "stderr": b"boom"})()

    monkeypatch.setattr(transcode.subprocess, "run", fake_run)
    result = transcode.ensure_faststart(video, tree)

    assert result["applied"] is False
    assert "boom" in (result["reason"] or "")
    assert video.read_bytes() == b"original"


def test_render_runner_records_faststart_for_finals(monkeypatch, tmp_path: Path) -> None:
    """A final render reports the remux outcome without changing success."""
    from harness.video.config import VideoConfig
    from harness.video import runner as runner_module

    calls: list[Path] = []

    def fake_ensure(path, ffmpeg_dir):  # noqa: ANN001
        calls.append(Path(path))
        return {"applied": True, "reason": None}

    monkeypatch.setattr(transcode, "ensure_faststart", fake_ensure)
    video_runner = runner_module.VideoRunner(VideoConfig())
    output = tmp_path / "final.mp4"
    output.write_bytes(b"video")
    monkeypatch.setattr(
        video_runner,
        "_render_result",
        lambda run, out: {"ok": True, "status": "rendered", "outputPath": str(output)},
    )

    async def fake_execute(*args, **kwargs):  # noqa: ANN001
        return {"rc": 0, "timedOut": False, "cancelled": False, "stdout": b"", "logTail": []}

    monkeypatch.setattr(video_runner, "_execute", fake_execute)

    import asyncio

    result = asyncio.run(
        video_runner.render(out_path=output, draft=False, timeout_ms=1000)
    )
    assert result["ok"] is True
    assert result["faststart"] == {"applied": True, "reason": None}
    assert calls == [output]
