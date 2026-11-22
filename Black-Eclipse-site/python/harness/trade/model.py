"""Broker-connection contracts.

These are DATA, not policy. A connection names a broker adapter kind, a mode and
the non-secret settings needed to reach it. Credentials are never part of this
shape: the preferred MetaTrader path attaches to a terminal the operator already
logged into, so no password ever enters Eclipse.

Anything that looks like a secret is refused by `validate_config`, not stored and
redacted later.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

KIND_MT5 = "mt5"
KIND_MT4_BRIDGE = "mt4_bridge"
KIND_ALPACA = "alpaca"
KIND_IBKR = "ibkr"
KIND_TRADING212 = "trading212"
KIND_ZERODHA = "zerodha"
KIND_TRADIER = "tradier"
KIND_OKX = "okx"
KIND_BINANCE = "binance"
KIND_KRAKEN = "kraken"
KIND_COINBASE = "coinbase"
KIND_BYBIT = "bybit"
KIND_DERIBIT = "deribit"
KIND_OANDA = "oanda"
KIND_BITGET = "bitget"
KIND_GATEIO = "gateio"
KIND_BITSTAMP = "bitstamp"
KIND_COINCHECK = "coincheck"
KIND_BITFINEX = "bitfinex"


class ConnectionMode(str, Enum):
    DEMO = "demo"
    LIVE = "live"
    UNKNOWN = "unknown"


#: Keys that may never appear in a connection config (recursively). A credential
#: VALUE is never accepted anywhere in this layer; only a vault secret NAME is.
SECRET_KEYS: frozenset[str] = frozenset({
    "password", "passwd", "pwd", "secret", "token", "access_token",
    "refresh_token", "api_key", "apikey", "api_secret", "private_key",
    "credential", "credentials", "session", "cookie",
})

#: Non-secret fields a connection config may carry. Adapters ignore unknown
#: keys, but the store only persists this set so a typo cannot silently vanish.
ALLOWED_CONFIG_KEYS: frozenset[str] = frozenset({
    "terminalPath", "portable", "login", "server", "magic", "symbolMap",
    "symbols", "bridgeUrl", "bridgeSecretRef", "baseUrl", "dataUrl", "accountId",
    "host", "port", "clientId", "credentialRef", "notes",
    # Terminal launch policy: attach-only unless autoLaunchTerminal is true; the
    # post-order focus can be switched off with openTerminalOnTrade: false.
    "autoLaunchTerminal", "openTerminalOnTrade",
    # P1 demo-execution limits (all optional; 0/empty = off).
    "maxMargin", "maxVolume", "maxOpenPositions", "maxSpreadPoints",
    "symbolAllowlist",
    # Risk engine (all optional; 0 = off).
    "riskPerTradePct", "maxDailyLossPct", "maxSymbolNotional",
})


class BrokerConfigError(ValueError):
    """A connection config the operator must fix."""


def _normalise_key(key: Any) -> str:
    return str(key or "").strip().lower().replace("-", "_").replace(" ", "_")


_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off"})


def _as_bool(name: str, value: Any) -> bool:
    """Booleans stay booleans: the old string fallback made `false` truthy."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    raise BrokerConfigError(f"{name} must be true or false")


def validate_config(config: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Return a cleaned config, refusing secrets and unknown keys."""
    if config is None:
        return {}
    if not isinstance(config, dict):
        raise BrokerConfigError("connection config must be an object")

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if _normalise_key(key) in SECRET_KEYS:
                    raise BrokerConfigError(
                        f"{path}{key!s} looks like a credential; "
                        "store a secret NAME, never a value"
                    )
                walk(child, f"{path}{key!s}.")
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}].")

    walk(config, "")
    cleaned: dict[str, Any] = {}
    for key, value in config.items():
        name = str(key)
        if name not in ALLOWED_CONFIG_KEYS:
            raise BrokerConfigError(
                f"unknown connection setting {name!r}; allowed: "
                f"{', '.join(sorted(ALLOWED_CONFIG_KEYS))}"
            )
        if value is None:
            continue
        if name == "magic":
            try:
                cleaned[name] = int(value)
            except (TypeError, ValueError) as exc:
                raise BrokerConfigError("magic must be an integer") from exc
        elif name == "port":
            try:
                cleaned[name] = int(value)
            except (TypeError, ValueError) as exc:
                raise BrokerConfigError("port must be an integer") from exc
        elif name in ("portable", "autoLaunchTerminal", "openTerminalOnTrade"):
            cleaned[name] = _as_bool(name, value)
        elif name in ("symbolMap", "symbols"):
            if not isinstance(value, (dict, list)):
                raise BrokerConfigError(f"{name} must be an object or list")
            cleaned[name] = value
        elif name == "symbolAllowlist":
            if not isinstance(value, (list, tuple)):
                raise BrokerConfigError("symbolAllowlist must be a list")
            cleaned[name] = [str(item).strip().upper() for item in value if str(item).strip()]
        elif name in ("maxMargin", "maxVolume", "maxSpreadPoints"):
            try:
                cleaned[name] = float(value)
            except (TypeError, ValueError) as exc:
                raise BrokerConfigError(f"{name} must be a number") from exc
        elif name == "maxOpenPositions":
            try:
                cleaned[name] = int(value)
            except (TypeError, ValueError) as exc:
                raise BrokerConfigError("maxOpenPositions must be an integer") from exc
        else:
            cleaned[name] = str(value)
    return cleaned


def normalise_mode(value: Any) -> ConnectionMode:
    if isinstance(value, ConnectionMode):
        return value
    raw = str(getattr(value, "value", value) or "").strip().lower()
    for mode in ConnectionMode:
        if mode.value == raw:
            return mode
    return ConnectionMode.UNKNOWN


# ── Order intents (P1 demo execution) ────────────────────────────────────────
ORDER_SIDES = ("buy", "sell")
ORDER_TYPES = ("market", "limit", "stop")
ORDER_TIFS = ("gtc", "day")
MAX_COMMENT = 31  # MetaTrader's comment field


class TradeRefused(Exception):
    """A structural refusal the caller must see (live off, bad approval, …)."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        detail: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        # Optional structured evidence for the caller (e.g. the proof report).
        self.detail = detail or {}

    def to_dict(self) -> dict[str, Any]:
        payload = {"code": self.code, "message": self.message}
        if self.detail:
            payload["detail"] = self.detail
        return payload


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


@dataclass
class OrderIntent:
    """One exact order. The digest binds an approval to these bytes only."""

    connection_id: str
    symbol: str
    side: str
    order_type: str
    volume: float
    price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    deviation_points: int = 20
    time_in_force: str = "gtc"
    magic: int = 0
    comment: str = "eclipse"

    def normalised(self) -> dict[str, Any]:
        return {
            "connectionId": self.connection_id,
            "symbol": self.symbol,
            "side": self.side,
            "type": self.order_type,
            "volume": round(float(self.volume), 8),
            "price": self.price,
            "stopLoss": self.stop_loss,
            "takeProfit": self.take_profit,
            "deviationPoints": self.deviation_points,
            "timeInForce": self.time_in_force,
            "magic": self.magic,
            "comment": self.comment,
        }

    @property
    def digest(self) -> str:
        canonical = json.dumps(self.normalised(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def request_key(self) -> str:
        return f"trade:{self.connection_id}"

    def to_dict(self) -> dict[str, Any]:
        return self.normalised()

    @classmethod
    def from_payload(
        cls, payload: dict[str, Any], *, connection_id: str, magic_default: int = 0,
    ) -> "OrderIntent":
        if not isinstance(payload, dict):
            raise TradeRefused("INVALID_ORDER", "an order intent object is required")
        symbol = str(payload.get("symbol") or "").strip()
        if not symbol:
            raise TradeRefused("INVALID_ORDER", "the order needs a symbol")
        side = str(payload.get("side") or "").strip().lower()
        if side not in ORDER_SIDES:
            raise TradeRefused("INVALID_ORDER", "side must be buy or sell")
        order_type = str(payload.get("type") or payload.get("orderType") or "market").strip().lower()
        if order_type not in ORDER_TYPES:
            raise TradeRefused("INVALID_ORDER", "type must be market, limit or stop")
        volume = _finite(payload.get("volume"))
        if volume is None or volume <= 0:
            raise TradeRefused("INVALID_ORDER", "volume must be a positive number")
        price = _finite(payload.get("price"))
        if order_type in ("limit", "stop") and (price is None or price <= 0):
            raise TradeRefused("INVALID_ORDER", f"a {order_type} order needs a positive price")
        if order_type == "market":
            price = None
        stop_loss = _finite(payload.get("stopLoss"))
        take_profit = _finite(payload.get("takeProfit"))
        for label, value in (("stopLoss", stop_loss), ("takeProfit", take_profit)):
            if value is not None and value <= 0:
                raise TradeRefused("INVALID_ORDER", f"{label} must be positive or omitted")
        try:
            deviation = int(payload.get("deviationPoints", 20) or 0)
        except (TypeError, ValueError) as exc:
            raise TradeRefused("INVALID_ORDER", "deviationPoints must be an integer") from exc
        if not 0 <= deviation <= 1000:
            raise TradeRefused("INVALID_ORDER", "deviationPoints must be 0..1000")
        tif = str(payload.get("timeInForce") or "gtc").strip().lower()
        if tif not in ORDER_TIFS:
            raise TradeRefused("INVALID_ORDER", "timeInForce must be gtc or day")
        raw_magic = payload.get("magic")
        if raw_magic is None or raw_magic == "":
            raw_magic = magic_default
        try:
            magic = int(raw_magic)
        except (TypeError, ValueError) as exc:
            raise TradeRefused("INVALID_ORDER", "magic must be an integer") from exc
        if magic < 0:
            raise TradeRefused("INVALID_ORDER", "magic cannot be negative")
        comment = str(payload.get("comment") or "eclipse").strip()[:MAX_COMMENT] or "eclipse"
        return cls(
            connection_id=connection_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            volume=volume,
            price=price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            deviation_points=deviation,
            time_in_force=tif,
            magic=magic,
            comment=comment,
        )


@dataclass
class BrokerConnection:
    id: str
    kind: str
    label: str
    mode: ConnectionMode = ConnectionMode.UNKNOWN
    config: dict[str, Any] = field(default_factory=dict)
    status: str = "unconfigured"
    status_reason: Optional[str] = None
    account: Optional[dict[str, Any]] = None
    armed_until: Optional[str] = None
    armed_at: Optional[str] = None
    armed_by: Optional[str] = None
    last_seen_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "mode": self.mode.value if isinstance(self.mode, ConnectionMode) else self.mode,
            "config": dict(self.config),
            "status": self.status,
            "statusReason": self.status_reason,
            "account": self.account,
            "armedUntil": self.armed_until,
            "armedAt": self.armed_at,
            "armedBy": self.armed_by,
            "lastSeenAt": self.last_seen_at,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }


@dataclass
class BrokerKind:
    """One adapter kind and what it can do. No connection, no side effect."""

    id: str
    label: str
    auth: str  # attach | credentials | oauth | bridge
    modes: tuple[str, ...] = ("demo", "live")
    asset_classes: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ("account", "positions", "orders", "quote")
    execution: bool = False
    execution_modes: tuple[str, ...] = ()
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "auth": self.auth,
            "modes": list(self.modes),
            "assetClasses": list(self.asset_classes),
            "capabilities": list(self.capabilities),
            "execution": bool(self.execution or self.execution_modes),
            "executionModes": list(self.execution_modes),
            "notes": self.notes,
        }


__all__ = [
    "ALLOWED_CONFIG_KEYS",
    "BrokerConfigError",
    "BrokerConnection",
    "BrokerKind",
    "ConnectionMode",
    "KIND_ALPACA",
    "KIND_IBKR",
    "KIND_MT4_BRIDGE",
    "KIND_MT5",
    "KIND_TRADING212",
    "KIND_ZERODHA",
    "KIND_TRADIER",
    "KIND_OKX",
    "KIND_BINANCE",
    "KIND_KRAKEN",
    "KIND_COINBASE",
    "KIND_BYBIT",
    "KIND_DERIBIT",
    "KIND_OANDA",
    "KIND_BITGET",
    "KIND_GATEIO",
    "KIND_BITSTAMP",
    "KIND_COINCHECK",
    "KIND_BITFINEX",
    "MAX_COMMENT",
    "ORDER_SIDES",
    "ORDER_TIFS",
    "ORDER_TYPES",
    "OrderIntent",
    "SECRET_KEYS",
    "TradeRefused",
    "normalise_mode",
    "validate_config",
]
