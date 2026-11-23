"""Resolve a credential reference for a broker adapter.

A connection config never stores a secret VALUE. It stores a NAME:
  * an environment-variable name (`ALPACA_KEYS`), or
  * a `secret://provider/name` reference resolved by the app's secrets manager.

The value is used only in an outbound header/query and is never logged or
returned in an error.
"""

from __future__ import annotations

import json
import os
import urllib.parse
from typing import Any, Optional

from ..adapter import AdapterUnavailable


def validate_http_url(raw: str, *, code: str, label: str) -> str:
    """Only http(s), no embedded credentials, no empty host."""
    text = str(raw or "").strip().rstrip("/")
    if not text:
        raise AdapterUnavailable(code, f"{label} is required")
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise AdapterUnavailable(code, f"{label} must be an http(s) URL")
    if parsed.username or parsed.password:
        raise AdapterUnavailable(code, f"{label} must not embed credentials")
    return text


def resolve_secret_value(
    ref: str,
    *,
    code: str = "BROKER_CREDENTIAL",
    label: str = "broker credential",
) -> Optional[str]:
    raw = str(ref or "").strip()
    if not raw:
        return None
    if raw.startswith("secret://"):
        try:
            from ...security.secrets import build_default_secrets_manager

            return build_default_secrets_manager().resolve(
                raw,
                consumer="trade",
                action="read",
                resource="broker_adapter",
                require_grant=False,
            )
        except Exception as exc:  # noqa: BLE001 - a missing secret is a named refusal
            raise AdapterUnavailable(
                code,
                f"the {label} reference {raw!r} could not be resolved",
            ) from exc
    value = os.environ.get(raw)
    if value is None:
        raise AdapterUnavailable(
            code,
            f"environment variable {raw!r} for the {label} is not set",
        )
    return value


def parse_key_pair(raw: str) -> tuple[str, str]:
    """Accept `key:secret` or `{"key": "...", "secret": "..."}` JSON."""
    text = (raw or "").strip()
    if not text:
        raise AdapterUnavailable("BROKER_CREDENTIAL", "the credential value is empty")
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise AdapterUnavailable(
                "BROKER_CREDENTIAL",
                "the credential JSON is invalid",
            ) from exc
        key = str(data.get("key") or data.get("keyId") or data.get("apiKey") or "")
        secret = str(data.get("secret") or data.get("secretKey") or data.get("apiSecret") or "")
        if key and secret:
            return key, secret
        raise AdapterUnavailable(
            "BROKER_CREDENTIAL",
            "the credential JSON needs `key` and `secret`",
        )
    key, sep, secret = text.partition(":")
    if sep and key.strip() and secret.strip():
        return key.strip(), secret.strip()
    raise AdapterUnavailable(
        "BROKER_CREDENTIAL",
        "the credential must be `key:secret` or a JSON object with key/secret",
    )


def parse_json_object(raw: str, *, label: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise AdapterUnavailable("BROKER_CREDENTIAL", f"the {label} JSON is invalid") from exc
    if not isinstance(data, dict):
        raise AdapterUnavailable("BROKER_CREDENTIAL", f"the {label} must be a JSON object")
    return data


__all__ = ["parse_json_object", "parse_key_pair", "resolve_secret_value", "validate_http_url"]
