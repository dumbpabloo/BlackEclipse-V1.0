"""Market Data Layer configuration.

One place for every tunable the market subsystem reads. Values come from the
environment with safe defaults, so zero-config keeps working and an operator can
change provider priority, timeouts, retry policy, cache TTLs and data-quality
thresholds WITHOUT editing source.

No secret value is ever read here — only the NAME of a credential the bridge
resolves. The Python runtime never holds a provider key.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_list(name: str) -> list[str]:
    raw = os.environ.get(name)
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


# Default provider priority per capability. The gateway consults this when the
# registry has no operator override. Names are provider ids the TS adapter
# router understands.
DEFAULT_PRIORITY: dict[str, tuple[str, ...]] = {
    "quote": ("yahoo", "binance", "coingecko", "stooq"),
    "historical": ("yahoo", "stooq"),
    "ohlcv": ("yahoo", "binance"),
    "volume": ("yahoo", "binance"),
    "orderbook": ("binance", "polygon", "finnhub", "yahoo"),
    "trades": ("binance", "polygon", "finnhub", "yahoo"),
    "market_cap": ("yahoo", "coingecko"),
    "supply": ("coingecko",),
    "funding": ("binance", "hyperliquid"),
    "open_interest": ("binance", "hyperliquid"),
    "options": ("yahoo", "polygon", "finnhub"),
    "fx": ("yahoo",),
    "bonds": ("treasury",),
    "tvl": ("defillama",),
    "onchain": ("etherscan", "solscan"),
    "liquidations": ("coinglass",),
    "long_short": ("coinglass",),
}


@dataclass
class MarketConfig:
    """Resolved market-data configuration. Immutable for the process lifetime."""

    # ── Provider routing ────────────────────────────────────────────────────
    priority: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: dict(DEFAULT_PRIORITY)
    )
    # Cap on how many providers the gateway will try for one request.
    max_providers: int = field(default_factory=lambda: _env_int("HARNESS_MARKET_MAX_PROVIDERS", 3))
    # ── Timeouts / retries ──────────────────────────────────────────────────
    request_timeout_ms: int = field(default_factory=lambda: _env_int("HARNESS_MARKET_TIMEOUT_MS", 12_000))
    max_retries: int = field(default_factory=lambda: _env_int("HARNESS_MARKET_MAX_RETRIES", 2))
    backoff_base_s: float = field(default_factory=lambda: _env_float("HARNESS_MARKET_BACKOFF_BASE_S", 0.35))
    backoff_max_s: float = field(default_factory=lambda: _env_float("HARNESS_MARKET_BACKOFF_MAX_S", 3.0))
    # ── Circuit breaker ─────────────────────────────────────────────────────
    breaker_fail_threshold: int = field(default_factory=lambda: _env_int("HARNESS_MARKET_BREAKER_THRESHOLD", 4))
    breaker_reset_s: float = field(default_factory=lambda: _env_float("HARNESS_MARKET_BREAKER_RESET_S", 60.0))
    #: Cap for the per-provider exponential backoff: after repeated failures the
    #: retry waits ``breaker_reset_s * 2^(failures-1)`` up to this many seconds.
    breaker_lifetime_s: float = field(
        default_factory=lambda: _env_float("HARNESS_MARKET_BREAKER_LIFETIME_S", 300.0)
    )
    # ── Cache ───────────────────────────────────────────────────────────────
    cache_enabled: bool = field(default_factory=lambda: _env_bool("HARNESS_MARKET_CACHE", True))
    cache_ttl_seconds: dict[str, int] = field(
        default_factory=lambda: {
            "quote": 5,
            "historical": 6 * 3600,
            "ohlcv": 60,
            "volume": 60,
            "orderbook": 2,
            "trades": 2,
            "market_cap": 300,
            "supply": 3600,
            "funding": 30,
            "open_interest": 30,
            "options": 300,
            "fx": 60,
            "bonds": 6 * 3600,
            "tvl": 300,
            "onchain": 60,
            "liquidations": 60,
            "long_short": 60,
        }
    )
    # ── Data quality thresholds ─────────────────────────────────────────────
    # Relative tolerance under which two providers agree on a value.
    conflict_rel_tolerance: float = field(default_factory=lambda: _env_float("HARNESS_MARKET_CONFLICT_TOLERANCE", 0.015))
    # Max age (seconds) before a value is flagged stale for a realtime ask.
    realtime_max_age_s: float = field(default_factory=lambda: _env_float("HARNESS_MARKET_REALTIME_MAX_AGE_S", 60.0))
    # Risk-free rate used to CALCULATE option Greeks when the provider does not
    # supply them (annual, decimal). Overridable per operator.
    risk_free_rate: float = field(default_factory=lambda: _env_float("HARNESS_MARKET_RISK_FREE", 0.04))
    # ── Streaming ───────────────────────────────────────────────────────────
    stream_enabled: bool = field(default_factory=lambda: _env_bool("HARNESS_MARKET_STREAMING", True))
    stream_max_subscriptions: int = field(default_factory=lambda: _env_int("HARNESS_MARKET_STREAM_MAX_SUBS", 64))
    stream_queue_size: int = field(default_factory=lambda: _env_int("HARNESS_MARKET_STREAM_QUEUE", 1000))
    stream_reconnect_max_s: float = field(default_factory=lambda: _env_float("HARNESS_MARKET_STREAM_RECONNECT_MAX_S", 30.0))
    # Initial reconnect backoff (doubles on each attempt up to the max).
    stream_backoff_base_s: float = field(default_factory=lambda: _env_float("HARNESS_MARKET_STREAM_BACKOFF_BASE_S", 0.5))
    # Jitter as a fraction of the backoff delay (0 = none). Prevents a fleet of
    # reconnects from synchronizing on one provider.
    stream_backoff_jitter: float = field(default_factory=lambda: _env_float("HARNESS_MARKET_STREAM_JITTER", 0.3))
    # A connected-but-silent stream is reconnected after this many seconds.
    stream_stale_timeout_s: float = field(default_factory=lambda: _env_float("HARNESS_MARKET_STREAM_STALE_S", 45.0))
    # Cap on reconnect attempts before the manager parks in an honest `backoff`
    # state (a later subscribe resets it).
    stream_max_reconnects: int = field(default_factory=lambda: _env_int("HARNESS_MARKET_STREAM_MAX_RECONNECTS", 0))
    # ── Provider enable/disable ─────────────────────────────────────────────
    disabled_providers: tuple[str, ...] = field(
        default_factory=lambda: tuple(_env_list("HARNESS_MARKET_DISABLED_PROVIDERS"))
    )

    def provider_order(self, capability: str) -> tuple[str, ...]:
        """Ordered providers for a capability, minus operator-disabled ones."""
        order = self.priority.get(capability, ())
        if not self.disabled_providers:
            return order
        return tuple(p for p in order if p not in self.disabled_providers)

    def ttl(self, capability: str) -> int:
        return self.cache_ttl_seconds.get(capability, 60)


_config: Optional[MarketConfig] = None


def get_config() -> MarketConfig:
    """The process-wide market config (built once, deterministic)."""
    global _config
    if _config is None:
        _config = MarketConfig()
    return _config


def reset_config() -> None:
    """Rebuild on next read. Tests only."""
    global _config
    _config = None


__all__ = ["DEFAULT_PRIORITY", "MarketConfig", "get_config", "reset_config"]
