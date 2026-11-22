"""The ONE edit tool (Round 3 / W3, ADR-H282).

**Why this module exists.** The harness had THREE ways to change a file's
contents and ELEVEN more file-lifecycle tools, spread over two subsystems:
`code/tools.py`'s `workspace_patch_apply` (a five-op patch engine), the fs
subsystem's `patch_file` / `replace_text` / `insert_text` / `delete_text` /
`apply_diff` / `multi_file_patch` / `edit_file`, and the natural-language
`edit_file` intent parser. Three of them were the SAME operation expressed
three ways, and none of them required proof that the model had seen the file
it was about to change: `expectedHash` was accepted everywhere and required
NOWHERE, so the model could write to a file it had never read.

This module owns the ONE text-edit tool, `code_edit`, and the two rules that
make it safe:

1. **A file may only be changed if THIS RUN read it.** The per-run read ledger
   (`harness.tools.read_ledger`) records every file a read tool returned a
   hash for. An edit to a file the run never read is REFUSED with
   `FILE_NOT_READ`, and the refusal names the read to make. The good news:
   `edit_file`'s inspect packet already did this for ONE tool, and the
   registry now does it for EVERY read tool that returns a hash.

2. **A file may only be changed if it has not changed since it was read.** The
   ledger's hash is passed to the edit engine as `expected_hash`, so the
   engine's own `STALE_FILE` refusal fires - the same refusal, the same code,
   the same recovery hint, in one place.

**What it is not.** `code_edit` REPLACES text. It does not create a file
(`write_file`), delete one (`delete_file`), move one (`move_file`), insert
without a replace target (`insert_text`), delete a line range
(`delete_text`), or apply a unified diff (`apply_diff`). Those are genuinely
different operations and they stay. What goes is the DUPLICATION: the four
text-edit mutators this tool subsumes (`replace_text`, `patch_file`,
`edit_file`, `multi_file_patch`) are deprecated in its favour, and
`workspace_patch_apply` folds into it.

**Path recovery.** A model that guesses a path wrong used to get a bare
"that file isn't there". This module turns that into a QUESTION with an
answer: up to five nearest candidates, found by exact basename, then suffix,
then a bounded fuzzy match. The candidates are PATHS - never contents.
"""

from __future__ import annotations

import difflib
from typing import Any, Optional

from ..tools import ToolContext, ToolDefinition, ToolRegistry, read_ledger
from .fs import errors as fs_err

#: The tool's name. The ONE text-edit tool.
TOOL_NAME = "code_edit"

#: How many nearest paths a refusal may offer.
MAX_CANDIDATES = 5

#: How many files the path search may look at.
MAX_CANDIDATE_SCAN = 5_000


def _normalise(path: str) -> str:
    """A workspace-relative, POSIX-style path - the ledger's key format."""
    text = str(path or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.strip("/")


def nearest_paths(ws: Any, wanted: str, *, limit: int = MAX_CANDIDATES) -> list[str]:
    """Up to `limit` paths in the workspace that look like `wanted`.

    Three passes, best first: an exact basename match, a suffix match (the
    wanted path ends with the candidate, or the other way round), and a bounded
    fuzzy match on the basename. A candidate is a PATH - never file content.
    """
    from .scan import iter_scan_files  # noqa: PLC0415

    target = _normalise(wanted)
    if not target:
        return []
    wanted_base = target.rsplit("/", 1)[-1]
    exact: list[str] = []
    suffix: list[str] = []
    fuzzy: list[str] = []
    seen = 0
    try:
        for rel, _full in iter_scan_files(ws, "."):
            seen += 1
            if seen > MAX_CANDIDATE_SCAN:
                break
            base = rel.rsplit("/", 1)[-1]
            if base == wanted_base:
                exact.append(rel)
            elif rel.endswith("/" + target) or target.endswith("/" + rel):
                suffix.append(rel)
            else:
                fuzzy.append(rel)
    except Exception:  # noqa: BLE001 - a broken walk means no candidates
        return []
    if exact:
        return exact[:limit]
    if suffix:
        return suffix[:limit]
    try:
        close = difflib.get_close_matches(wanted_base, [r.rsplit("/", 1)[-1] for r in fuzzy], n=limit, cutoff=0.6)
    except Exception:  # noqa: BLE001 - a fuzzy failure means no candidates
        return []
    return [r for r in fuzzy if r.rsplit("/", 1)[-1] in close][:limit]


def _not_read_error(rel: str, ws: Any) -> fs_err.FsError:
    """The refusal for a file this run never read."""
    candidates = nearest_paths(ws, rel) if ws is not None else []
    details: dict[str, Any] = {"path": rel, "readTool": "read_file"}
    if candidates:
        details["candidates"] = candidates
    return fs_err.FsError(
        fs_err.FILE_NOT_READ,
        "Eclipse has not read that file in this task, so it will not change it.",
        path=rel,
        details=details,
    )


async def code_edit(args: dict[str, Any], ctx: ToolContext) -> tuple[str, Any]:
    """Replace text in a file - the ONE text-edit tool.

    The file must have been READ by this run, and it must not have changed
    since. Both rules are enforced here, and both refusals name the way
    forward.
    """
    from .fs_tools import _write_service  # noqa: PLC0415

    model = _input(**args)
    rel = _normalise(model.path)
    if not rel:
        return 'REFUSED (PATH_NOT_FOUND): That file path is empty.', {
            "status": "refused", "code": fs_err.PATH_NOT_FOUND, "path": model.path,
        }

    ws = getattr(ctx, "workspace", None)
    ledger = read_ledger(ctx)
    known = ledger.get(rel)
    if known is None:
        # The run never read this file. Offer the nearest paths, and name the
        # read that would make the edit legal.
        error = _not_read_error(rel, ws)
        return f"REFUSED ({error.code}): {error.reason}", error.to_dict()

    try:
        service = _write_service(ctx)
    except fs_err.FsError as exc:
        return f"REFUSED ({exc.code}): {exc.reason}", exc.to_dict()

    try:
        result = service.replace_text(
            rel,
            model.oldText,
            model.newText,
            count=0 if model.replaceAll else 1,
            expected_occurrences=None if model.replaceAll else model.expectedOccurrences,
            expected_hash=known,
        )
    except fs_err.FsError as exc:
        # `STALE_FILE` (the file changed since the read) and `AMBIGUOUS_PATCH`
        # (the text appears more than once) are the two refusals that matter
        # here; both already carry a recovery hint.
        if exc.code == fs_err.PATH_NOT_FOUND and ws is not None:
            candidates = nearest_paths(ws, rel)
            if candidates:
                exc.details.setdefault("candidates", candidates)
        text = f"REFUSED ({exc.code}): {exc.reason}"
        hint = exc.details.get("hint") if isinstance(exc.details, dict) else None
        if hint:
            text += "\n\n" + str(hint)
        return text, exc.to_dict()

    changed = int(result.get("replaced") or 0)
    text = (
        f"Changed {rel}: replaced {changed} occurrence(s). "
        f"sha256 before {result.get('sha256Before', '?')}, after {result.get('sha256After', '?')}."
    )
    from .post_edit import render_diagnostics, render_format  # noqa: PLC0415

    for line in (render_format(result.get("format")), render_diagnostics(result.get("diagnostics"))):
        if line:
            text += "\n" + line
    payload = dict(result)
    payload["status"] = "ok"
    payload["path"] = rel
    payload["readVerified"] = True
    return text, payload


def _input(**args: Any) -> Any:
    """The tool's input model, built from the registry's generic arg model."""
    from pydantic import BaseModel, ConfigDict, Field  # noqa: PLC0415

    class CodeEditInput(BaseModel):
        model_config = ConfigDict(extra="forbid")

        path: str = Field(..., min_length=1, max_length=1024)
        oldText: str = Field(..., min_length=1, max_length=100_000)  # noqa: N815
        newText: str = Field(..., max_length=100_000)  # noqa: N815
        expectedOccurrences: Optional[int] = Field(None, ge=1, le=1000)  # noqa: N815
        replaceAll: bool = False  # noqa: N815

    return CodeEditInput(**{k: v for k, v in args.items() if v is not None})


_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "File to change, relative to the project root."},
        "oldText": {
            "type": "string",
            "description": "The exact text to replace. It must appear in the file, and "
                           "exactly once unless you say otherwise - add surrounding context "
                           "so the match is unambiguous.",
        },
        "newText": {"type": "string", "description": "What to put in its place."},
        "expectedOccurrences": {
            "type": "integer",
            "description": "How many times oldText should appear. Leave it out when the text "
                           "is unique; Eclipse refuses an ambiguous match rather than guessing.",
        },
        "replaceAll": {
            "type": "boolean",
            "description": "Replace every occurrence instead of requiring exactly one. "
                           "Use for a rename across the file; leave it out for a normal edit.",
        },
    },
    "required": ["path", "oldText", "newText"],
    "additionalProperties": False,
}

_DESCRIPTION = (
    "Replace text in a file - the ONE way to change a file's contents. Eclipse has to have "
    "READ the file earlier in this task, and the file must not have changed since; both "
    "refusals name the way forward. The text to replace must be unique unless you say how "
    "many times it appears (or pass replaceAll), so include enough surrounding context to "
    "make the match unambiguous. Matching tolerates curly quotes, non-ASCII dashes, "
    "non-breaking spaces and trailing whitespace, and the file's own line endings are "
    "preserved. On a miss the refusal includes the closest numbered lines. To create a file "
    "use write_file; to apply a prepared diff use apply_diff."
)


def register_edit_tools(registry: ToolRegistry) -> list[str]:
    """Register the ONE text-edit tool on the registry."""
    registry.register(ToolDefinition(
        name=TOOL_NAME,
        description=_DESCRIPTION,
        input_schema=_INPUT_SCHEMA,
        handler=code_edit,
        permission="write",
        risk="medium",
        timeout_ms=60_000,
        effect="write",
        version="1.0.0",
    ))
    return [TOOL_NAME]


__all__ = [
    "MAX_CANDIDATES",
    "TOOL_NAME",
    "code_edit",
    "nearest_paths",
    "register_edit_tools",
]
