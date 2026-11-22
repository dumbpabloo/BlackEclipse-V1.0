"""Deterministic pre-trade limits for demo execution (P1).

The broker's own `order_check` proves the order is *possible*; this policy decides
whether it is *allowed*. Every rule is a plain number from the connection config,
so an operator can explain a refusal without reading code.

Defaults are deliberately visible and conservative where they cost nothing:
`maxOpenPositions=20`, `maxSpreadPoints=50`. A value of 0 (or an empty allowlist)
turns that rule off. P1 is demo-only; live arming is a later phase.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .model import BrokerConnection


@dataclass(frozen=True)
class TradePolicy:
    max_margin: float = 0.0
    max_volume: float = 0.0
    max_open_positions: int = 20
    max_spread_points: float = 50.0
    symbol_allowlist: tuple[str, ...] = ()
    #: Percent of account equity a single order may risk at its stop (0 = off).
    #: When set, an order without a stop is refused: undefined risk is not small.
    risk_per_trade_pct: float = 0.0
    #: Absolute notional cap for one symbol (account currency, 0 = off).
    max_symbol_notional: float = 0.0
    #: Rolling 24 h loss brake (percent of equity, 0 = off). Enforced in the
    #: service because it needs the broker's own deal history.
    max_daily_loss_pct: float = 0.0

    @classmethod
    def from_connection(cls, connection: BrokerConnection) -> "TradePolicy":
        config = connection.config or {}

        def number(key: str, default: float) -> float:
            try:
                return float(config.get(key, default) or default)
            except (TypeError, ValueError):
                return default

        def integer(key: str, default: int) -> int:
            try:
                return int(config.get(key, default) or default)
            except (TypeError, ValueError):
                return default

        allowlist = config.get("symbolAllowlist") or ()
        if not isinstance(allowlist, (list, tuple)):
            allowlist = ()
        return cls(
            max_margin=number("maxMargin", 0.0),
            max_volume=number("maxVolume", 0.0),
            max_open_positions=integer("maxOpenPositions", 20),
            max_spread_points=number("maxSpreadPoints", 50.0),
            symbol_allowlist=tuple(str(item).strip().upper() for item in allowlist if str(item).strip()),
            risk_per_trade_pct=number("riskPerTradePct", 0.0),
            max_symbol_notional=number("maxSymbolNotional", 0.0),
            max_daily_loss_pct=number("maxDailyLossPct", 0.0),
        )

    def evaluate(self, preview: dict[str, Any]) \
            -> dict[str, Any]:
        """Return `{allowed, reasons, rules}` for one broker preview."""
        reasons: list[str] = []
        rules: list[str] = []
        symbol = str(preview.get("symbol") or "").upper()
        if self.symbol_allowlist and symbol not in self.symbol_allowlist:
            rules.append("policy.symbol-not-allowed")
            reasons.append(
                f"{symbol or 'this symbol'} is not on the connection's symbol allowlist."
            )

        margin = _num(preview.get("margin"))
        if self.max_margin > 0:
            if margin is None:
                # Fail closed, as the reference mandate gate does: a configured cap
                # that cannot be verified is not a cap. Silence here would let a
                # broker preview that omits margin walk past the limit.
                rules.append("policy.max-margin-unverified")
                reasons.append(
                    "The order's margin could not be read, so the "
                    f"{self.max_margin:,.2f} margin cap cannot be verified; refusing."
                )
            elif margin > self.max_margin:
                rules.append("policy.max-margin")
                reasons.append(
                    f"The order needs {margin:,.2f} margin, above the {self.max_margin:,.2f} cap."
                )

        volume = _num(preview.get("volume"))
        if self.max_volume > 0:
            if volume is None:
                rules.append("policy.max-volume-unverified")
                reasons.append(
                    "The order's volume could not be read, so the "
                    f"{self.max_volume:g} volume cap cannot be verified; refusing."
                )
            elif volume > self.max_volume:
                rules.append("policy.max-volume")
                reasons.append(
                    f"Volume {volume:g} exceeds the {self.max_volume:g} cap."
                )

        spread = _num(preview.get("spreadPoints"))
        if self.max_spread_points > 0 and spread is not None and spread > self.max_spread_points:
            rules.append("policy.max-spread")
            reasons.append(
                f"The spread is {spread:g} points, above the {self.max_spread_points:g}-point cap."
            )

        open_positions = _num(preview.get("openPositions"))
        if self.max_open_positions > 0:
            if open_positions is None:
                rules.append("policy.max-open-positions-unverified")
                reasons.append(
                    "The open-position count could not be read, so the "
                    f"{self.max_open_positions}-position cap cannot be verified; refusing."
                )
            elif open_positions >= self.max_open_positions:
                rules.append("policy.max-open-positions")
                reasons.append(
                    f"There are already {int(open_positions)} open positions "
                    f"(cap {self.max_open_positions})."
                )

        notional = _num(preview.get("notional"))
        if self.max_symbol_notional > 0:
            if notional is None:
                rules.append("policy.max-symbol-notional-unverified")
                reasons.append(
                    "The order's notional could not be read, so the "
                    f"{self.max_symbol_notional:,.2f} per-symbol cap cannot be "
                    "verified; refusing."
                )
            elif notional > self.max_symbol_notional:
                rules.append("policy.max-symbol-notional")
                reasons.append(
                    f"The order's {symbol or 'symbol'} notional is {notional:,.2f}, above "
                    f"the {self.max_symbol_notional:,.2f} per-symbol cap."
                )

        if self.risk_per_trade_pct > 0:
            account = preview.get("account") or {}
            equity = _num(account.get("equity"))
            price = _num(preview.get("price"))
            stop = _num(preview.get("stopLoss"))
            contract = _num(preview.get("contractSize"))
            units = (volume or 0.0) * (contract or 0.0)
            if equity and equity > 0:
                if not stop or not price or not units:
                    rules.append("policy.risk-stop-required")
                    reasons.append(
                        "A stop loss is required: the connection has a "
                        f"{self.risk_per_trade_pct:g}% risk-per-trade cap and this "
                        "order has no defined risk."
                    )
                else:
                    risk = abs(price - stop) * units
                    cap = equity * self.risk_per_trade_pct / 100.0
                    if risk > cap + 1e-9:
                        rules.append("policy.risk-per-trade")
                        reasons.append(
                            f"The order risks {risk:,.2f} at its stop, above the "
                            f"{self.risk_per_trade_pct:g}% cap ({cap:,.2f} of "
                            f"{equity:,.2f} equity)."
                        )

        return {"allowed": not reasons, "reasons": reasons, "rules": rules}


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


__all__ = ["TradePolicy"]
