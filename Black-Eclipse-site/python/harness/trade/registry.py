"""Broker-kind registry: the one place a new broker is declared.

A kind is a descriptor plus a factory. The descriptor is what the UI and the
agent can see; the factory is only called when a saved connection is actually
used. `list_kinds()` reports dependency availability without touching a broker.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from .adapter import BrokerAdapter
from .model import (
    KIND_ALPACA,
    KIND_BINANCE,
    KIND_BITFINEX,
    KIND_BITGET,
    KIND_BITSTAMP,
    KIND_BYBIT,
    KIND_COINBASE,
    KIND_COINCHECK,
    KIND_DERIBIT,
    KIND_GATEIO,
    KIND_IBKR,
    KIND_KRAKEN,
    KIND_MT4_BRIDGE,
    KIND_MT5,
    KIND_OANDA,
    KIND_OKX,
    KIND_TRADING212,
    KIND_TRADIER,
    KIND_ZERODHA,
    BrokerKind,
)

_FACTORIES: dict[str, Callable[[dict[str, Any]], BrokerAdapter]] = {}
_KINDS: dict[str, BrokerKind] = {}


def register_kind(
    kind: BrokerKind,
    factory: Callable[[dict[str, Any]], BrokerAdapter],
    *,
    replace: bool = False,
) -> None:
    if kind.id in _KINDS and not replace:
        raise ValueError(f"broker kind {kind.id!r} is already registered")
    _KINDS[kind.id] = kind
    _FACTORIES[kind.id] = factory


def get_kind(kind_id: str) -> BrokerKind:
    kind = _KINDS.get(str(kind_id or "").strip().lower())
    if kind is None:
        raise KeyError(
            f"unknown broker kind {kind_id!r}; Eclipse knows "
            f"{', '.join(sorted(_KINDS)) or 'none'}"
        )
    return kind


def list_kinds() -> list[dict[str, Any]]:
    """Every kind plus whether its dependency is installed on this host."""
    out: list[dict[str, Any]] = []
    for kind in _KINDS.values():
        payload = kind.to_dict()
        factory = _FACTORIES[kind.id]
        try:
            available, reason = factory({}).availability()
        except Exception as exc:  # noqa: BLE001 - availability must never raise
            available, reason = False, f"availability check failed: {exc}"
        payload["available"] = bool(available)
        payload["unavailableReason"] = None if available else (reason or "unavailable")
        out.append(payload)
    return out


def make_adapter(kind_id: str, config: Optional[dict[str, Any]] = None) -> BrokerAdapter:
    kind = get_kind(kind_id)
    return _FACTORIES[kind.id](dict(config or {}))


def _register_builtins() -> None:
    from .adapters.alpaca import AlpacaAdapter  # noqa: PLC0415 - avoids import cycles
    from .adapters.bitfinex import BitfinexAdapter  # noqa: PLC0415 - avoids import cycles
    from .adapters.bitget import BitgetAdapter  # noqa: PLC0415 - avoids import cycles
    from .adapters.bitstamp import BitstampAdapter  # noqa: PLC0415 - avoids import cycles
    from .adapters.bybit import BybitAdapter  # noqa: PLC0415 - avoids import cycles
    from .adapters.coinbase import CoinbaseAdapter  # noqa: PLC0415 - avoids import cycles
    from .adapters.coincheck import CoincheckAdapter  # noqa: PLC0415 - avoids import cycles
    from .adapters.deribit import DeribitAdapter  # noqa: PLC0415 - avoids import cycles
    from .adapters.binance import BinanceAdapter  # noqa: PLC0415 - avoids import cycles
    from .adapters.gateio import GateioAdapter  # noqa: PLC0415 - avoids import cycles
    from .adapters.ibkr import IBKRAdapter  # noqa: PLC0415 - avoids import cycles
    from .adapters.kraken import KrakenAdapter  # noqa: PLC0415 - avoids import cycles
    from .adapters.mt4_bridge import MT4BridgeAdapter  # noqa: PLC0415 - avoids import cycles
    from .adapters.mt5 import MT5Adapter  # noqa: PLC0415 - avoids import cycles
    from .adapters.oanda import OandaAdapter  # noqa: PLC0415 - avoids import cycles
    from .adapters.okx import OkxAdapter  # noqa: PLC0415 - avoids import cycles
    from .adapters.trading212 import Trading212Adapter  # noqa: PLC0415 - avoids import cycles
    from .adapters.tradier import TradierAdapter  # noqa: PLC0415 - avoids import cycles
    from .adapters.zerodha import ZerodhaAdapter  # noqa: PLC0415 - avoids import cycles

    register_kind(
        BrokerKind(
            id=KIND_ALPACA,
            label="Alpaca",
            auth="credentials",
            modes=("demo", "live"),
            asset_classes=("stocks", "etf", "crypto"),
            capabilities=("account", "positions", "orders", "quote"),
            execution=True,
            execution_modes=("demo", "live"),
            notes=(
                "US equities/ETFs/crypto via Alpaca's REST API. Configure a "
                "credentialRef (a secret NAME) containing KEY:SECRET. Market, "
                "limit and stop entries; bracket/OTO; cancel, replace and "
                "close-by-symbol. A paper host is the demo account. Journal P&L "
                "is FIFO-computed from filled orders."
            ),
        ),
        AlpacaAdapter,
    )

    register_kind(
        BrokerKind(
            id=KIND_IBKR,
            label="Interactive Brokers",
            auth="credentials",
            modes=("demo", "live"),
            asset_classes=("stocks", "etf", "options", "futures", "fx"),
            capabilities=("account", "positions", "orders", "quote"),
            execution=True,
            execution_modes=("demo", "live"),
            notes=(
                "Execution via the local Client Portal Gateway (CPAPI). "
                "credentialRef names the gateway session cookie. Market/limit/stop "
                "orders, cancel, price replace and close-by-conid. The gateway's "
                "confirmation question is answered once; a second question is "
                "refused. Bracket legs and cancel-all have no CPAPI order tag and "
                "are named refusals. Journal P&L is FIFO-computed from executions."
            ),
        ),
        IBKRAdapter,
    )

    register_kind(
        BrokerKind(
            id=KIND_MT4_BRIDGE,
            label="MetaTrader 4 (bridge)",
            auth="bridge",
            modes=("demo", "live"),
            asset_classes=("fx", "cfd", "futures", "stocks", "metals"),
            capabilities=("account", "positions", "orders", "quote"),
            execution=True,
            execution_modes=("demo", "live"),
            notes=(
                "Attaches to a local HTTP bridge that already runs next to MT4; "
                "Eclipse never stores a password. Reads use GET /account, "
                "/positions, /orders, /quote, /deals; writes use POST /orders, "
                "/orders/{t}/cancel, /orders/{t}/modify, /positions/{t}/close, "
                "/positions/{t}/modify and GET /orders/{t}/status. A bridge that "
                "does not implement a write returns BRIDGE_EXECUTION_UNSUPPORTED."
            ),
        ),
        MT4BridgeAdapter,
    )

    register_kind(
        BrokerKind(
            id=KIND_MT5,
            label="MetaTrader 5",
            auth="attach",
            modes=("demo", "live"),
            asset_classes=("fx", "cfd", "futures", "stocks", "crypto", "metals"),
            capabilities=("account", "positions", "orders", "quote"),
            execution=True,
            execution_modes=("demo", "live"),
            notes=(
                "Attaches to a running, logged-in terminal64.exe over local IPC. "
                "Demo execution is always available; live execution requires a "
                "time-boxed arm, a positive maxMargin and a non-zero magic."
            ),
        ),
        MT5Adapter,
    )

    register_kind(
        BrokerKind(
            id=KIND_TRADING212,
            label="Trading212",
            auth="credentials",
            modes=("demo", "live"),
            asset_classes=("stocks", "etf"),
            # No `quote`: the public API has no quote endpoint, so the capability
            # is declared absent rather than faked.
            capabilities=("account", "positions", "orders"),
            execution=True,
            execution_modes=("demo", "live"),
            notes=(
                "Equities/ETFs via Trading212's public REST API. Market and limit "
                "orders (signed quantity), cancel and close-by-ticker. No quote "
                "endpoint: a market order needs an explicit price reference in the "
                "intent, and no attached stop/target or scoped cancel-all exists "
                "(named refusals). credentialRef names the env var or secret "
                "holding the API key."
            ),
        ),
        Trading212Adapter,
    )

    register_kind(
        BrokerKind(
            id=KIND_ZERODHA,
            label="Zerodha Kite",
            auth="credentials",
            modes=("live",),
            asset_classes=("stocks", "etf", "futures", "options"),
            capabilities=("account", "positions", "orders", "quote"),
            execution=True,
            execution_modes=("live",),
            notes=(
                "NSE/BSE equities and F&O via Kite Connect's REST API. "
                "MARKET/LIMIT/SL-M orders, modify, cancel and close-from-net, "
                "tagged eclipse-<magic> (trimmed to Kite's 20-char limit). "
                "Quotes take EXCHANGE:SYMBOL; a bare ticker is ambiguous and "
                "refused. Brackets and ticker stream are not implemented. "
                "credentialRef names `API_KEY:ACCESS_TOKEN`."
            ),
        ),
        ZerodhaAdapter,
    )

    register_kind(
        BrokerKind(
            id=KIND_TRADIER,
            label="Tradier",
            auth="credentials",
            modes=("live", "demo"),
            asset_classes=("stocks", "etf", "options"),
            capabilities=("account", "positions", "orders", "quote"),
            execution=True,
            execution_modes=("demo", "live"),
            notes=(
                "US equities/ETFs/options via Tradier's brokerage REST API. "
                "Market/limit/stop orders, cancel, price replace and "
                "close-by-symbol; options use the 100x multiplier. Ownership is "
                "the order tag. Brackets and a scoped cancel-all are named "
                "refusals. Sandbox is a baseUrl, not a mode flag. credentialRef "
                "names the access token."
            ),
        ),
        TradierAdapter,
    )

    register_kind(
        BrokerKind(
            id=KIND_OKX,
            label="OKX",
            auth="credentials",
            modes=("live",),
            asset_classes=("crypto",),
            capabilities=("account", "positions", "orders", "quote"),
            execution=True,
            execution_modes=("live",),
            notes=(
                "OKX spot/perpetuals via the v5 REST API. Market/limit entries, "
                "cancel, amend, attached SL/TP on derivatives (not spot), "
                "reduce-only close, and positions by posId. clOrdId is the "
                "Eclipse ownership tag. HMAC-signed; credentialRef names "
                "`API_KEY:SECRET:PASSPHRASE`. No ticker stream."
            ),
        ),
        OkxAdapter,
    )

    register_kind(
        BrokerKind(
            id=KIND_BINANCE,
            label="Binance (spot)",
            auth="credentials",
            modes=("live",),
            asset_classes=("crypto",),
            capabilities=("account", "positions", "orders", "quote"),
            execution=True,
            execution_modes=("live",),
            notes=(
                "Binance spot via v3 REST. Market/limit/stop-limit entries, "
                "cancel, and close-by-pair (an asset ticket resolves to the "
                "venue's pair via symbolMap/quoteAsset). `positions` are non-zero "
                "spot balances, not margin positions. newClientOrderId is the "
                "Eclipse ownership tag; attached SL/TP is refused on spot. "
                "credentialRef names `KEY:SECRET`. No stream."
            ),
        ),
        BinanceAdapter,
    )

    register_kind(
        BrokerKind(
            id=KIND_KRAKEN,
            label="Kraken",
            auth="credentials",
            modes=("live",),
            asset_classes=("crypto",),
            capabilities=("account", "positions", "orders", "quote"),
            execution=True,
            execution_modes=("live",),
            notes=(
                "Kraken spot via the private/public REST API. Market/limit/"
                "stop-loss entries, cancel, and close-by-pair (asset tickets "
                "resolve through symbolMap/quoteAsset). userref carries the "
                "Eclipse magic; attached SL/TP is refused on spot. "
                "`positions` are non-zero spot balances. Credential is "
                "`KEY:SECRET` (base64 secret). No stream."
            ),
        ),
        KrakenAdapter,
    )

    register_kind(
        BrokerKind(
            id=KIND_COINBASE,
            label="Coinbase Exchange",
            auth="credentials",
            modes=("live",),
            asset_classes=("crypto",),
            capabilities=("account", "positions", "orders", "quote"),
            execution=True,
            execution_modes=("live",),
            notes=(
                "Coinbase Exchange via REST. Limit/market/stop entries with "
                "client_oid as the ownership tag, cancel, status and "
                "close-by-product (asset tickets resolve through "
                "symbolMap/quoteAsset). `positions` are non-zero spot balances. "
                "Attached SL/TP is refused on spot. Credential is "
                "`KEY:SECRET:PASSPHRASE`. No stream."
            ),
        ),
        CoinbaseAdapter,
    )

    register_kind(
        BrokerKind(
            id=KIND_BYBIT,
            label="Bybit (unified)",
            auth="credentials",
            modes=("live",),
            asset_classes=("crypto", "derivatives"),
            capabilities=("account", "positions", "orders", "quote"),
            execution=True,
            execution_modes=("live",),
            notes=(
                "Bybit v5 REST: unified wallet, linear positions, real-time "
                "orders, spot tickers. Create/cancel/amend orders, attached "
                "TP/SL on derivatives, reduce-only close and trading-stop "
                "updates. orderLinkId is the Eclipse ownership tag. Credential "
                "is `KEY:SECRET`. No stream."
            ),
        ),
        BybitAdapter,
    )

    register_kind(
        BrokerKind(
            id=KIND_DERIBIT,
            label="Deribit (derivatives)",
            auth="credentials",
            modes=("live",),
            asset_classes=("crypto", "derivatives", "options"),
            capabilities=("account", "positions", "orders", "quote"),
            execution=True,
            execution_modes=("live",),
            notes=(
                "Deribit v2 JSON-RPC over GET: account summary per currency, "
                "positions, open orders and instrument tickers. Buy/sell market "
                "or limit with stop_loss/take_profit, edit, cancel, reduce-only "
                "close and order state. The order label is the Eclipse ownership "
                "tag. Credential is `CLIENT_ID:CLIENT_SECRET`. No stream."
            ),
        ),
        DeribitAdapter,
    )

    register_kind(
        BrokerKind(
            id=KIND_OANDA,
            label="OANDA v20 (FX/CFD)",
            auth="credentials",
            modes=("demo", "live"),
            asset_classes=("fx", "cfd", "metals"),
            capabilities=("account", "positions", "orders", "quote"),
            execution=True,
            execution_modes=("demo", "live"),
            notes=(
                "OANDA v20 REST: account summary (NAV, margin), openPositions "
                "(long and short halves reported separately), pendingOrders and "
                "pricing. Market/limit/stop with attached takeProfitOnFill/"
                "stopLossOnFill, replace, cancel and full/partial close. Tickets "
                "name the instrument and side (EUR_USD:LONG). The practice host "
                "is the demo account. Credential is a bearer token. No stream."
            ),
        ),
        OandaAdapter,
    )

    register_kind(
        BrokerKind(
            id=KIND_BITGET,
            label="Bitget v2",
            auth="credentials",
            modes=("live",),
            asset_classes=("crypto", "derivatives"),
            capabilities=("account", "positions", "orders", "quote"),
            execution=True,
            execution_modes=("live",),
            notes=(
                "Bitget v2 REST: spot balances, USDT-margined positions, "
                "unfilled spot orders and tickers. Place/cancel/modify orders, "
                "attached TP/SL on derivatives, reduce-only close and TPSL "
                "legs. clientOid is the Eclipse ownership tag. Credential is "
                "`KEY:SECRET:PASSPHRASE`. No stream."
            ),
        ),
        BitgetAdapter,
    )

    register_kind(
        BrokerKind(
            id=KIND_GATEIO,
            label="Gate.io v4",
            auth="credentials",
            modes=("live",),
            asset_classes=("crypto", "derivatives"),
            capabilities=("account", "positions", "orders", "quote"),
            execution=True,
            execution_modes=("live",),
            notes=(
                "Gate.io v4 REST: spot balances, USDT-settled perpetuals, open "
                "orders and tickers. Spot and futures orders, cancel, amend, "
                "futures close and price-trigger rules; attached SL/TP is refused "
                "on spot. The `t-eclipse-...` text is the ownership tag. "
                "Credential is `KEY:SECRET`. No stream."
            ),
        ),
        GateioAdapter,
    )

    register_kind(
        BrokerKind(
            id=KIND_BITSTAMP,
            label="Bitstamp v2",
            auth="credentials",
            modes=("live",),
            asset_classes=("crypto",),
            capabilities=("account", "positions", "orders", "quote"),
            execution=True,
            execution_modes=("live",),
            notes=(
                "Bitstamp v2 REST: balances, open orders, tickers. Buy/sell "
                "market/limit/stop orders, cancel, status and close-by-market "
                "(asset tickets resolve through symbolMap/quoteAsset). A market "
                "buy is sized in quote currency. No client-order id, so a scoped "
                "cancel-all is OWNERSHIP_UNAVAILABLE. Credential is "
                "`KEY:SECRET:CUSTOMER_ID`. No stream."
            ),
        ),
        BitstampAdapter,
    )

    register_kind(
        BrokerKind(
            id=KIND_COINCHECK,
            label="Coincheck",
            auth="credentials",
            modes=("live",),
            asset_classes=("crypto",),
            capabilities=("account", "positions", "orders", "quote"),
            execution=True,
            execution_modes=("live",),
            notes=(
                "Coincheck REST: balances, open orders, tickers. Limit/market "
                "orders with an optional stop_loss_rate, cancel, status and "
                "close-by-asset (BTC resolves to the venue's btc_jpy pair; the "
                "JPY quote balance is not a position). Coincheck exposes no "
                "client-order tag, so cancel-all is OWNERSHIP_UNAVAILABLE. "
                "Credential is `KEY:SECRET`. No stream."
            ),
        ),
        CoincheckAdapter,
    )

    register_kind(
        BrokerKind(
            id=KIND_BITFINEX,
            label="Bitfinex v2",
            auth="credentials",
            modes=("live",),
            asset_classes=("crypto", "derivatives"),
            capabilities=("account", "positions", "orders", "quote"),
            execution=True,
            execution_modes=("live",),
            notes=(
                "Bitfinex v2 REST: wallets, positions, orders and tickers. "
                "Submit/cancel/update orders, attached stop via price_aux_limit, "
                "close a position with an opposite market order and read status. "
                "cid carries the Eclipse ownership tag. Positional v2 arrays are "
                "read by documented index. Credential is `KEY:SECRET`. No stream."
            ),
        ),
        BitfinexAdapter,
    )


_register_builtins()

__all__ = ["get_kind", "list_kinds", "make_adapter", "register_kind"]
