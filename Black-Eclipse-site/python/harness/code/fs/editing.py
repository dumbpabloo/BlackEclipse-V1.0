"""The edit/patch engine — minimal, validated, reversible text changes.

Preferred workflow: search → read region → generate a MINIMAL patch → validate →
hash-check → apply → verify. This module owns the "minimal patch" half.

Operations:
  * `replace_text` — literal or regex replacement with an exact occurrence count;
    refuses an ambiguous target instead of replacing the first hit.
  * `patch_file`   — one exact replacement keyed by unique surrounding context.
  * `insert_text`  — before/after an anchor, at a line, or at a character offset.
  * `delete_text`  — exact text or a line range.
  * `apply_diff`   — a standard unified diff, fully validated before any write.
  * `multi_file_patch` — many patches, all-or-nothing (see `transactions`).

Every mutation:
  * checks the autonomy/permission gate;
  * verifies the expected base hash when supplied (optimistic concurrency);
  * preserves the file's encoding, BOM and line endings;
  * writes atomically and rolls back on failure;
  * returns the resulting diff.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from . import encoding as enc
from . import errors as err
from . import metadata as meta
from . import resolver as res
from .context import FsContext
from .diff import preview_for
from .transactions import FileTransaction

MAX_SPAN_CHARS = 100_000
MAX_PATCHES = 50


# ── shared read/verify/write ────────────────────────────────────────────────


@dataclass
class _Editable:
    ctx: FsContext
    path: Path
    rel: str
    text: str
    encoding: str
    had_bom: bool
    newline: str
    before_hash: str


def _load_editable(ctx: FsContext, path: str, *, expected_hash: Optional[str]) -> _Editable:
    target = res.require_regular_file(ctx.ws, path)
    if res.is_binary_file(target):
        raise err.FsError(err.UNSUPPORTED_BINARY_OPERATION, "That file is not editable text.", path=path)
    decoded = enc.read_text(target)
    before_hash = meta.hash_file(target)
    if expected_hash is not None and expected_hash != before_hash:
        raise err.FsError(
            err.STALE_FILE,
            "That file changed since it was read.",
            path=res.relative(ctx.ws, target),
            details={"expectedHash": expected_hash, "actualHash": before_hash},
        )
    return _Editable(
        ctx=ctx, path=target, rel=res.relative(ctx.ws, target), text=decoded.text,
        encoding=decoded.encoding, had_bom=decoded.had_bom, newline=decoded.newline,
        before_hash=before_hash,
    )


def _commit_edit(edit: _Editable, new_text: str, *, expect_text: Optional[str] = None) -> dict[str, Any]:
    if new_text == edit.text:
        return {
            "status": "ok", "path": edit.rel, "changed": False, "bytesWritten": 0,
            **preview_for(edit.rel, edit.text, new_text),
        }
    txn = FileTransaction(edit.ctx.ws)
    txn.backup(edit.path)
    try:
        nbytes = enc.write_text_atomic(edit.path, new_text, encoding=edit.encoding, had_bom=edit.had_bom)
    except err.FsError:
        txn.rollback()
        raise
    txn.commit()
    # Gap #5: the project's formatter runs on the saved file BEFORE the hash and
    # diff are reported, so the model sees the bytes that actually exist. It is
    # bounded and non-fatal; a failure leaves the written text intact.
    from .. import formatting as fmt_mod  # noqa: PLC0415 - keeps the edit path lean
    from .. import post_edit  # noqa: PLC0415

    format_info = fmt_mod.maybe_format(edit.path, edit.rel)
    final_text = new_text
    if format_info.get("changed"):
        try:
            final_text = enc.read_text(edit.path).text
        except err.FsError:  # pragma: no cover - the write just succeeded
            final_text = new_text
    preview = preview_for(edit.rel, edit.text, final_text)
    after_hash = meta.hash_file(edit.path)
    result: dict[str, Any] = {
        "status": "ok", "path": edit.rel, "changed": True, "bytesWritten": nbytes,
        "sha256Before": edit.before_hash, "sha256After": after_hash,
        **preview,
        "format": format_info,
    }
    # Verification is part of the lifecycle: a post-write syntax/integrity check
    # rides on every edit result so the agent sees a broken edit immediately.
    from . import verify as verify_mod  # noqa: PLC0415 - avoid a circular import

    result["verification"] = verify_mod.verify_mutation(
        edit.ctx.ws, edit.rel, expected_hash=after_hash,
        expect_text=None if format_info.get("changed") else expect_text,
    )
    result["diagnostics"] = post_edit.diagnostics_for(edit.ctx.ws, edit.rel, final_text)
    return result


# ── tolerant matching (parity with the best coding agents) ──────────────────
#
# A coding model reproduces text from what it read; a refusal for a curly quote,
# a non-breaking space, or trailing whitespace is noise, not safety. Matching is
# therefore tried in tiers, best first, and every tier still refuses ambiguity:
#
#   1. exact bytes (tolerating LF vs CRLF, as before);
#   2. typographic variants — curly quotes, en/em dashes, NBSP and thin spaces;
#   3. per-line trailing whitespace;
#   4. case-insensitive, when the caller allowed it.
#
# Tiers 2-4 run on an LF-only view and map spans back to the source, so a CRLF
# file matches the LF text the model was shown without shifting any offset.

_TYPOGRAPHIC = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
    "\u2014": "-", "\u2015": "-", "\u2212": "-",
    "\u00a0": " ", "\u2002": " ", "\u2003": " ", "\u2004": " ",
    "\u2005": " ", "\u2006": " ", "\u2007": " ", "\u2008": " ",
    "\u2009": " ", "\u200a": " ", "\u202f": " ", "\u205f": " ", "\u3000": " ",
})

#: How many lines a miss hint may show.
_HINT_LINES = 12
#: How long a hint line may be before it is clipped.
_HINT_LINE_CHARS = 200


def _typographic(text: str) -> str:
    """Fold look-alike punctuation to ASCII. Always 1:1, so offsets stay valid."""
    return text.translate(_TYPOGRAPHIC)


def _lf_view(text: str) -> tuple[str, Optional[list[int]]]:
    """The LF-only view of `text` plus a view-offset -> source-offset map."""
    if "\r\n" not in text:
        return text, None
    chars: list[str] = []
    offsets: list[int] = []
    index = 0
    while index < len(text):
        if text[index] == "\r" and index + 1 < len(text) and text[index + 1] == "\n":
            chars.append("\n")
            offsets.append(index)
            index += 2
            continue
        chars.append(text[index])
        offsets.append(index)
        index += 1
    offsets.append(len(text))
    return "".join(chars), offsets


def _to_source(offsets: Optional[list[int]], span: tuple[int, int]) -> tuple[int, int]:
    if offsets is None:
        return span
    return offsets[span[0]], offsets[span[1]]


def _occurrences(haystack: str, needle: str) -> list[tuple[int, int]]:
    if not needle:
        return []
    spans: list[tuple[int, int]] = []
    start = 0
    while True:
        index = haystack.find(needle, start)
        if index < 0:
            return spans
        spans.append((index, index + len(needle)))
        start = index + len(needle)


def _line_matches(view: str, needle: str) -> list[tuple[int, int]]:
    """Matches that differ only in trailing whitespace, whole lines at a time.

    Line endings are already normalised by the LF view. A match consumes the
    final line ending only when the needle itself ends with one, so a match at
    the end of a file without a trailing newline still works.
    """
    if "\n" not in needle:
        return []
    expected = needle.split("\n")
    trailing_newline = needle.endswith("\n")
    if trailing_newline:
        expected.pop()
    if not expected:
        return []
    lines: list[tuple[int, int, str, bool]] = []
    position = 0
    for chunk in view.splitlines(keepends=True):
        has_newline = chunk.endswith("\n")
        text = chunk[:-1] if has_newline else chunk
        lines.append((position, position + len(chunk), text, has_newline))
        position += len(chunk)
    if position < len(view):
        lines.append((position, len(view), view[position:], False))

    candidates: list[tuple[int, int]] = []
    for index in range(0, len(lines) - len(expected) + 1):
        window = lines[index:index + len(expected)]
        if any(
            _typographic(actual[2].rstrip()) != _typographic(wanted.rstrip())
            for actual, wanted in zip(window, expected)
        ):
            continue
        last = window[-1]
        if trailing_newline and not last[3]:
            continue
        start = window[0][0]
        end = last[1] if trailing_newline else last[1] - (1 if last[3] else 0)
        candidates.append((start, end))
    # A shorter overlapping candidate is the same edit seen twice.
    deduped: list[tuple[int, int]] = []
    for candidate in candidates:
        if any(candidate[0] < other[1] and other[0] < candidate[1] for other in deduped):
            continue
        deduped.append(candidate)
    return deduped


def _miss_hint(text: str, old_text: str) -> str:
    """Numbered lines closest to a miss, so the model can retry without a re-read."""
    view, _offsets = _lf_view(text)
    source = _typographic(old_text.replace("\r\n", "\n"))
    wanted_text = source.split("\n")

    def key(line: str) -> str:
        return " ".join(_typographic(line).split())

    def distinct(line: str) -> bool:
        return any(char.isalnum() for char in line)

    lines = view.split("\n")
    positions: dict[str, list[int]] = {}
    for index, line in enumerate(lines):
        value = key(line)
        if distinct(value):
            positions.setdefault(value, []).append(index)

    votes: dict[int, int] = {}
    wanted = 0
    for offset, line in enumerate(wanted_text):
        value = key(line)
        if not distinct(value):
            continue
        wanted += 1
        for index in positions.get(value, []):
            votes[index - offset] = votes.get(index - offset, 0) + 1
    if not votes:
        return ""
    start, count = max(votes.items(), key=lambda item: item[1])
    if count == 0 or count * 2 < wanted:
        return ""
    matched = [
        start + offset
        for offset, line in enumerate(wanted_text)
        if key(line) and 0 <= start + offset < len(lines) and key(lines[start + offset]) == key(line)
    ]
    if not matched:
        return ""
    first = matched[0]
    last = min(matched[-1] + 1, first + _HINT_LINES)
    shown = [
        f"{first + offset + 1}: "
        + (line[:_HINT_LINE_CHARS] + "..." if len(line) > _HINT_LINE_CHARS else line)
        for offset, line in enumerate(lines[first:last])
    ]
    return "The closest lines in the file:\n" + "\n".join(shown)


def _preserve_endings(new_text: str, newline: str) -> str:
    """Give a replacement the file's line endings so an edit cannot mix them."""
    if newline == "\r\n":
        return re.sub(r"(?<!\r)\n", "\r\n", new_text)
    if newline == "\n":
        return new_text.replace("\r\n", "\n")
    return new_text


def _match_text(edit: _Editable, needle: str) -> str:
    """The needle as it appears in the file, tolerating a CRLF/LF difference.

    A model usually writes `\n`; a Windows checkout stores `\r\n`. We try the
    literal first, then the line-ending-adjusted form, so an edit is not refused
    for a reason unrelated to intent — while the file's own endings are preserved.
    """
    if needle in edit.text:
        return needle
    if edit.newline == "\r\n" and "\n" in needle and "\r\n" not in needle:
        adjusted = needle.replace("\n", "\r\n")
        if adjusted in edit.text:
            return adjusted
    if edit.newline == "\n" and "\r\n" in needle:
        adjusted = needle.replace("\r\n", "\n")
        if adjusted in edit.text:
            return adjusted
    return needle


# ── replace_text ────────────────────────────────────────────────────────────


def replace_text(
    ctx: FsContext,
    path: str,
    old_text: str,
    new_text: str,
    *,
    count: int = 1,
    expected_occurrences: Optional[int] = None,
    regex: bool = False,
    case_sensitive: bool = True,
    expected_hash: Optional[str] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    ctx.require("write", path)
    if not old_text:
        raise err.FsError(err.INVALID_PATH, "There was no text to replace.")
    if len(old_text) > MAX_SPAN_CHARS:
        raise err.FsError(err.TOO_LARGE, "The text to replace is too large.")
    edit = _load_editable(ctx, path, expected_hash=expected_hash)
    matches = _find_matches(edit, old_text, regex=regex, case_sensitive=case_sensitive)
    actual = len(matches)
    if actual == 0:
        hint = _miss_hint(edit.text, old_text)
        details: dict[str, Any] = {"searched": old_text[:120]}
        if hint:
            details["hint"] = hint
        raise err.FsError(
            err.PATCH_CONTEXT_MISMATCH,
            "The text to replace was not found."
            + (" The closest lines are shown in details.hint." if hint else " Re-read the region and retry."),
            path=edit.rel,
            details=details,
        )
    if expected_occurrences is not None and actual != expected_occurrences:
        raise err.FsError(
            err.AMBIGUOUS_PATCH,
            f"The text appears {actual} time(s), not {expected_occurrences}.",
            path=edit.rel,
            details={"occurrences": actual, "expectedOccurrences": expected_occurrences, "lines": _match_lines(edit, matches)},
        )
    if expected_occurrences is None and count == 1 and actual > 1:
        raise err.FsError(
            err.AMBIGUOUS_PATCH,
            f"The text appears {actual} times. Add more context or set count.",
            path=edit.rel,
            details={"occurrences": actual, "lines": _match_lines(edit, matches)},
        )
    if dry_run or ctx.dry_run:
        new_text_value = _apply_matches(edit, old_text, new_text, matches, count, regex, case_sensitive)
        return {"status": "ok", "dryRun": True, "path": edit.rel, "occurrences": actual,
                **preview_for(edit.rel, edit.text, new_text_value)}
    new_text_value = _apply_matches(edit, old_text, new_text, matches, count, regex, case_sensitive)
    result = _commit_edit(
        edit, new_text_value,
        expect_text=(None if regex else _preserve_endings(new_text, edit.newline)),
    )
    result["occurrences"] = actual
    result["replaced"] = actual if count <= 0 else min(count, actual)
    return result


def _find_matches(edit: _Editable, old_text: str, *, regex: bool, case_sensitive: bool) -> list[tuple[int, int]]:
    if regex:
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            pattern = re.compile(old_text, flags)
        except re.error as exc:
            raise err.FsError(err.INVALID_PATH, f"That pattern isn't valid: {exc}") from None
        return [(m.start(), m.end()) for m in pattern.finditer(edit.text)]

    # Tier 1: exact bytes, already tolerating the file's LF/CRLF convention.
    needle = _match_text(edit, old_text)
    spans: list[tuple[int, int]] = []
    start = 0
    while True:
        index = edit.text.find(needle, start)
        if index < 0:
            break
        spans.append((index, index + len(needle)))
        start = index + len(needle)
    if spans:
        return spans

    # Tiers 2-4 run on the LF view the model was shown, then map back.
    view, offsets = _lf_view(edit.text)
    wanted = old_text.replace("\r\n", "\n")
    if not case_sensitive:
        found = _occurrences(view.lower(), wanted.lower())
        if found:
            return [_to_source(offsets, span) for span in found]
    found = _occurrences(_typographic(view), _typographic(wanted))
    if found:
        return [_to_source(offsets, span) for span in found]
    found = _line_matches(view, wanted)
    if found:
        return [_to_source(offsets, span) for span in found]
    return []


def _match_lines(edit: _Editable, spans: list[tuple[int, int]]) -> list[int]:
    return [edit.text.count("\n", 0, start) + 1 for start, _end in spans]


def _apply_matches(
    edit: _Editable, old_text: str, new_text: str,
    spans: list[tuple[int, int]], count: int, regex: bool, case_sensitive: bool,
) -> str:
    selected = spans if count <= 0 else spans[:count]
    # Right-to-left so earlier offsets remain valid. The replacement takes the
    # file's line endings; otherwise writing an LF snippet into a CRLF checkout
    # would silently mix endings. Only the replaced spans are touched, so a
    # mixed-ending file keeps every byte the edit did not target.
    replacement = _preserve_endings(new_text, edit.newline)
    out = edit.text
    for start, end in reversed(selected):
        out = out[:start] + replacement + out[end:]
    return out


# ── patch_file ──────────────────────────────────────────────────────────────


def patch_file(
    ctx: FsContext,
    path: str,
    operation: str,
    *,
    old_text: Optional[str] = None,
    new_text: Optional[str] = None,
    expected_hash: Optional[str] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """One minimal change. `operation` is replace | insert | delete | append | prepend."""
    op = str(operation or "replace").lower()
    if op == "replace":
        if old_text is None or new_text is None:
            raise err.FsError(err.INVALID_PATH, "A replace needs oldText and newText.")
        return replace_text(
            ctx, path, old_text, new_text, count=1,
            expected_hash=expected_hash, dry_run=dry_run,
        )
    if op == "append":
        return insert_text(ctx, path, new_text or "", at_line=None, anchor=None, position="end",
                           expected_hash=expected_hash, dry_run=dry_run)
    if op == "prepend":
        return insert_text(ctx, path, new_text or "", anchor=None, position="start",
                           expected_hash=expected_hash, dry_run=dry_run)
    if op == "insert":
        if old_text is None:
            raise err.FsError(err.INVALID_PATH, "An insert needs an anchor (oldText) or a line.")
        return insert_text(ctx, path, new_text or "", anchor=old_text, position="after",
                           expected_hash=expected_hash, dry_run=dry_run)
    if op == "delete":
        if old_text is None:
            raise err.FsError(err.INVALID_PATH, "A delete needs oldText or a line range.")
        return delete_text(ctx, path, old_text=old_text, expected_hash=expected_hash, dry_run=dry_run)
    raise err.FsError(err.INVALID_PATH, f"Unknown patch operation {operation!r}.")


# ── insert_text ─────────────────────────────────────────────────────────────


def insert_text(
    ctx: FsContext,
    path: str,
    text: str,
    *,
    anchor: Optional[str] = None,
    after_anchor: Optional[str] = None,
    before_anchor: Optional[str] = None,
    position: str = "after",
    at_line: Optional[int] = None,
    at_offset: Optional[int] = None,
    expected_hash: Optional[str] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    ctx.require("write", path)
    edit = _load_editable(ctx, path, expected_hash=expected_hash)
    if at_offset is not None:
        offset = max(0, min(int(at_offset), len(edit.text)))
        new_value = edit.text[:offset] + text + edit.text[offset:]
    elif at_line is not None:
        lines = edit.text.splitlines(keepends=True)
        index = max(0, min(int(at_line) - 1, len(lines)))
        lines.insert(index, text if text.endswith(("\n", "\r\n")) else text + edit.newline)
        new_value = "".join(lines)
    else:
        effective_anchor = after_anchor or before_anchor or anchor
        mode = "after" if after_anchor is not None else ("before" if before_anchor is not None else position)
        if not effective_anchor:
            if position == "start":
                new_value = text + ("" if text.endswith(("\n", "\r\n")) else edit.newline) + edit.text
            elif position == "end":
                prefix = "" if not edit.text or edit.text.endswith(("\n", "\r\n")) else edit.newline
                new_value = edit.text + prefix + text
            else:
                raise err.FsError(err.INVALID_PATH, "An insert needs an anchor, a line, or an offset.")
        else:
            needle = _match_text(edit, effective_anchor)
            occurrences = edit.text.count(needle)
            if occurrences == 0:
                raise err.FsError(err.PATCH_CONTEXT_MISMATCH, "The anchor text was not found.", path=edit.rel)
            if occurrences > 1:
                raise err.FsError(
                    err.AMBIGUOUS_PATCH,
                    f"The anchor appears {occurrences} times. Use more context.",
                    path=edit.rel,
                    details={"occurrences": occurrences},
                )
            index = edit.text.find(needle)
            if mode == "before":
                insert_at = index
            else:
                insert_at = index + len(needle)
            addition = text
            if mode == "after":
                has_eol = needle.endswith(("\n", "\r\n")) or insert_at == len(edit.text)
                if not has_eol:
                    addition = edit.newline + text
            new_value = edit.text[:insert_at] + addition + edit.text[insert_at:]
    if dry_run or ctx.dry_run:
        return {"status": "ok", "dryRun": True, "path": edit.rel, **preview_for(edit.rel, edit.text, new_value)}
    result = _commit_edit(edit, new_value, expect_text=text)
    result["inserted"] = True
    return result


# ── delete_text ─────────────────────────────────────────────────────────────


def delete_text(
    ctx: FsContext,
    path: str,
    *,
    old_text: Optional[str] = None,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    expected_hash: Optional[str] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    ctx.require("write", path)
    edit = _load_editable(ctx, path, expected_hash=expected_hash)
    if old_text is not None:
        needle = _match_text(edit, old_text)
        occurrences = edit.text.count(needle)
        if occurrences == 0:
            raise err.FsError(err.PATCH_CONTEXT_MISMATCH, "The text to delete was not found.", path=edit.rel)
        if occurrences > 1:
            raise err.FsError(
                err.AMBIGUOUS_PATCH,
                f"The text to delete appears {occurrences} times. Add more context.",
                path=edit.rel, details={"occurrences": occurrences},
            )
        start = edit.text.find(needle)
        new_value = edit.text[:start] + edit.text[start + len(needle):]
    elif start_line is not None:
        lines = edit.text.splitlines(keepends=True)
        first = max(0, int(start_line) - 1)
        last = min(len(lines), int(end_line) if end_line is not None else int(start_line))
        if first >= len(lines) or first >= last:
            raise err.FsError(err.INVALID_PATH, "That line range isn't in the file.", path=edit.rel)
        new_value = "".join(lines[:first] + lines[last:])
    else:
        raise err.FsError(err.INVALID_PATH, "A delete needs text or a line range.")
    if dry_run or ctx.dry_run:
        return {"status": "ok", "dryRun": True, "path": edit.rel, **preview_for(edit.rel, edit.text, new_value)}
    result = _commit_edit(edit, new_value)
    result["deleted"] = True
    return result


# ── apply_diff (unified) ────────────────────────────────────────────────────


@dataclass
class _FileDiff:
    path: str
    hunks: list[tuple[int, list[str]]]   # (old_start_line_1based, hunk body lines)


def parse_unified_diff(diff_text: str) -> list[_FileDiff]:
    """Parse a standard unified diff into per-file hunks. Raises `DIFF_FAILED`."""
    if not diff_text or not diff_text.strip():
        raise err.FsError(err.DIFF_FAILED, "There was no change to read.")
    lines = diff_text.splitlines()
    files: list[_FileDiff] = []
    current: Optional[_FileDiff] = None
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("--- "):
            if index + 1 >= len(lines) or not lines[index + 1].startswith("+++ "):
                raise err.FsError(err.DIFF_FAILED, "The change is missing its target file line.")
            raw_target = lines[index + 1][4:].strip()
            path = _normalize_diff_path(raw_target)
            current = _FileDiff(path=path, hunks=[])
            files.append(current)
            index += 2
            continue
        if line.startswith("@@ "):
            if current is None:
                raise err.FsError(err.DIFF_FAILED, "A hunk appeared before its file.")
            header, body, index = _read_hunk(lines, index)
            current.hunks.append((header, body))
            continue
        index += 1
    if not files:
        raise err.FsError(err.DIFF_FAILED, "The change named no files.")
    if any(not f.hunks for f in files):
        raise err.FsError(err.DIFF_FAILED, "A file in the change had no hunks.")
    return files


def _normalize_diff_path(raw: str) -> str:
    text = raw.strip().split("\t")[0]
    if text in ("/dev/null",):
        return text
    if text.startswith("a/") or text.startswith("b/"):
        text = text[2:]
    if text.startswith("/"):  # git absolute form /abs/path -> strip leading slash only
        text = text.lstrip("/")
    return text


def _read_hunk(lines: list[str], index: int) -> tuple[int, list[str], int]:
    header = lines[index]
    match = re.match(r"@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@", header)
    if not match:
        raise err.FsError(err.DIFF_FAILED, "A hunk header could not be read.")
    old_start = int(match.group(1))
    body: list[str] = []
    index += 1
    while index < len(lines):
        line = lines[index]
        if line.startswith(("@@ ", "--- ", "+++ ")):
            break
        if line.startswith("\\"):  # "\ No newline at end of file"
            index += 1
            continue
        body.append(line)
        index += 1
    return old_start, body, index


def _apply_hunks(text: str, hunks: list[tuple[int, list[str]]], path: str, *, newline: str) -> str:
    """Apply validated hunks. Context lines must match or `PATCH_CONTEXT_MISMATCH`."""
    lines = text.splitlines()
    trailing_newline = text.endswith(("\n", "\r\n"))
    # Apply from the bottom so earlier line numbers stay valid.
    ordered = sorted(hunks, key=lambda h: h[0], reverse=True)
    for start, body in ordered:
        # Collect the old side and the new side for this hunk.
        old_side: list[str] = []
        new_side: list[str] = []
        for raw in body:
            if not raw:
                old_side.append("")
                new_side.append("")
                continue
            marker, content = raw[0], raw[1:]
            if marker == " ":
                old_side.append(content)
                new_side.append(content)
            elif marker == "-":
                old_side.append(content)
            elif marker == "+":
                new_side.append(content)
            elif marker == "\\":
                continue
            else:
                # An unprefixed line is treated as context (some tools omit space).
                old_side.append(raw)
                new_side.append(raw)
        index = start - 1
        end = index + len(old_side)
        if index < 0 or end > len(lines):
            raise err.FsError(err.PATCH_CONTEXT_MISMATCH, "A change hunk points past the end of the file.", path=path)
        actual = lines[index:end]
        if actual != old_side:
            raise err.FsError(
                err.PATCH_CONTEXT_MISMATCH,
                "The file's current content does not match the change.",
                path=path,
                details={"expectedLine": start, "found": actual[:3]},
            )
        lines[index:end] = new_side
    result = newline.join(lines)
    if trailing_newline:
        result += newline
    return result


def _read_for_diff(ctx: FsContext, path: str) -> tuple[Path, Optional[str], str, bool, str, bool]:
    """Return (target, text, encoding, had_bom, newline, existed) for a diff path."""
    if path == "/dev/null":
        raise err.FsError(err.DIFF_FAILED, "A change can't target /dev/null.")
    target = res.resolve(ctx.ws, path, for_write=True)
    if target.exists() and target.is_dir():
        raise err.FsError(err.NOT_A_FILE, "A change targeted a folder.", path=path)
    if not target.exists():
        return target, None, "utf-8", False, "\n", False
    if res.is_binary_file(target):
        raise err.FsError(err.UNSUPPORTED_BINARY_OPERATION, "A change targeted a binary file.", path=path)
    decoded = enc.read_text(target)
    return target, decoded.text, decoded.encoding, decoded.had_bom, decoded.newline, True


def apply_diff(ctx: FsContext, diff_text: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Apply a unified diff atomically. The WHOLE diff is validated first."""
    ctx.require("write", "")
    files = parse_unified_diff(diff_text)
    planned: list[dict[str, Any]] = []
    txn = FileTransaction(ctx.ws)
    for file_diff in files:
        ctx.require("write", file_diff.path)
        target, current, encoding, had_bom, newline, existed = _read_for_diff(ctx, file_diff.path)
        if not existed:
            # A new file: reconstruct from the new side of the hunks.
            new_text = _reconstruct_new_file(file_diff.hunks)
        else:
            new_text = _apply_hunks(current or "", file_diff.hunks, file_diff.path, newline=newline)
        planned.append({
            "target": target, "rel": res.relative(ctx.ws, target),
            "before": current, "after": new_text, "encoding": encoding,
            "had_bom": had_bom, "newline": newline, "existed": existed,
        })
    changes = [preview_for(p["rel"], p["before"], p["after"]) for p in planned]
    if dry_run or ctx.dry_run:
        return {
            "status": "ok", "dryRun": True, "filesChanged": len(planned),
            "previews": changes, "applied": [],
        }
    applied: list[dict[str, Any]] = []
    from . import verify as verify_mod  # noqa: PLC0415 - avoid a circular import

    for plan in planned:
        txn.backup(plan["target"])
        enc.write_text_atomic(plan["target"], plan["after"], encoding=plan["encoding"], had_bom=plan["had_bom"])
        after_hash = meta.hash_file(plan["target"])
        applied.append({
            "path": plan["rel"], "sha256": after_hash,
            "verification": verify_mod.verify_mutation(ctx.ws, plan["rel"], expected_hash=after_hash),
        })
    txn.commit()
    return {
        "status": "ok", "dryRun": False, "filesChanged": len(planned),
        "previews": changes, "applied": applied,
    }


def _reconstruct_new_file(hunks: list[tuple[int, list[str]]]) -> str:
    """The full new content of a file created by a diff (context + additions)."""
    out_lines: list[str] = []
    for _start, body in hunks:
        for line in body:
            if not line:
                out_lines.append("")
                continue
            if line.startswith("+"):
                out_lines.append(line[1:])
            elif line.startswith(" "):
                out_lines.append(line[1:])
            elif line.startswith("-"):
                continue
            elif line.startswith("\\"):
                continue
            else:
                out_lines.append(line)
    text = "\n".join(out_lines)
    if text and not text.endswith("\n"):
        text += "\n"
    return text


# ── multi_file_patch ────────────────────────────────────────────────────────


def multi_file_patch(
    ctx: FsContext,
    patches: list[dict[str, Any]],
    *,
    atomic: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Apply many patches. With `atomic`, all validate before any byte is written."""
    if not isinstance(patches, list) or not patches:
        raise err.FsError(err.INVALID_PATH, "There were no changes to make.")
    if len(patches) > MAX_PATCHES:
        raise err.FsError(err.TOO_LARGE, f"More than {MAX_PATCHES} changes at once is too many.")
    # Phase 1: validate + compute every result against a frozen view.
    snapshot = _FreezeView(ctx)
    results: list[dict[str, Any]] = []
    try:
        for patch in patches:
            results.append(_apply_one(snapshot, patch, dry_run=True))
    except err.FsError:
        if atomic:
            raise
        results = []
    # Phase 2: apply for real. Snapshot EVERY target up front in the outer
    # transaction so a failure mid-way restores every file, even though the inner
    # operations commit their own atomic writes.
    if dry_run or ctx.dry_run:
        return {
            "status": "ok", "dryRun": True, "filesChanged": len(results),
            "atomic": atomic, "results": results,
        }
    txn = FileTransaction(ctx.ws)
    snapshotted: set[str] = set()
    for patch in patches:
        target = _target_of(ctx, patch)
        if target is None:
            continue
        key = str(target)
        if key in snapshotted:
            continue
        snapshotted.add(key)
        txn.backup(target)
    applied: list[dict[str, Any]] = []
    try:
        for patch in patches:
            outcome = _apply_one(ctx, patch, dry_run=False, txn=txn)
            applied.append(outcome)
    except err.FsError:
        txn.rollback()
        raise
    txn.commit()
    return {
        "status": "ok", "dryRun": False, "atomic": atomic,
        "filesChanged": len(applied), "results": applied,
    }


def _target_of(ctx: FsContext, patch: dict[str, Any]) -> Optional[Path]:
    """The absolute path a single patch will touch, or None if it is malformed."""
    if not isinstance(patch, dict):
        return None
    path = str(patch.get("path") or "")
    if not path:
        return None
    try:
        return res.resolve(ctx.ws, path, for_write=True)
    except err.FsError:
        return None


class _FreezeView(FsContext):
    """A read-only overlay so the validation phase computes against a frozen view.

    It is only used for the validate phase; because it forces `dry_run=True` no
    operation in the validation pass writes anything.
    """

    def __init__(self, base: FsContext) -> None:
        super().__init__(ws=base.ws, binding=base.binding, actor=base.actor, config=base.config, dry_run=True)


def _apply_one(ctx: FsContext, patch: dict[str, Any], *, dry_run: bool, txn: Optional[FileTransaction] = None) -> dict[str, Any]:
    if not isinstance(patch, dict):
        raise err.FsError(err.INVALID_PATH, "Each change must be an object.")
    from .operations import create_file, delete_file  # noqa: PLC0415 - avoid a circular import

    path = str(patch.get("path") or "")
    operation = str(patch.get("operation") or patch.get("op") or "replace").lower()
    old_text = patch.get("oldText", patch.get("old"))
    new_text = patch.get("newText", patch.get("new"))
    expected_hash = patch.get("expectedHash") or patch.get("expect_hash")
    if operation in ("create",):
        return create_file(ctx, path, str(patch.get("content", new_text or "")), overwrite=bool(patch.get("overwrite", False)), dry_run=dry_run)
    if operation in ("delete",):
        return delete_file(ctx, path, expected_hash=expected_hash, dry_run=dry_run)
    return patch_file(
        ctx, path, operation, old_text=old_text, new_text=new_text,
        expected_hash=expected_hash, dry_run=dry_run,
    )


__all__ = [
    "MAX_PATCHES",
    "apply_diff",
    "delete_text",
    "insert_text",
    "multi_file_patch",
    "parse_unified_diff",
    "patch_file",
    "replace_text",
]
