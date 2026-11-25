"""Typed canvas edit operations — validation and deterministic application.

The model plans; this module decides what is legal. Every operation is checked
against the *current* content before anything is written. There is no eval, no
template rendering and no arbitrary code execution.
"""

from __future__ import annotations

import re
from typing import Iterable

from .model import CanvasError, CanvasOp, EditOperation, MAX_OPERATIONS

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")


class OpError(CanvasError):
    code = "CANVAS_INVALID_OPERATION"


def _fail(message: str) -> None:
    raise OpError(message)


def _line_offsets(content: str) -> list[tuple[int, int, str]]:
    """Return (start, end, line-without-newline) for every line."""
    rows: list[tuple[int, int, str]] = []
    offset = 0
    for raw in content.splitlines(keepends=True):
        body = raw.rstrip("\r\n")
        rows.append((offset, offset + len(body), body))
        offset += len(raw)
    if content and not content.endswith(("\n", "\r")) and not rows:
        rows.append((0, len(content), content))
    return rows


def _section(content: str, heading: str, *, body_only: bool) -> tuple[int, int]:
    needle = heading.strip().lstrip("#").strip()
    if not needle:
        _fail("a section operation requires a heading")
    rows = _line_offsets(content)
    for index, (start, _end, line) in enumerate(rows):
        match = _HEADING.match(line.strip())
        if not match:
            continue
        if match.group(2).strip().casefold() != needle.casefold():
            continue
        level = len(match.group(1))
        section_end = len(content)
        for later_start, _later_end, later in rows[index + 1:]:
            later_match = _HEADING.match(later.strip())
            if later_match and len(later_match.group(1)) <= level:
                section_end = later_start
                break
        if body_only:
            heading_end = start + len(rows[index][2])
            if heading_end < len(content) and content[heading_end:heading_end + 1] == "\r":
                heading_end += 1
            if heading_end < len(content) and content[heading_end:heading_end + 1] == "\n":
                heading_end += 1
            return heading_end, section_end
        return start, section_end
    _fail(f"section {heading!r} was not found in the current content")


def _ensure_trailing_newline(text: str) -> str:
    return text if not text or text.endswith("\n") else text + "\n"


def apply_operations(
    content: str, operations: Iterable[EditOperation], *, kind: str = "document"
) -> str:
    """Apply operations in order, validating each against the live content."""
    if kind == "presentation":
        raise OpError(
            "text operations do not apply to decks; use the presentation deck ops"
        )
    ops = list(operations)
    if len(ops) > MAX_OPERATIONS:
        _fail(f"at most {MAX_OPERATIONS} operations may be applied at once")
    if any(op.op is CanvasOp.REPLACE_DOCUMENT for op in ops) and len(ops) > 1:
        _fail("replace_document must be the only operation in a proposal")

    current = content
    for index, operation in enumerate(ops):
        try:
            current = _apply_one(current, operation, kind=kind)
        except OpError as exc:
            raise OpError(f"operation {index + 1}: {exc.message}") from exc
    if len(current.encode("utf-8")) > 2 * 1024 * 1024:
        _fail("the edited content exceeds the canvas size limit")
    return current


def _apply_one(content: str, op: EditOperation, *, kind: str) -> str:
    if op.op is CanvasOp.APPEND:
        if not op.content:
            _fail("append requires content")
        if not content:
            return _ensure_trailing_newline(op.content)
        if content.endswith("\n\n"):
            separator = ""
        elif content.endswith("\n"):
            separator = "\n"
        else:
            separator = "\n\n"
        return content + separator + op.content

    if op.op is CanvasOp.REPLACE_DOCUMENT:
        if not op.content:
            _fail("replace_document requires content")
        return op.content

    if op.op is CanvasOp.REPLACE_TEXT_RANGE:
        if op.start is None or op.end is None:
            _fail("replace_text_range requires start and end")
        if op.end < op.start or op.end > len(content):
            _fail("replace_text_range is outside the current content")
        actual = content[op.start:op.end]
        if op.expected_text is not None and op.expected_text != actual:
            _fail("replace_text_range expectedText does not match the current content")
        return content[:op.start] + op.content + content[op.end:]

    if op.op is CanvasOp.REPLACE_SECTION:
        if kind == "code":
            _fail("replace_section is only available for documents")
        if not op.heading:
            _fail("replace_section requires a heading")
        start, end = _section(content, op.heading, body_only=False)
        replacement = op.content
        tail = content[end:]
        if tail and not replacement.endswith("\n"):
            replacement += "\n"
        if replacement and tail and not tail.startswith("\n"):
            replacement += "\n"
        return content[:start] + replacement + tail

    if op.op is CanvasOp.INSERT_AFTER_SECTION:
        if kind == "code":
            _fail("insert_after_section is only available for documents")
        if not op.heading:
            _fail("insert_after_section requires a heading")
        if not op.content:
            _fail("insert_after_section requires content")
        _start, end = _section(content, op.heading, body_only=True)
        prefix = content[:end]
        tail = content[end:]
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        if prefix and not prefix.endswith("\n\n"):
            prefix += "\n"
        block = op.content.rstrip("\n") + "\n"
        if tail and not tail.startswith("\n"):
            block += "\n"
        return prefix + block + tail

    if op.op is CanvasOp.APPLY_CODE_PATCH:
        if not op.search:
            _fail("apply_code_patch requires search")
        occurrences = content.count(op.search)
        if occurrences == 0:
            _fail("apply_code_patch search text was not found")
        if occurrences > 1:
            _fail("apply_code_patch search text is ambiguous (matched more than once)")
        return content.replace(op.search, op.replace or "", 1)

    _fail(f"unsupported operation {op.op.value!r}")


def describe(operations: Iterable[EditOperation]) -> str:
    labels = {
        CanvasOp.APPEND: "append content",
        CanvasOp.REPLACE_TEXT_RANGE: "replace text range",
        CanvasOp.REPLACE_SECTION: "replace section",
        CanvasOp.INSERT_AFTER_SECTION: "insert after section",
        CanvasOp.REPLACE_DOCUMENT: "replace document",
        CanvasOp.APPLY_CODE_PATCH: "apply code patch",
    }
    return ", ".join(labels.get(op.op, op.op.value) for op in operations) or "no change"


__all__ = ["OpError", "apply_operations", "describe"]
