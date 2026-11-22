"""Gap #11: read_file returns images/PDFs as real model media.

The old behaviour refused every binary with `BINARY_FILE`. A coding agent that
needs to look at a screenshot, a design mock or a PDF spec had no way to see it.
The contract:

1. a recognised image is returned as bounded media and becomes an `image_url`
   content part;
2. a PDF becomes a `file` part with its base64 data;
3. a binary that is NOT media still gets `BINARY_FILE`;
4. the size ceiling is enforced before any base64 work.
"""

from __future__ import annotations

import asyncio

import pytest

from harness.code import fs_tools, media
from harness.tools import ToolContext

import fs_helpers

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"x" * 64
PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n"


def _ctx(root):
    ws = fs_helpers.make_ws(root)
    return ToolContext(run_id="media-run", log=lambda *_: None, workspace=ws, autonomy="edit_verify")


def test_an_image_read_returns_media_and_becomes_image_content(tmp_path):
    root = fs_helpers.tree(tmp_path)
    (root / "shot.png").write_bytes(PNG)
    ctx = _ctx(root)

    text, payload = asyncio.run(fs_tools._read_file({"path": "shot.png"}, ctx))

    assert payload["status"] == "ok"
    info = payload["media"]
    assert info["kind"] == "image"
    assert info["mime"] == "image/png"
    assert info["bytes"] == len(PNG)
    assert info["dataUrl"].startswith("data:image/png;base64,")
    assert "Read image" in text

    parts = media.model_parts(info)
    assert parts == [{"type": "image_url", "image_url": {"url": info["dataUrl"]}}]
    message = media.model_message([info])
    assert message.content[0]["type"] == "text"
    assert message.content[1]["type"] == "image_url"


def test_a_pdf_read_returns_a_file_part(tmp_path):
    root = fs_helpers.tree(tmp_path)
    (root / "spec.pdf").write_bytes(PDF)
    ctx = _ctx(root)

    _text, payload = asyncio.run(fs_tools._read_file({"path": "spec.pdf"}, ctx))

    assert payload["media"]["mime"] == "application/pdf"
    parts = media.model_parts(payload["media"])
    assert parts[0]["type"] == "file"
    assert parts[0]["file"]["filename"] == "spec.pdf"
    assert parts[0]["file"]["file_data"].startswith("data:application/pdf;base64,")


def test_a_non_media_binary_still_refuses(tmp_path):
    root = fs_helpers.tree(tmp_path)
    (root / "blob.bin").write_bytes(b"\x00\x01\x02not media\x00")
    ctx = _ctx(root)

    _text, payload = asyncio.run(fs_tools._read_file({"path": "blob.bin"}, ctx))

    assert payload["status"] == "refused"
    assert payload.get("code") in ("BINARY_FILE", "UNSUPPORTED_BINARY_OPERATION")


def test_oversized_media_is_refused_before_encoding(tmp_path, monkeypatch):
    root = fs_helpers.tree(tmp_path)
    (root / "big.png").write_bytes(PNG + b"x" * 100)
    monkeypatch.setattr(media, "MAX_MEDIA_BYTES", 16)
    ctx = _ctx(root)

    text, payload = asyncio.run(fs_tools._read_file({"path": "big.png"}, ctx))

    assert payload["status"] == "refused"
    assert payload["code"] == "TOO_LARGE"
    assert "TOO_LARGE" in text


@pytest.mark.asyncio
async def test_the_tool_turn_attaches_media_to_the_model_messages():
    import test_parallel_tools as TP
    from harness.tool_turn import ToolTurn
    from harness.tools import ToolRegistry

    reg = ToolRegistry()

    async def read_an_image(_args, _ctx):
        info = media.media_payload("shot.png", PNG, "image/png", "image")
        return "Read image shot.png", {"status": "ok", "media": info}

    TP._add(reg, "read_media", read_an_image)
    result = await ToolTurn(TP._deps(reg)).run(TP._state(("read_media", {}, "c1")))

    humans = [m for m in result["messages"] if getattr(m, "type", "") == "human"]
    assert humans, "no media message was appended"
    content = humans[-1].content
    assert isinstance(content, list)
    assert any(part.get("type") == "image_url" for part in content)


def test_sniffing_ignores_the_extension():
    assert media.sniff_mime(PNG) == ("image/png", "image")
    assert media.sniff_mime(b"GIF89a....") == ("image/gif", "image")
    assert media.sniff_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == ("image/webp", "image")
    assert media.sniff_mime(PDF) == ("application/pdf", "pdf")
    assert media.sniff_mime(b"plain text") is None
