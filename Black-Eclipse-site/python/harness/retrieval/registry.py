"""Provider capability registry (Financial Data Gateway).

The retrieval layer already treats every data source through ONE contract
(`RetrievalSource`). What was missing was a *declarative registry* that answers,
without running a request:

  * which provider can serve which capability,
  * which credential each provider needs (and whether it is configured),
  * each provider's rate-limit/quota shape,
  * the licensing/attribution/redistribution/storage policy that applies to its
    data.

This module owns exactly that metadata. It does NOT execute anything and it does
NOT name a transport: an adapter (JDdS, the live structured provider, SEC, WEB,
crypto, a declared MCP source) is registered here as a `ProviderDescriptor`
alongside the existing `RetrievalSource` instance. The layer and the agent tools
read the registry to describe providers; the planner keeps doing selection.

Design rules (mirror the rest of `retrieval/`):

  * One row per provider. Capabilities are a set, not an `if` ladder.
  * A provider need not implement every capability. Absence is honest.
  * Licensing metadata is recorded, never asserted. We do not make legal claims;
    operators and the UI surface the recorded policy.
  * No secret value ever flows through here — only the NAME of the env var /
    credential reference.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .contract import DataType

# Capabilities a financial provider may declare. This is a superset of the
# retrieval layer's `DataType` vocabulary: the extra members (crypto, entity
# resolution, filings-by-id) are the Financial Data Gateway surface that the
# retrieval planner does not itself select on, but the registry and the agent
# tools expose so a provider's real coverage is visible.
Capability = str


# ── Licensing / data-usage policy (recorded, never asserted) ────────────────

@dataclass(frozen=True)
class DataUsagePolicy:
    """The data-usage terms recorded for a provider.

    Every field defaults to the honest "unknown"/"unspecified" state. Populating
    a field is an operator statement about the provider's terms, not a legal
    conclusion drawn by Eclipse. The UI surfaces this verbatim.
    """

    license: str = "unspecified"
    attribution: Optional[str] = None
    redistribution: str = "unspecified"   # "permitted" | "restricted" | "unknown"
    storage: str = "unspecified"          # "permitted" | "restricted" | "unknown"
    notes: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "license": self.license,
            "attribution": self.attribution,
            "redistribution": self.redistribution,
            "storage": self.storage,
            "notes": self.notes,
        }


# ── Rate limits / quota ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class RateLimitSpec:
    """A provider's declared rate limit for one capability (best-effort).

    `requests_per_minute` / `requests_per_day` are `None` when the provider does
    not publish a number — never a guessed default. `concurrency` bounds how many
    simultaneous calls the manager will permit for that provider.
    """

    requests_per_minute: Optional[int] = None
    requests_per_day: Optional[int] = None
    concurrency: int = 4
    # A cost hint for COST_OPTIMIZED routing: 0 = free/keyless. Advisory only.
    cost_tier: str = "unknown"            # "free" | "freemium" | "paid" | "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "requestsPerMinute": self.requests_per_minute,
            "requestsPerDay": self.requests_per_day,
            "concurrency": self.concurrency,
            "costTier": self.cost_tier,
        }


# ── Provider descriptor ─────────────────────────────────────────────────────

@dataclass
class ProviderDescriptor:
    """Everything the gateway knows about one provider, declaratively.

    `source_name` links this descriptor to the live `RetrievalSource` registered
    in the layer (when one exists). A descriptor may also describe a provider
    that is *declared* (catalogued / credential-ready) but has no runnable source
    yet — `implemented=False` makes that explicit instead of pretending.
    """

    id: str
    label: str
    tier: str
    capabilities: tuple[Capability, ...]
    datasets: tuple[str, ...] = ()
    # The credential reference (env var name / vault key). NEVER the value.
    credential_ref: Optional[str] = None
    # Whether the credential is currently satisfied (env or vault). None =
    # "not applicable / keyless". Filled by the live availability probe, not here.
    configured: Optional[bool] = None
    rate_limit: RateLimitSpec = field(default_factory=RateLimitSpec)
    usage: DataUsagePolicy = field(default_factory=DataUsagePolicy)
    # True when a runnable `RetrievalSource` backs this descriptor. False = the
    # provider is declared/catalogued but not yet implemented (documented gap).
    implemented: bool = True
    source_name: Optional[str] = None
    notes: Optional[str] = None

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def to_dict(self, *, include_usage: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "label": self.label,
            "tier": self.tier,
            "capabilities": list(self.capabilities),
            "datasets": list(self.datasets),
            "credentialRef": self.credential_ref,
            "configured": self.configured,
            "implemented": self.implemented,
            "sourceName": self.source_name,
            "rateLimit": self.rate_limit.to_dict(),
        }
        if include_usage:
            out["usage"] = self.usage.to_dict()
        if self.notes:
            out["notes"] = self.notes
        return out


class ProviderRegistry:
    """The one capability registry. Ordered and deterministic."""

    def __init__(self) -> None:
        self._providers: dict[str, ProviderDescriptor] = {}
        self._order: list[str] = []

    def register(self, descriptor: ProviderDescriptor) -> None:
        if descriptor.id not in self._providers:
            self._order.append(descriptor.id)
        self._providers[descriptor.id] = descriptor

    def unregister(self, provider_id: str) -> bool:
        removed = self._providers.pop(provider_id, None) is not None
        if removed:
            try:
                self._order.remove(provider_id)
            except ValueError:
                pass
        return removed

    def get(self, provider_id: str) -> Optional[ProviderDescriptor]:
        return self._providers.get(provider_id)

    def all(self) -> list[ProviderDescriptor]:
        return [self._providers[pid] for pid in self._order]

    def by_capability(self, capability: Capability) -> list[ProviderDescriptor]:
        return [p for p in self.all() if p.supports(capability)]

    def capabilities(self) -> list[str]:
        seen: list[str] = []
        for p in self.all():
            for c in p.capabilities:
                if c not in seen:
                    seen.append(c)
        return seen

    def describe(self) -> list[dict[str, Any]]:
        return [p.to_dict() for p in self.all()]

    def summary(self) -> dict[str, Any]:
        providers = self.all()
        return {
            "providerCount": len(providers),
            "implementedCount": sum(1 for p in providers if p.implemented),
            "capabilities": self.capabilities(),
            "providers": [p.to_dict() for p in providers],
        }


# ── The default descriptor set ───────────────────────────────────────────────
# Built from the sources the layer actually constructs. A descriptor is only
# `implemented=True` when a runnable source exists; a catalogued-only provider is
# recorded with `implemented=False` and the dependency named. Nothing here is a
# fake adapter — a non-implemented row is a documented gap, not a stub.

# Capability constants used by the descriptors (kept as plain strings so the
# registry does not need an enum that would drift from `DataType`).
CAP_QUOTE = "current_quote"
CAP_HISTORY = "historical_prices"
CAP_PROFILE = "company_profile"
CAP_STATEMENTS = "financial_statements"
CAP_MACRO = "macro_series"
CAP_NEWS = "news"
CAP_WEB = "web_research"
CAP_EARNINGS = "earnings"
CAP_FILINGS = "filings"
CAP_CRYPTO = "crypto_market_data"
CAP_ENTITY = "entity_resolution"


def build_default_registry() -> ProviderRegistry:
    """The registry the service installs at boot.

    Mirrors the sources `FinancialRetrievalLayer` registers. A future provider is
    added here as one row; no other module changes.
    """
    registry = ProviderRegistry()

    registry.register(
        ProviderDescriptor(
            id="JDDS",
            label="JDdS vault",
            tier="structured",
            source_name="JDDS",
            capabilities=(CAP_QUOTE, CAP_PROFILE, CAP_STATEMENTS),
            datasets=("universe", "cached_prices", "fundamentals_snapshot"),
            # JDdS is local and keyless; it is a cache/vault, not an authority.
            credential_ref=None,
            configured=True,
            rate_limit=RateLimitSpec(requests_per_minute=None, concurrency=8, cost_tier="free"),
            usage=DataUsagePolicy(
                license="local-seed-snapshot",
                redistribution="unknown",
                storage="permitted",
                notes="The app's own committed market-data seed. Not a universal authority; can be stale.",
            ),
        )
    )
    registry.register(
        ProviderDescriptor(
            id="LIVE_STRUCTURED",
            label="Live structured provider (yfinance / scraper chain)",
            tier="structured",
            source_name="LIVE_STRUCTURED",
            capabilities=(CAP_QUOTE, CAP_HISTORY, CAP_MACRO),
            datasets=("live_quote", "price_history", "macro_series"),
            credential_ref=None,
            configured=True,
            rate_limit=RateLimitSpec(requests_per_minute=None, concurrency=4, cost_tier="free"),
            usage=DataUsagePolicy(
                license="upstream-provider-terms",
                redistribution="restricted",
                storage="unknown",
                notes="Keyless yfinance/scraper bridge; subject to the upstream provider's terms.",
            ),
        )
    )
    registry.register(
        ProviderDescriptor(
            id="SEC",
            label="SEC EDGAR (XBRL)",
            tier="primary",
            source_name="SEC",
            capabilities=(CAP_STATEMENTS, CAP_FILINGS),
            datasets=("companyfacts", "companyconcept", "filings"),
            credential_ref=None,
            configured=True,
            rate_limit=RateLimitSpec(requests_per_minute=600, concurrency=2, cost_tier="free"),
            usage=DataUsagePolicy(
                license="public-domain (US Government work)",
                attribution="SEC EDGAR",
                redistribution="permitted",
                storage="permitted",
                notes="US issuers only. Declared email User-Agent required by SEC fair-access policy.",
            ),
        )
    )
    registry.register(
        ProviderDescriptor(
            id="WEB",
            label="Web research (search + fetch)",
            tier="secondary",
            source_name="WEB",
            capabilities=(CAP_NEWS, CAP_WEB, CAP_EARNINGS, CAP_STATEMENTS),
            datasets=("web_search", "web_fetch"),
            credential_ref=None,
            configured=True,
            rate_limit=RateLimitSpec(requests_per_minute=None, concurrency=6, cost_tier="free"),
            usage=DataUsagePolicy(
                license="per-source-terms",
                redistribution="restricted",
                storage="unknown",
                notes="Content is UNTRUSTED data. A snippet is never evidence until the page is retrieved.",
            ),
        )
    )
    registry.register(
        ProviderDescriptor(
            id="CRYPTO",
            label="Crypto market data (Binance public)",
            tier="structured",
            source_name="CRYPTO",
            capabilities=(CAP_CRYPTO, CAP_QUOTE),
            datasets=("crypto_ticker_24h",),
            credential_ref=None,
            configured=True,
            rate_limit=RateLimitSpec(requests_per_minute=1200, concurrency=4, cost_tier="free"),
            usage=DataUsagePolicy(
                license="upstream-provider-terms",
                redistribution="unknown",
                storage="unknown",
                notes="Keyless public 24h ticker for crypto pairs.",
            ),
        )
    )
    # ── Catalogued / credential-ready providers (declared, NOT implemented) ──
    # These have a credential entry in the vault and a catalog entry, but no
    # runnable source yet. Recorded honestly with `implemented=False` and the
    # exact missing dependency, rather than a fake adapter.
    for pid, label, env, caps, datasets, note in (
        (
            "FMP", "Financial Modeling Prep", "FINANCIALMODELINGPREP_API_KEY",
            (CAP_STATEMENTS, CAP_QUOTE, CAP_HISTORY, CAP_PROFILE, CAP_EARNINGS),
            ("income-statement", "cash-flow", "balance-sheet", "quote", "profile", "ratios-ttm"),
            "Served through the fundamentals service (SEC/FMP/yfinance merge) on the TS bridge; "
            "not yet a first-class retrieval source.",
        ),
        (
            "POLYGON", "Polygon.io", "POLYGON_API_KEY",
            (CAP_QUOTE, CAP_HISTORY, CAP_STATEMENTS),
            ("aggregates", "reference", "financials"),
            "Credential vault entry + catalog entry exist; no retrieval adapter yet.",
        ),
        (
            "FINNHUB", "Finnhub", "FINNHUB_API_KEY",
            (CAP_QUOTE, CAP_NEWS, CAP_PROFILE, CAP_EARNINGS),
            ("quote", "company-news", "profile", "earnings"),
            "Credential vault entry + catalog entry exist; no retrieval adapter yet.",
        ),
        (
            "ALPHAVANTAGE", "Alpha Vantage", "ALPHAVANTAGE_API_KEY",
            (CAP_QUOTE, CAP_HISTORY, CAP_EARNINGS, CAP_STATEMENTS),
            ("quote", "time-series", "earnings", "fundamentals"),
            "Used for the earnings calendar today; not a first-class retrieval source.",
        ),
        (
            "COINGECKO", "CoinGecko", "COINGECKO_API_KEY",
            (CAP_CRYPTO,),
            ("coins", "market-chart", "global"),
            "Credential vault entry + catalog entry exist; no retrieval adapter yet.",
        ),
        (
            "COINMARKETCAP", "CoinMarketCap", "COINMARKETCAP_API_KEY",
            (CAP_CRYPTO,),
            ("quotes", "listings"),
            "Credential vault entry + catalog entry exist; no retrieval adapter yet.",
        ),
    ):
        registry.register(
            ProviderDescriptor(
                id=pid,
                label=label,
                tier="structured",
                capabilities=caps,
                datasets=datasets,
                credential_ref=env,
                configured=None,  # resolved by the live availability probe
                rate_limit=RateLimitSpec(cost_tier="freemium"),
                usage=DataUsagePolicy(notes=note),
                implemented=False,
                source_name=None,
                notes=note,
            )
        )
    return registry


__all__ = [
    "Capability",
    "DataUsagePolicy",
    "ProviderDescriptor",
    "ProviderRegistry",
    "RateLimitSpec",
    "build_default_registry",
    "CAP_CRYPTO",
    "CAP_EARNINGS",
    "CAP_ENTITY",
    "CAP_FILINGS",
    "CAP_HISTORY",
    "CAP_MACRO",
    "CAP_NEWS",
    "CAP_PROFILE",
    "CAP_QUOTE",
    "CAP_STATEMENTS",
    "CAP_WEB",
]
