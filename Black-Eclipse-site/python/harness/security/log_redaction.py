"""Strip secret query-parameter values from access-log lines.

A long-lived credential must never reach a URL, because a URL reaches logs, browser
history, proxy logs and ``Referer`` headers. A local API that accepts ``?api_key=`` or a
stream ticket in ``?ticket=`` therefore has to redact it *at the log record*, before any
handler formats the line — the value is already in the request target by then.

Transcribed from Vibe-Trading's `api/security.py` (``_redact_query_secrets`` and its
``logging.Filter``), MIT. Kept deliberately narrow: only the VALUE of a known sensitive
parameter is replaced, the parameter name survives, and text with no such parameter is
returned unchanged so ordinary log lines are not mangled.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable

__all__ = ["REDACTED", "RedactionFilter", "install_log_redaction", "redact_query_secrets"]

#: The placeholder written in place of a secret value.
REDACTED = "***REDACTED***"

#: The parameters whose values are secret when they appear in a query string.
_SECRET_PARAMS = ("api_key", "ticket")

_QUERY_SECRET_RE = re.compile(
    r"((?:api_key|ticket)=)[^&\s\"']+", re.IGNORECASE
)


def redact_query_secrets(text: str) -> str:
    """Replace ``api_key=…`` / ``ticket=…`` values with a fixed placeholder.

    The name is kept so a reader can see *which* credential leaked and rotate it; only
    the value is removed. A second pass is a no-op, because the placeholder itself
    contains no characters the pattern accepts after ``=``.
    """
    return _QUERY_SECRET_RE.sub(rf"\1{REDACTED}", str(text))


class RedactionFilter(logging.Filter):
    """Apply :func:`redact_query_secrets` to a record's message and arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                redact_query_secrets(arg) if isinstance(arg, str) else arg
                for arg in record.args
            )
        if isinstance(record.msg, str):
            record.msg = redact_query_secrets(record.msg)
        return True


def install_log_redaction(logger_names: Iterable[str] = ("uvicorn.access",)) -> int:
    """Attach the filter to each named logger, idempotently.

    Returns the number of loggers that gained the filter. Re-installing does not stack
    duplicate filters — a server that restarts in-process must not grow a filter chain
    per boot.
    """
    added = 0
    for name in logger_names:
        target = logging.getLogger(name)
        if any(isinstance(item, RedactionFilter) for item in target.filters):
            continue
        target.addFilter(RedactionFilter())
        added += 1
    return added


def redact_any(value: Any) -> Any:
    """Convenience for non-log call sites: redact a string, pass anything else through."""
    return redact_query_secrets(value) if isinstance(value, str) else value
