"""Video poster frames + the preview-engine seam (Phase 7): a video artifact's
thumbnail is a real frame from the registered extractor, and stays honestly
UNSUPPORTED without one."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.artifacts.preview import set_video_poster_extractor
from harness.artifacts.service import ArtifactService
from harness.artifacts.storage import LocalArtifactStorage
from harness.artifacts.store import ArtifactStore
from harness.persistence import AgentMemoryStore
from harness.video import config as video_config
from harness.video.config import VideoConfig
from harness.video.poster import extract_video_poster

_MP4_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"0" * 64
_POSTER = b"\x89PNG\r\n\x1a\nfake-poster"


@pytest.fixture()
def artifacts_env(tmp_path, monkeypatch):
    # The artifact importer confines source paths to an authorized workspace
    # root; the tmp dir is the test's root (same recipe as `video_fakes`).
    monkeypatch.setenv("BLACK_ECLIPSE_CODE_ROOT", str(tmp_path))
    aml = AgentMemoryStore(str(tmp_path / "agent-memory.db"))
    service = ArtifactService(
        ArtifactStore(aml), LocalArtifactStorage(root=tmp_path / "artifacts")
    )
    return {"service": service, "aml": aml}


def _register_video(env, tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(_MP4_BYTES)
    return env["service"].register_path(path, name="clip.mp4", owner_id="local")


def test_video_preview_is_unsupported_without_an_extractor(artifacts_env, tmp_path):
    artifact = _register_video(artifacts_env, tmp_path)
    assert artifact.category.value == "video"
    assert artifact.preview_kind == "video"
    assert artifact.preview_status.value == "UNSUPPORTED"
    assert artifact.thumbnail_key is None


def test_registered_poster_becomes_the_thumbnail(artifacts_env, tmp_path, monkeypatch):
    calls: list[tuple] = []

    def fake_poster(storage, artifact):
        calls.append((artifact.id, artifact.mime_type))
        return _POSTER

    monkeypatch.setattr("harness.artifacts.preview._video_poster", fake_poster, raising=False)
    artifact = _register_video(artifacts_env, tmp_path)
    assert calls and calls[0][1] == "video/mp4"
    assert artifact.preview_status.value == "READY"
    assert artifact.thumbnail_key and artifact.thumbnail_key.endswith(".thumb.png")
    found = artifacts_env["service"].open_thumbnail(artifact.id)
    assert found is not None
    handle, _ = found
    assert handle.read() == _POSTER


def test_a_failing_poster_is_unsupported_not_failed(artifacts_env, tmp_path, monkeypatch):
    def broken_poster(storage, artifact):
        raise RuntimeError("ffmpeg exploded")

    monkeypatch.setattr("harness.artifacts.preview._video_poster", broken_poster, raising=False)
    artifact = _register_video(artifacts_env, tmp_path)
    # The artifact is fine; only the preview could not be produced.
    assert artifact.status.value == "ACTIVE"
    assert artifact.preview_status.value == "UNSUPPORTED"


def test_extractor_returns_none_without_an_ffmpeg_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(video_config, "_default", VideoConfig(ffmpeg_dir=""))
    path = tmp_path / "clip.mp4"
    path.write_bytes(_MP4_BYTES)
    storage = LocalArtifactStorage(root=tmp_path / "artifacts")
    stored = storage.import_file(path, owner_id="local", name="clip.mp4")
    artifact = type("A", (), {"storage_key": stored.storage_key, "mime_type": "video/mp4"})()
    assert extract_video_poster(storage, artifact) is None


def test_extractor_ignores_non_video_mime():
    assert extract_video_poster(object(), type("A", (), {"storage_key": "k", "mime_type": "image/png"})()) is None


def test_the_seam_can_be_cleared():
    set_video_poster_extractor(None)
    from harness.artifacts import preview as engine

    assert engine._video_poster is None


def test_poster_samples_content_not_the_title_card(monkeypatch):
    """A known duration puts the poster just over a third in (past the intro)."""
    from harness.video import poster

    monkeypatch.setattr(poster, "_probe_duration", lambda ffmpeg, source: 40.0)
    times = poster._candidate_times(Path("ffmpeg"), "clip.mp4")
    assert times[0] == 14.0
    assert times[1:] == poster._POSTER_TIMES


def test_poster_falls_back_to_fixed_times_without_a_duration(monkeypatch):
    from harness.video import poster

    monkeypatch.setattr(poster, "_probe_duration", lambda ffmpeg, source: None)
    assert poster._candidate_times(Path("ffmpeg"), "clip.mp4") == poster._POSTER_TIMES


def test_poster_candidates_are_bounded_and_deduped(monkeypatch):
    from harness.video import poster

    # 4.2857 s * 0.35 rounds to 1.5 — the fixed fallback must not repeat it.
    monkeypatch.setattr(poster, "_probe_duration", lambda ffmpeg, source: 4.2857)
    times = poster._candidate_times(Path("ffmpeg"), "clip.mp4")
    assert times[0] == 1.5
    assert len(times) == len(set(times))

    # A tiny clip never samples at or past its end.
    monkeypatch.setattr(poster, "_probe_duration", lambda ffmpeg, source: 0.8)
    tiny = poster._candidate_times(Path("ffmpeg"), "clip.mp4")
    assert all(0 <= value < 0.8 for value in tiny)


def test_extractor_prefers_the_content_frame(monkeypatch, tmp_path):
    from harness.video import poster

    tried: list[float] = []

    def fake_extract(ffmpeg, source, tmp, seconds):
        tried.append(seconds)
        return b"png"

    monkeypatch.setattr(poster, "_ffmpeg_binary", lambda: Path("ffmpeg"))
    monkeypatch.setattr(poster, "_probe_duration", lambda ffmpeg, source: 30.0)
    monkeypatch.setattr(poster, "_extract_at", fake_extract)

    path = tmp_path / "clip.mp4"
    path.write_bytes(_MP4_BYTES)
    storage = LocalArtifactStorage(root=tmp_path / "artifacts")
    stored = storage.import_file(path, owner_id="local", name="clip.mp4")
    artifact = type("A", (), {"storage_key": stored.storage_key, "mime_type": "video/mp4"})()

    assert extract_video_poster(storage, artifact) == b"png"
    assert tried == [10.5]


def test_install_video_layer_registers_the_poster_extractor(tmp_path, monkeypatch):
    """Boot wiring: installing the video layer installs the preview seam."""
    from harness.artifacts import preview as engine
    from harness.tools import ToolRegistry
    from harness.video.bootstrap import install_video_layer
    from harness.video.service import set_video_service

    class FakeService:
        registry = ToolRegistry()

    monkeypatch.setenv("HARNESS_VIDEO_ROOT", str(tmp_path / "video"))
    engine.set_video_poster_extractor(None)
    install_video_layer(FakeService())
    try:
        assert engine._video_poster is not None
    finally:
        set_video_service(None)
