"""Structured error vocabulary for the Portfolio Accounting Engine.

Errors are data, not crashes. Every failure names a stable machine code so the
agent, the HTTP front door and the UI can branch on it and render an actionable
message — never a bare exception string and never a fabricated value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# ── Error codes ───────────────────────────────────────────────────────────────
PORTFOLIO_NOT_FOUND = "PORTFOLIO_NOT_FOUND"
PORTFOLIO_FORBIDDEN = "PORTFOLIO_FORBIDDEN"
ACCOUNT_NOT_FOUND = "ACCOUNT_NOT_FOUND"
ACCOUNT_EXISTS = "ACCOUNT_EXISTS"
ACCOUNT_IN_USE = "ACCOUNT_IN_USE"
INVALID_ACCOUNT = "INVALID_ACCOUNT"
UNSUPPORTED_CORPORATE_ACTION = "UNSUPPORTED_CORPORATE_ACTION"
BASIS_UNAVAILABLE = "BASIS_UNAVAILABLE"
INVALID_TRANSACTION = "INVALID_TRANSACTION"
INVALID_QUANTITY = "INVALID_QUANTITY"
NEGATIVE_QUANTITY = "NEGATIVE_QUANTITY"
ZERO_QUANTITY = "ZERO_QUANTITY"
INVALID_PRICE = "INVALID_PRICE"
INVALID_AMOUNT = "INVALID_AMOUNT"
INSUFFICIENT_POSITION = "INSUFFICIENT_POSITION"
SHORT_NOT_SUPPORTED = "SHORT_NOT_SUPPORTED"
DUPLICATE_TRANSACTION = "DUPLICATE_TRANSACTION"
UNSUPPORTED_TRANSACTION = "UNSUPPORTED_TRANSACTION"
UNSUPPORTED_ASSET = "UNSUPPORTED_ASSET"
LOT_MISMATCH = "LOT_MISMATCH"
MISSING_PRICE = "MISSING_PRICE"
STALE_PRICE = "STALE_PRICE"
MISSING_FX = "MISSING_FX"
INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
UNSUPPORTED_METHOD = "UNSUPPORTED_METHOD"
INVALID_TARGET = "INVALID_TARGET"
DATABASE_FAILURE = "DATABASE_FAILURE"
MARKET_DATA_FAILURE = "MARKET_DATA_FAILURE"
NOT_IMPLEMENTED = "NOT_IMPLEMENTED"


@dataclass
class PortfolioError(Exception):
    """A structured, actionable, non-fatal portfolio error."""

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    retryable: bool = False

    def __post_init__(self) -> None:  # pragma: no cover - dataclass plumbing
        Exception.__init__(self, self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
            "retryable": self.retryable,
        }


def missing_price(symbol: str, reason: str = "no price available") -> PortfolioError:
    return PortfolioError(MISSING_PRICE, f"no market price for {symbol}: {reason}", {"symbol": symbol})


def missing_fx(base: str, quote: str) -> PortfolioError:
    return PortfolioError(MISSING_FX, f"no FX rate for {base}/{quote}", {"base": base, "quote": quote})


def invalid(message: str, code: str = INVALID_TRANSACTION, **details: Any) -> PortfolioError:
    return PortfolioError(code, message, details)


class PortfolioRefused(Exception):
    """A hard refusal raised for programmer/authorization errors.

    Used only where continuing would be unsafe (e.g. an unknown cost-basis
    method). Runtime data problems use `PortfolioError` instead.
    """


__all__ = [
    "PortfolioError",
    "PortfolioRefused",
    "PORTFOLIO_NOT_FOUND",
    "PORTFOLIO_FORBIDDEN",
    "ACCOUNT_NOT_FOUND",
    "UNSUPPORTED_CORPORATE_ACTION",
    "BASIS_UNAVAILABLE",
    "INVALID_TRANSACTION",
    "INVALID_QUANTITY",
    "NEGATIVE_QUANTITY",
    "ZERO_QUANTITY",
    "INVALID_PRICE",
    "INVALID_AMOUNT",
    "INSUFFICIENT_POSITION",
    "SHORT_NOT_SUPPORTED",
    "DUPLICATE_TRANSACTION",
    "UNSUPPORTED_TRANSACTION",
    "UNSUPPORTED_ASSET",
    "LOT_MISMATCH",
    "MISSING_PRICE",
    "STALE_PRICE",
    "MISSING_FX",
    "INSUFFICIENT_HISTORY",
    "UNSUPPORTED_METHOD",
    "INVALID_TARGET",
    "DATABASE_FAILURE",
    "MARKET_DATA_FAILURE",
    "NOT_IMPLEMENTED",
    "missing_price",
    "missing_fx",
    "invalid",
]
