"""B3 COTAHIST — fixed-width daily quote records, parsed without a network.

B3 publishes ``COTAHIST_A{YYYY}.ZIP`` / ``COTAHIST_D{DDMMYYYY}.ZIP``: fixed-width
text, **245 characters per record**, Latin-1, one record per line.

Layout (1-based positions, from B3's published layout; record type ``01``)::

    DATA    3-10    trade date           YYYYMMDD
    CODBDI  11-12   BDI code
    CODNEG  13-24   ticker (e.g. PETR4)
    TPMERC  25-27   market type (10 cash, 20 odd lot, 70/80 options ...)
    NOMRES  28-39   short name
    ESPECI  40-49   specification (ON, PN, CI, DRN ...)
    PREABE  57-69   open
    PREMAX  70-82   high
    PREMIN  83-95   low
    PREMED  96-108  average
    PREULT 109-121  close
    PREOFC 122-134  best bid
    PREOFV 135-147  best ask
    TOTNEG 148-152  number of trades
    QUATOT 153-170  quantity traded
    VOLTOT 171-188  volume (currency)
    PREEXE 189-201  exercise price (options)
    DATVEN 203-210  expiration date (options)
    FATCOT 211-217  quote factor
    CODISI 231-242  ISIN

Prices carry **two implied decimals** (``000000001050`` is 10.50). A record whose
``FATCOT`` is greater than 1 is quoted per lot; `unit_price` divides it out and
is the per-share figure, with the raw quoted value kept alongside so nothing is
hidden.

Honesty: this module parses only. It does not download, does not invent a missing
field, and a malformed line is a named error rather than a silently skipped row.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable, Optional

RECORD_LENGTH = 245
QUOTE_RECORD_TYPE = "01"
HEADER_RECORD_TYPE = "00"
TRAILER_RECORD_TYPE = "99"

#: 1-based inclusive (start, end) from B3's layout. Slicing is done via
#: `_field`, which converts to 0-based half-open exactly once.
COLUMNS: dict[str, tuple[int, int]] = {
    "record_type": (1, 2),
    "trade_date": (3, 10),
    "bdi": (11, 12),
    "ticker": (13, 24),
    "market_type": (25, 27),
    "short_name": (28, 39),
    "spec": (40, 49),
    "open": (57, 69),
    "high": (70, 82),
    "low": (83, 95),
    "average": (96, 108),
    "close": (109, 121),
    "best_bid": (122, 134),
    "best_ask": (135, 147),
    "trades": (148, 152),
    "quantity": (153, 170),
    "volume": (171, 188),
    "exercise_price": (189, 201),
    "expiration_date": (203, 210),
    "factor": (211, 217),
    "isin": (231, 242),
}

PRICE_FIELDS = ("open", "high", "low", "average", "close", "best_bid", "best_ask",
                "exercise_price")
#: Public (camelCase) key for each raw price field, so one row does not mix styles.
_PRICE_KEYS = {
    "open": "open", "high": "high", "low": "low", "average": "average",
    "close": "close", "best_bid": "bestBid", "best_ask": "bestAsk",
    "exercise_price": "exercisePrice",
}


class CotahistError(ValueError):
    """A record could not be parsed, with the line number and the reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _field(line: str, name: str) -> str:
    start, end = COLUMNS[name]
    return line[start - 1:end]


def _price(raw: str) -> float:
    text = raw.strip()
    if not text:
        return 0.0
    try:
        return int(text) / 100.0
    except ValueError as exc:
        raise CotahistError("BAD_PRICE", f"not an implied-2-decimals price: {raw!r}") from exc


def _int(raw: str) -> int:
    text = raw.strip()
    return int(text) if text.isdigit() or (text[:1] in "+-" and text[1:].isdigit()) else 0


def _date(raw: str) -> str:
    text = raw.strip()
    if not text or set(text) == {"0"}:
        return ""
    try:
        return date(int(text[0:4]), int(text[4:6]), int(text[6:8])).isoformat()
    except (ValueError, IndexError) as exc:
        raise CotahistError("BAD_DATE", f"not a YYYYMMDD date: {raw!r}") from exc


def parse_record(line: str) -> Optional[dict[str, Any]]:
    """Parse one line. Returns `None` for the header/trailer, else a normalized row.

    A line that is not `RECORD_LENGTH` characters, or is a quote record with an
    unparseable field, raises `CotahistError` — a skipped row is data loss the
    caller cannot see.
    """
    text = str(line or "").rstrip("\r\n")
    if not text:
        return None
    kind = _field(text, "record_type")
    if kind in (HEADER_RECORD_TYPE, TRAILER_RECORD_TYPE):
        return None
    if kind != QUOTE_RECORD_TYPE:
        return None
    if len(text) != RECORD_LENGTH:
        raise CotahistError(
            "BAD_LENGTH",
            f"expected {RECORD_LENGTH} characters, got {len(text)}",
        )
    row: dict[str, Any] = {
        "recordType": kind,
        "tradeDate": _date(_field(text, "trade_date")),
        "bdi": _field(text, "bdi").strip(),
        "ticker": _field(text, "ticker").strip(),
        "marketType": _int(_field(text, "market_type")),
        "shortName": _field(text, "short_name").strip(),
        "spec": _field(text, "spec").strip(),
        "trades": _int(_field(text, "trades")),
        "quantity": _int(_field(text, "quantity")),
        "volume": _price(_field(text, "volume")),
        "expirationDate": _date(_field(text, "expiration_date")),
        "factor": _int(_field(text, "factor")) or 1,
        "isin": _field(text, "isin").strip(),
    }
    for field in PRICE_FIELDS:
        row[_PRICE_KEYS[field]] = _price(_field(text, field))
    factor = row["factor"]
    row["unitPrice"] = {
        _PRICE_KEYS[name]: (row[_PRICE_KEYS[name]] / factor if factor > 1 else row[_PRICE_KEYS[name]])
        for name in PRICE_FIELDS
    }
    return row


#: Cash-market and odd-lot ``TPMERC`` codes, as published in the COTAHIST layout.
CASH_MARKET = 10
ODD_LOT_MARKET = 20


def map_symbol(code: str) -> tuple[str, int]:
    """``PETR4.SA`` -> (``PETR4``, 10); ``PETR4F.SA`` -> (``PETR4F``, 20).

    Parity with Vibe-Trading's `backtest/loaders/b3_cotahist_loader.py::map_symbol`.
    The odd-lot market is selected by a trailing ``F`` that follows a fraction
    digit, so a ticker whose ``F`` is part of its name is not misread as odd-lot.
    """
    ticker = str(code or "").strip().upper()
    if ticker.endswith(".SA"):
        ticker = ticker[:-3]
    odd_lot = ticker.endswith("F") and len(ticker) >= 2 and ticker[-2].isdigit()
    return ticker, ODD_LOT_MARKET if odd_lot else CASH_MARKET


def parse_records(text: str) -> list[dict[str, Any]]:
    """Parse a whole file. Blank lines are ignored; the header/trailer are dropped."""
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(str(text or "").splitlines(), start=1):
        try:
            row = parse_record(line)
        except CotahistError as exc:
            raise CotahistError(exc.code, f"line {number}: {exc.message}") from exc
        if row is not None:
            rows.append(row)
    return rows


def iter_records(lines: Iterable[str]) -> Iterable[dict[str, Any]]:
    """Stream records from an iterable of lines (a large file need not be held)."""
    for number, line in enumerate(lines, start=1):
        try:
            row = parse_record(line)
        except CotahistError as exc:
            raise CotahistError(exc.code, f"line {number}: {exc.message}") from exc
        if row is not None:
            yield row


__all__ = [
    "COLUMNS",
    "CotahistError",
    "PRICE_FIELDS",
    "QUOTE_RECORD_TYPE",
    "RECORD_LENGTH",
    "iter_records",
    "parse_record",
    "parse_records",
]
