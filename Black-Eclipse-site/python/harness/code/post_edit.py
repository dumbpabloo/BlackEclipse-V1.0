"""Post-write diagnostics: what the file looks like after the change.

The loop this closes: edit -> saved -> obvious error survives -> the model
finds out only if it happens to run a check. After every successful write the
edit result now carries the diagnostics for THAT file, bounded and non-fatal.

Two sources, one seam:

  * structural (syntax) diagnostics run synchronously and always work;
  * LSP diagnostics run in a daemon thread with a short deadline. The thread
    uses the process-wide CIUE runtime only when one already exists (the
    service builds it at boot); it never CREATES an index or an LSP manager on
    the write path. A server start that exceeds the deadline is abandoned to
    that thread and the result says `lsp: "timeout"` - the edit has already
    succeeded and is never held hostage by a language server.

The result is data, not a verdict: `engine` names what answered, and
"no diagnostics" is distinct from "diagnostics unavailable".
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Callable, Optional

#: How long the LSP half may take before the structural half is returned alone.
DIAGNOSTIC_TIMEOUT_S = 2.0

#: How many diagnostics a tool result carries. The rest are counted, not dropped
#: silently.
MAX_DIAGNOSTICS = 20

_ERROR_RANK = {"error": 0, "warning": 1, "information": 2, "info": 2, "hint": 3}


def _bounded(call: Callable[[], Any], timeout_s: float) -> tuple[bool, Any]:
    """Run `call` in a daemon thread. Returns `(finished, value)`. Never raises."""
    result: "queue.Queue[tuple[bool, Any]]" = queue.Queue(maxsize=1)

    def _run() -> None:
        try:
            result.put((True, call()))
        except Exception as exc:  # noqa: BLE001 - diagnostics are never fatal
            result.put((False, exc))

    thread = threading.Thread(target=_run, name="post-edit-diagnostics", daemon=True)
    thread.start()
    thread.join(timeout=max(0.1, float(timeout_s)))
    if thread.is_alive():
        return False, None
    try:
        return result.get_nowait()
    except Exception:  # noqa: BLE001
        return False, None


def _pack(
    items: list[dict[str, Any]], *, engine: str, lsp: str, file: str = "",
) -> dict[str, Any]:
    ordered = sorted(
        items,
        key=lambda d: (
            _ERROR_RANK.get(str(d.get("severity") or "").lower(), 4),
            int(d.get("line") or 0),
            int(d.get("column") or 0),
        ),
    )
    return {
        "status": "ok",
        "engine": engine,
        "lsp": lsp,
        "file": file,
        "count": len(ordered),
        "diagnostics": ordered[:MAX_DIAGNOSTICS],
        "truncated": max(0, len(ordered) - MAX_DIAGNOSTICS),
    }


def diagnostics_for(
    ws: Any, rel_path: str, text: Optional[str] = None,
    *, timeout_s: float = DIAGNOSTIC_TIMEOUT_S,
) -> dict[str, Any]:
    """Structural + (bounded, best-effort) LSP diagnostics for one file."""
    items: list[dict[str, Any]] = []
    try:
        from .intel.diagnostics import structural_diagnostics  # noqa: PLC0415

        if text is None:
            from .fs import encoding as enc  # noqa: PLC0415
            from .fs import resolver as res  # noqa: PLC0415

            text = enc.read_text(res.resolve(ws, rel_path)).text
        items.extend(d.to_dict() for d in structural_diagnostics(rel_path, text))
    except Exception:  # noqa: BLE001 - a checker failure is not a diagnostic
        pass

    try:
        from .intel.runtime import peek_code_intel_runtime  # noqa: PLC0415

        runtime = peek_code_intel_runtime()
    except Exception:  # noqa: BLE001
        runtime = None
    if runtime is None:
        return _pack(items, engine="structural", lsp="unavailable", file=rel_path)
    try:
        if hasattr(runtime, "is_ready_for") and not runtime.is_ready_for(ws):
            return _pack(items, engine="structural", lsp="not_indexed", file=rel_path)
    except Exception:  # noqa: BLE001 - a readiness probe is advisory
        pass

    def _lsp() -> list[dict[str, Any]]:
        manager = runtime.manager_for(ws)
        # The edit happened on disk, outside the LSP client: push the new text
        # first, or the server reports the pre-edit file.
        if hasattr(manager, "sync_document"):
            manager.sync_document(rel_path)
        payload = manager.get_diagnostics(file=rel_path)
        return list(payload.get("diagnostics") or [])

    finished, value = _bounded(_lsp, timeout_s)
    if not finished:
        return _pack(items, engine="structural", lsp="timeout", file=rel_path)
    if isinstance(value, Exception) or value is None:
        return _pack(items, engine="structural", lsp="failed", file=rel_path)
    # LSP is the richer source; keep its entries and any structural syntax error
    # the server did not report.
    seen = {
        (str(d.get("severity")), int(d.get("line") or 0), int(d.get("column") or 0), str(d.get("message") or ""))
        for d in value
    }
    for item in items:
        key = (str(item.get("severity")), int(item.get("line") or 0), int(item.get("column") or 0), str(item.get("message") or ""))
        if key not in seen:
            value.append(item)
            seen.add(key)
    return _pack(value, engine="lsp" if value else "structural", lsp="ok", file=rel_path)


def render_diagnostics(payload: Any, *, limit: int = 5) -> str:
    """A short, model-facing rendering of a diagnostics payload ("" when none)."""
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return ""
    items = [d for d in (payload.get("diagnostics") or []) if isinstance(d, dict)]
    if not items:
        if payload.get("lsp") == "ok":
            return "No diagnostics reported for this file."
        return ""
    lines = [f"Diagnostics ({payload.get('count', len(items))}):"]
    for item in items[:limit]:
        where = f"{payload.get('file') or item.get('file') or ''}:{item.get('line') or '?'}:{item.get('column') or 1}"
        message = str(item.get("message") or "").strip().replace("\n", " ")[:240]
        lines.append(f"- {str(item.get('severity') or 'error').upper()} {where} {message}")
    remaining = int(payload.get("count") or len(items)) - min(limit, len(items))
    if remaining > 0:
        lines.append(f"- ...and {remaining} more")
    return "\n".join(lines)


def render_format(payload: Any) -> str:
    """One line for a formatter result ("" when there is nothing to say)."""
    if not isinstance(payload, dict):
        return ""
    status = str(payload.get("status") or "")
    name = str(payload.get("formatter") or "formatter")
    if status == "ok":
        return f"Formatted with {name}." if payload.get("changed") else f"Checked with {name}; no change."
    if status == "failed":
        return f"{name} could not format the file: {str(payload.get('reason') or 'failed')[:200]}"
    return ""


__all__ = [
    "DIAGNOSTIC_TIMEOUT_S",
    "MAX_DIAGNOSTICS",
    "diagnostics_for",
    "render_diagnostics",
    "render_format",
]
