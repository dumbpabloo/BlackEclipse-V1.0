"""Realized-gains reporting by tax year, with wash-sale *candidates*.

This is a REPORT over the engine's lot matches. It never rewrites realized P&L
and never applies a jurisdiction's rule: a wash sale is reported as a candidate
with the replacement purchase it found, so a user can take it to a tax adviser.
The engine's job is to show the facts and name the assumption, not to file.

Multi-currency honesty: gains are grouped by tax year AND currency. Summing a
EUR gain and a USD gain into one number would be a fabricated figure.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Iterable, Mapping, Optional, Sequence

from .money import D, ZERO, money_str

SHORT = "SHORT"
LONG = "LONG"
UNKNOWN = "UNKNOWN"


def _parse_day(value: Any) -> Optional[date]:
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def _term(holding_days: Optional[int], long_term_days: int) -> str:
    if holding_days is None:
        return UNKNOWN
    return LONG if holding_days >= long_term_days else SHORT


def _replacements(
    symbol: str,
    disposed: Optional[date],
    acquisitions: Sequence[Mapping[str, Any]],
    *,
    window_days: int,
    sell_transaction_id: Optional[str],
) -> list[dict[str, Any]]:
    if disposed is None or window_days < 0:
        return []
    found: list[dict[str, Any]] = []
    for acquisition in acquisitions:
        if str(acquisition.get("symbol") or "").upper() != symbol.upper():
            continue
        acquisition_id = str(acquisition.get("id") or "")
        if sell_transaction_id and acquisition_id == str(sell_transaction_id):
            continue
        acquired = _parse_day(acquisition.get("date"))
        if acquired is None:
            continue
        if abs((acquired - disposed).days) <= window_days:
            found.append({
                "transactionId": acquisition_id or None,
                "type": acquisition.get("type"),
                "date": acquired.isoformat(),
                "quantity": str(acquisition.get("quantity") or ""),
                "daysFromDisposal": (acquired - disposed).days,
            })
    found.sort(key=lambda row: abs(int(row["daysFromDisposal"])))
    return found


def build_realized_gains_report(
    *,
    portfolio_id: str,
    method: str,
    base_currency: str,
    matches: Iterable[Mapping[str, Any]],
    acquisitions: Sequence[Mapping[str, Any]] = (),
    currency_by_symbol: Optional[Mapping[str, str]] = None,
    tax_year: Optional[int] = None,
    long_term_days: int = 365,
    wash_sale_window_days: int = 30,
    include_wash_sale: bool = True,
) -> dict[str, Any]:
    """Build a deterministic report from the engine's realized lot matches.

    `matches` are `TaxLotMatch.to_dict()` rows. `acquisitions` are transactions
    that acquired the same symbol (`id`, `symbol`, `date`, `quantity`, `type`).
    """
    currencies = {str(k).upper(): str(v).upper() for k, v in (currency_by_symbol or {}).items()}
    long_term_days = max(1, int(long_term_days))
    window = max(0, int(wash_sale_window_days))

    disposals: list[dict[str, Any]] = []
    for match in matches:
        disposed_at = str(match.get("disposedAt") or "")
        year = disposed_at[:4] if len(disposed_at) >= 4 and disposed_at[:4].isdigit() else "UNKNOWN"
        if tax_year is not None and year != str(tax_year):
            continue
        symbol = str(match.get("symbol") or "").upper()
        currency = currencies.get(symbol, base_currency.upper())
        holding_days = match.get("holdingDays")
        holding = int(holding_days) if isinstance(holding_days, int) else None
        gain = D(match.get("realizedPnl"))
        replacements = (
            _replacements(symbol, _parse_day(disposed_at), acquisitions,
                          window_days=window, sell_transaction_id=match.get("sellTransactionId"))
            if include_wash_sale and gain < ZERO else []
        )
        disposals.append({
            "id": match.get("id"),
            "symbol": symbol,
            "currency": currency,
            "taxYear": year,
            "term": _term(holding, long_term_days),
            "holdingDays": holding,
            "acquiredAt": match.get("acquiredAt"),
            "disposedAt": disposed_at,
            "quantity": str(match.get("quantity") or ""),
            "proceeds": money_str(D(match.get("proceeds"))),
            "costBasis": money_str(D(match.get("costBasis"))),
            "realizedPnl": money_str(gain),
            "washSaleSuspected": bool(replacements),
            "replacementTransactions": replacements,
            # A candidate only: the full loss is shown, never silently netted.
            "candidateDisallowedLoss": money_str(abs(gain)) if replacements else None,
        })

    disposals.sort(key=lambda row: (row["disposedAt"], row["symbol"], str(row["id"])))

    years: dict[tuple[str, str], dict[str, Any]] = {}
    for row in disposals:
        key = (row["taxYear"], row["currency"])
        bucket = years.setdefault(key, {
            "taxYear": row["taxYear"],
            "currency": row["currency"],
            "shortTermGain": ZERO,
            "longTermGain": ZERO,
            "unknownTermGain": ZERO,
            "washSaleCandidateLoss": ZERO,
            "disposals": 0,
            "washSaleCandidates": 0,
        })
        gain = D(row["realizedPnl"])
        if row["term"] == SHORT:
            bucket["shortTermGain"] += gain
        elif row["term"] == LONG:
            bucket["longTermGain"] += gain
        else:
            bucket["unknownTermGain"] += gain
        bucket["disposals"] += 1
        if row["washSaleSuspected"]:
            bucket["washSaleCandidates"] += 1
            bucket["washSaleCandidateLoss"] += abs(gain)

    year_rows = []
    for bucket in years.values():
        bucket["totalGain"] = (
            bucket["shortTermGain"] + bucket["longTermGain"] + bucket["unknownTermGain"]
        )
        year_rows.append({
            "taxYear": bucket["taxYear"],
            "currency": bucket["currency"],
            "shortTermGain": money_str(bucket["shortTermGain"]),
            "longTermGain": money_str(bucket["longTermGain"]),
            "unknownTermGain": money_str(bucket["unknownTermGain"]),
            "totalGain": money_str(bucket["totalGain"]),
            "washSaleCandidateLoss": money_str(bucket["washSaleCandidateLoss"]),
            "disposals": bucket["disposals"],
            "washSaleCandidates": bucket["washSaleCandidates"],
        })
    year_rows.sort(key=lambda row: (row["taxYear"], row["currency"]), reverse=True)

    totals: dict[str, dict[str, Decimal]] = {}
    for row in disposals:
        bucket = totals.setdefault(row["currency"], {"gain": ZERO, "candidateLoss": ZERO})
        bucket["gain"] += D(row["realizedPnl"])
        if row["washSaleSuspected"]:
            bucket["candidateLoss"] += abs(D(row["realizedPnl"]))
    total_rows = [
        {
            "currency": currency,
            "realizedPnl": money_str(bucket["gain"]),
            "washSaleCandidateLoss": money_str(bucket["candidateLoss"]),
        }
        for currency, bucket in sorted(totals.items())
    ]

    return {
        "portfolioId": portfolio_id,
        "method": method,
        "baseCurrency": base_currency.upper(),
        "taxYear": tax_year,
        "longTermDays": long_term_days,
        "washSale": {
            "enabled": bool(include_wash_sale),
            "windowDays": window,
            "applied": False,
            "note": (
                "Candidates only. The engine does not apply a jurisdiction's "
                "wash-sale rule, adjust replacement-lot basis, or disallow a loss. "
                "A candidate means a same-symbol acquisition was found within the "
                "window; verify with a tax adviser."
            ),
        },
        "years": year_rows,
        "totalsByCurrency": total_rows,
        "disposals": disposals,
    }
