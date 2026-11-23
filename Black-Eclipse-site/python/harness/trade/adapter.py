"""The broker adapter contract.

Every broker — MetaTrader 5's local IPC, an MT4 bridge EA, a REST broker — is
reduced to these read operations in P0. Adding a broker is implementing this
class and registering it; no caller changes.

`probe()` must never claim availability it cannot prove. A missing package, a
missing terminal, a logged-out terminal and a disabled Algo Trading switch are
distinct, named reasons.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional


class AdapterUnavailable(RuntimeError):
    """A broker cannot be reached, with a machine-readable code and a reason."""

    def __init__(self, code: str, reason: str, *, detail: Optional[dict[str, Any]] = None) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.detail = detail or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.reason, **self.detail}


class BrokerAdapter(ABC):
    """Read-only broker surface for P0.

    Implementations open a short-lived session per call and close it. They must
    be safe to construct without a reachable broker (construction never connects).
    """

    kind: str = ""

    def __init__(self, config: Optional[dict[str, Any]] = None) -> None:
        self.config = dict(config or {})

    @classmethod
    @abstractmethod
    def availability(cls) -> tuple[bool, Optional[str]]:
        """Whether this adapter's dependency is installed on this host.

        Returns `(available, reason)`. This is a cheap check (package + platform),
        never a network or terminal call.
        """

    @abstractmethod
    def probe(self) -> dict[str, Any]:
        """Test the connection and return an honest, structured status."""

    @abstractmethod
    def account(self) -> dict[str, Any]:
        ...

    @abstractmethod
    def positions(self) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    def orders(self) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    def quote(self, symbol: str) -> dict[str, Any]:
        ...

    def symbol_info(self, symbol: str) -> dict[str, Any]:
        """Static symbol facts (no tick). Default: this broker cannot report them."""
        raise AdapterUnavailable(
            "SYMBOL_INFO_NOT_SUPPORTED",
            f"{self.kind or 'this broker'} cannot report symbol information",
        )

    def deals_since(self, since_ms: int) -> list[dict[str, Any]]:
        """Deals at/after `since_ms` (server epoch ms). Default: unsupported.

        History is a READ capability, not execution, so a read-only adapter may
        implement it without claiming an order path.
        """
        return []

    def deals_window(self, days: int) -> list[dict[str, Any]]:
        """Deals from the last `days` days, ignoring any local cursor."""
        return self.deals_since(0)

    def close(self) -> None:
        """Release any shared session. Safe to call more than once."""


class TradingAdapter(BrokerAdapter):
    """The P1 execution extension (demo-only until the live gates land).

    `order_preview` must be side-effect free: normalize the volume, validate the
    symbol/stops/filling mode and run the broker's own `order_check`. It must not
    call `order_send`. `submit_order` re-validates and sends exactly once; it never
    retries an ambiguous result.

    Identifiers are `Ticket` values (`str` or `int`): MetaTrader uses integer
    tickets, REST venues use opaque ids. An adapter must accept the id it
    returned in `orders()`/`positions()` and act on exactly that object.
    """

    @abstractmethod
    def order_preview(self, intent: Any) -> dict[str, Any]:
        ...

    @abstractmethod
    def submit_order(self, intent: Any) -> dict[str, Any]:
        ...

    @abstractmethod
    def cancel_order(self, ticket: Any) -> dict[str, Any]:
        ...

    @abstractmethod
    def cancel_all(self, magic: int) -> dict[str, Any]:
        """Cancel every working order that carries `magic` (never a blind all)."""

    def close_position(self, ticket: Any, deviation: int = 20, volume: float | None = None) -> dict[str, Any]:
        """Close one open position. Default: this broker has no close path.

        Closing is risk-reducing, so it needs no trade approval — but it is still
        a broker mutation and the service only calls it on a demo connection.
        """
        raise AdapterUnavailable(
            "CLOSE_NOT_SUPPORTED",
            f"{self.kind or 'this broker'} cannot close positions",
        )

    def modify_position(
        self, ticket: Any, stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> dict[str, Any]:
        """Move a position's stop/target. Default: this broker cannot modify."""
        raise AdapterUnavailable(
            "MODIFY_NOT_SUPPORTED",
            f"{self.kind or 'this broker'} cannot modify positions",
        )

    def modify_order(
        self, ticket: Any, price: float | None = None,
        stop_loss: float | None = None, take_profit: float | None = None,
    ) -> dict[str, Any]:
        """Change a working (pending) order. Default: this broker cannot modify orders."""
        raise AdapterUnavailable(
            "ORDER_MODIFY_NOT_SUPPORTED",
            f"{self.kind or 'this broker'} cannot modify working orders",
        )

    def order_status(self, ticket: Any) -> dict[str, Any]:
        """The broker's current/historical state for one order ticket."""
        return {"found": False}


__all__ = ["AdapterUnavailable", "BrokerAdapter", "TradingAdapter"]
