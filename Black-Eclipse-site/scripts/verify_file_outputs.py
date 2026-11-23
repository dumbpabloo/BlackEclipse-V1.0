"""End-to-end proof of the file-location fix, run against the real harness code.

Run from `Black-Eclipse-site` with the vendored interpreter:

    scripts/vendor/python/python.exe scripts/verify_file_outputs.py

It exercises exactly what the app does: the shared ToolRegistry handler for
`eclipse_report` (with the real ArtifactService), then prints the absolute path
the agent receives and checks the file exists. It also prints the folder the
running app will use by default and the folders `file_locate` searches, so the
answer is checkable against this machine, not a fixture.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from harness.artifacts import ArtifactService, ArtifactStore, LocalArtifactStorage  # noqa: E402
from harness.documents import outputs  # noqa: E402
from harness.documents.tools import register_document_authoring_tools  # noqa: E402
from harness.persistence import AgentMemoryStore  # noqa: E402
from harness.tools import ToolContext, ToolRegistry  # noqa: E402


def main() -> int:
    print("== default folders on this machine ==")
    print("exports_dir :", outputs.exports_dir())
    for root in outputs.search_roots():
        print("search root :", root)

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        store = AgentMemoryStore(str(base / "agent-memory.db"))
        registry = ToolRegistry()
        register_document_authoring_tools(registry)
        service = ArtifactService(ArtifactStore(store), LocalArtifactStorage(base / "artifacts"))
        ctx = ToolContext(
            run_id="verify-file-outputs", log=lambda *a, **k: None,
            memory_store=store, artifact_service=service, agent_id="eclipse-agent",
        )

        print("\n== eclipse_report (the tool the agent calls for a PDF) ==")
        tool = registry.get("eclipse_report")
        text, data = asyncio.run(tool.handler({
            "title": "Eclipse file-location check",
            "format": "pdf",
            "blocks": [
                {"type": "heading", "text": "Where this file lives", "level": 1},
                {"type": "paragraph", "text": "Produced by the verification script."},
            ],
        }, ctx))
        print(text)
        path = data.get("path")
        print("result.path :", path)
        print("absolute    :", bool(path) and os.path.isabs(path))
        print("exists      :", bool(path) and os.path.isfile(path))
        print("header      :", Path(path).read_bytes()[:8] if path else b"")

        print("\n== file_locate finds it again (name only) ==")
        locate = registry.get("file_locate")
        found_text, found = asyncio.run(locate.handler({"query": "file-location", "extensions": ["pdf"]}, ctx))
        print(found_text)
        print("found path  :", found["files"][0]["path"] if found["files"] else None)

        ok = (
            bool(path) and os.path.isabs(path) and os.path.isfile(path)
            and Path(path).read_bytes().startswith(b"%PDF-")
            and bool(found["files"]) and found["files"][0]["path"] == path
            and str(path) in text and str(path) in found_text
        )
        store.close()
        print("\nRESULT:", "PASS" if ok else "FAIL")
        if ok:
            # The proof ran against the REAL export folder; do not leave the
            # probe behind in the person's Documents.
            Path(path).unlink(missing_ok=True)
            print("cleaned up the probe file (the evidence above is the run).")
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
