"""One human-readable summary for a filesystem mutation result.

Kept beside the fs tools (not inside `fs_tools.py`) because it is presentation,
not an operation, and because `fs_tools.py` is pinned to a size ratchet. It is
also the ONE place the post-write format and diagnostics lines enter the model's
tool text, so `write_file`, `create_file` and `code_edit` cannot diverge.
"""

from __future__ import annotations

from typing import Any

from ..post_edit import render_diagnostics, render_format


def summarize(data: dict[str, Any], *, verb: str, path: str) -> str:
    changed = data.get("changed")
    if changed is False:
        return f"No change made to {path}."
    if data.get("dryRun"):
        preview = data.get("diff") or ""
        return f"Dry run for {verb} on {path} (nothing written).\n{preview[:2000]}"
    if "diff" in data and data.get("diff"):
        text = f"{verb} {path}.\n{data['diff'][:2000]}"
    else:
        text = f"{verb} {path}."
    # Name the ABSOLUTE path. A model that only hears "src/x.py" cannot answer
    # "where is it?" — and a mismatch with the folder the person expects stays
    # invisible. Never repeat it when it equals what was already shown.
    absolute = data.get("absolutePath")
    if absolute and str(absolute) != str(path):
        text += f"\nAbsolute path: {absolute}"
    extra = [
        render_format(data.get("format")),
        render_diagnostics(data.get("diagnostics")),
    ]
    return "\n".join([text, *[line for line in extra if line]])


__all__ = ["summarize"]
