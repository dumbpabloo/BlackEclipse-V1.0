"""Path recovery for the filesystem read tools.

A model that guesses a path wrong used to get a bare "that file isn't there".
`code_edit` already answered with the nearest real paths; this module gives the
read/list tools the same recovery without growing `fs_tools.py` past its size
ratchet. Candidates are PATHS only - never file contents.
"""

from __future__ import annotations

from typing import Any


def with_path_suggestions(
    ctx: Any, path: str, text: str, payload: Any,
) -> tuple[str, Any]:
    """Turn a bare not-found into a question with candidate paths.

    Returns `(text, payload)` unchanged when the refusal is not a missing path,
    when there is no workspace, or when the search yields nothing. A path
    candidate is added to `details.candidates` so the front door can render it.
    """
    if not isinstance(payload, dict) or payload.get("status") != "refused":
        return text, payload
    if payload.get("code") not in ("PATH_NOT_FOUND", "NOT_FOUND"):
        return text, payload
    ws = getattr(ctx, "workspace", None)
    if ws is None:
        return text, payload
    try:
        from ..edit_tools import nearest_paths  # noqa: PLC0415 - avoids a module cycle

        candidates = nearest_paths(ws, path)
    except Exception:  # noqa: BLE001 - suggestions are best-effort
        return text, payload
    if not candidates:
        return text, payload
    details = dict(payload.get("details") or {})
    details["candidates"] = candidates
    payload = {**payload, "details": details}
    shown = "\n".join(f"- {candidate}" for candidate in candidates)
    return f"{text}\n\nDid you mean one of these?\n{shown}", payload


__all__ = ["with_path_suggestions"]
