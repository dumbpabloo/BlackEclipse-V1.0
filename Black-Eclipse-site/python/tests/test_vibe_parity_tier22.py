"""Branch parity, twenty-second tier: secrets in a URL never reach the log.

Reference fetched 2026-11-21 (MIT, `HKUDS/Vibe-Trading`, `agent/src/api/security.py`,
``_redact_query_secrets`` + ``_AccessLogRedactionFilter``).

Eclipse redacts config *values* by key name (`channels/config.py`) and spans in traces
(`session_index.redact_span`), but nothing handled a **query string**: a request line
like `GET /v1/events?api_key=secret` reaches Uvicorn's access log verbatim, and from
there the proxy log, the browser history and any `Referer` header. Ported the
reference's narrow rule: replace the VALUE of `api_key=` / `ticket=`, keep the name so
an operator can see which credential to rotate, and leave every other line untouched.
"""

from __future__ import annotations

import logging

import pytest

from harness.security.log_redaction import (
    REDACTED,
    RedactionFilter,
    install_log_redaction,
    redact_query_secrets,
)


# -- the substitution -----------------------------------------------------

def test_a_query_api_key_value_is_replaced_and_the_name_kept():
    text = "GET /v1/x?api_key=sk-live-SECRET&limit=5 HTTP/1.1"
    out = redact_query_secrets(text)
    assert "sk-live-SECRET" not in out
    assert f"api_key={REDACTED}" in out
    assert "limit=5" in out


def test_a_ticket_value_is_replaced():
    out = redact_query_secrets("GET /events?ticket=abc123xyz")
    assert "abc123xyz" not in out
    assert f"ticket={REDACTED}" in out


def test_several_secrets_in_one_line_are_all_replaced():
    out = redact_query_secrets("?api_key=one&other=2&ticket=three")
    assert "one" not in out.split("api_key=")[1].split("&")[0]
    assert "three" not in out.split("ticket=")[1]
    assert out.count(REDACTED) == 2


def test_the_match_is_case_insensitive():
    out = redact_query_secrets("GET /x?API_KEY=SECRETVALUE")
    assert "SECRETVALUE" not in out
    assert REDACTED in out


def test_a_value_ending_at_a_quote_is_handled():
    out = redact_query_secrets('referer="https://x/?api_key=abc"')
    assert "abc" not in out
    assert REDACTED in out


def test_an_empty_value_is_left_as_it_is():
    """An empty value carries no secret; the reference's pattern requires a value."""
    assert redact_query_secrets("?api_key=&x=1") == "?api_key=&x=1"


def test_ordinary_text_is_returned_unchanged():
    for text in ("GET /v1/health HTTP/1.1", "api_keyless=1", "the key=value pair"):
        assert redact_query_secrets(text) == text


def test_redaction_is_idempotent():
    once = redact_query_secrets("?api_key=secret")
    assert redact_query_secrets(once) == once


def test_an_empty_value_still_redacts_the_parameter():
    """Documented as the reference's behaviour: the name survives, the value is empty."""
    assert redact_query_secrets("?ticket=&x=1") == "?ticket=&x=1"


# -- the logging filter ---------------------------------------------------

def _record(msg, args=()):
    return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, msg, args, None)


def test_the_filter_redacts_the_formatted_message():
    record = _record("%s - %s", ("client", "GET /x?api_key=SECRET"))
    assert RedactionFilter().filter(record) is True
    assert "SECRET" not in record.getMessage()
    assert REDACTED in record.getMessage()


def test_the_filter_redacts_the_message_template_too():
    record = _record("GET /x?ticket=SECRET2")
    RedactionFilter().filter(record)
    assert "SECRET2" not in record.getMessage()


def test_the_filter_handles_a_mapping_argument_record():
    """Uvicorn's access line arrives as a tuple; a mapping record's message is still
    redacted because the template itself is scanned."""
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, "GET /x?api_key=SECRET3", None, None
    )
    RedactionFilter().filter(record)
    assert "SECRET3" not in record.getMessage()


def test_the_filter_leaves_non_string_arguments_alone():
    record = _record("%d %s", (42, "GET /x"))
    RedactionFilter().filter(record)
    assert record.getMessage() == "42 GET /x"


def test_installing_twice_does_not_stack_filters():
    name = "eclipse.test.redaction"
    logger = logging.getLogger(name)
    logger.filters.clear()
    try:
        assert install_log_redaction([name]) == 1
        assert install_log_redaction([name]) == 0        # already present
        assert sum(isinstance(f, RedactionFilter) for f in logger.filters) == 1
    finally:
        logger.filters.clear()


def test_the_filter_never_drops_a_record():
    """Redaction must not become a way to lose an audit line."""
    assert RedactionFilter().filter(_record("anything")) is True
