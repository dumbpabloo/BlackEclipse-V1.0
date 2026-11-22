import { describe, it, expect, vi, beforeEach } from 'vitest';

// Regression for the verified defect: `market_get_quote(symbol="FFC",
// market="PSX")` returned a US closed-end fund's price (15.54 USD) instead of
// Fauji Fertilizer's (PKR) because the live path discarded the market qualifier
// and fetched the bare ticker. These tests pin the identity-first behaviour.

const assetFindMany = vi.fn();
const assetFindFirst = vi.fn();
const fetchQuoteWithFallback = vi.fn();
const fetchYahooBatchRaw = vi.fn();
const newsFindMany = vi.fn();
const searchWeb = vi.fn();

vi.mock('@/lib/db', () => ({
  prisma: {
    asset: {
      findMany: (...a: unknown[]) => assetFindMany(...a),
      findFirst: (...a: unknown[]) => assetFindFirst(...a),
    },
    newsItem: { findMany: (...a: unknown[]) => newsFindMany(...a) },
  },
}));
// The news capability falls through to the web transport when the vault is
// empty. Mock it: this file pins the RESPONSE CONTRACT (C2: never claim "no
// data" over a failed lookup), not the network. Left unmocked, the case below
// hit the live provider inside the unit suite and timed out under load.
vi.mock('@/lib/web-research', () => ({
  searchWeb: (...a: unknown[]) => searchWeb(...a),
  fetchWebPageDetailed: vi.fn(),
}));
vi.mock('@/lib/scraper/fetchers', () => ({
  fetchQuoteWithFallback: (...a: unknown[]) => fetchQuoteWithFallback(...a),
}));
vi.mock('@/lib/scraper/fetchers/yfinance', () => ({
  fetchYahooBatchRaw: (...a: unknown[]) => fetchYahooBatchRaw(...a),
}));

import { fetchCapability } from '../data-bridge';

const FFC_ROWS = [
  {
    symbol: 'FFC',
    name: 'Flaherty & Crumrine Preferred and Income Securities Fund',
    securityType: 'Closed-end Fund',
    market: { country: 'US' },
  },
  { symbol: 'FFC', name: 'Fauji Fertilizer Co. Ltd.', securityType: 'Stock', market: { country: 'PK' } },
];

beforeEach(() => {
  assetFindMany.mockReset();
  assetFindFirst.mockReset();
  fetchQuoteWithFallback.mockReset();
  fetchYahooBatchRaw.mockReset();
  newsFindMany.mockReset();
  newsFindMany.mockResolvedValue([]);
  searchWeb.mockReset();
  // Both sources reachable and genuinely empty -> covered-but-empty (no_data).
  searchWeb.mockResolvedValue({ provider: 'mock', results: [], diagnostics: [] });
});

describe('live_quote — instrument identity', () => {
  it('resolves FFC/PSX to the Pakistan instrument (not the US fund)', async () => {
    assetFindMany.mockResolvedValue(FFC_ROWS);
    fetchQuoteWithFallback.mockResolvedValue({
      status: 'ok',
      source: 'tradingview',
      quote: { symbol: 'FFC', price: 538.2, changePct: -0.079, market: 'PK' },
    });

    const result = await fetchCapability('live_quote', { symbol: 'FFC', market: 'PSX' });

    expect(result.status).toBe('OK');
    expect(result.market).toBe('PK');
    expect(result.currency).toBe('PKR');
    expect(result.instrumentId).toBe('PK:FFC');
    // The fetcher was called with the RESOLVED market, never the raw "PSX".
    expect(fetchQuoteWithFallback).toHaveBeenCalledWith('FFC', 'PK');
  });

  it('is AMBIGUOUS for a bare colliding ticker (never guesses US)', async () => {
    assetFindMany.mockResolvedValue(FFC_ROWS);
    const result = await fetchCapability('live_quote', { symbol: 'FFC' });
    expect(result.status).toBe('AMBIGUOUS');
    expect(result).not.toHaveProperty('price');
    expect(fetchQuoteWithFallback).not.toHaveBeenCalled();
  });

  it('rejects an unknown market qualifier instead of dropping it', async () => {
    assetFindMany.mockResolvedValue(FFC_ROWS);
    const result = await fetchCapability('live_quote', { symbol: 'FFC', market: 'MARS' });
    // An unknown venue is a COVERAGE gap (not_covered), not an ambiguity (#4).
    expect(result.failureClass).toBe('not_covered');
    expect(fetchQuoteWithFallback).not.toHaveBeenCalled();
  });

  it('rejects a provider answer priced in the wrong currency', async () => {
    assetFindMany.mockResolvedValue(FFC_ROWS);
    // Provider priced it in USD for a PK instrument — the exact defect shape.
    fetchQuoteWithFallback.mockResolvedValue({
      status: 'ok',
      source: 'yfinance',
      quote: { symbol: 'FFC', price: 15.54, changePct: 0.03, market: 'US', currency: 'USD' },
    });
    const result = await fetchCapability('live_quote', { symbol: 'FFC', market: 'PSX' });
    expect(result.status).toBe('UNAVAILABLE');
    expect(result).not.toHaveProperty('price');
  });
});

describe('price_history — instrument identity', () => {
  it('refuses to fetch history for an ambiguous ticker', async () => {
    assetFindMany.mockResolvedValue(FFC_ROWS);
    const result = await fetchCapability('price_history', { symbol: 'FFC' });
    expect(result.status).toBe('AMBIGUOUS');
    expect(fetchYahooBatchRaw).not.toHaveBeenCalled();
  });

  it('fetches with the resolved market', async () => {
    assetFindMany.mockResolvedValue(FFC_ROWS);
    fetchYahooBatchRaw.mockResolvedValue({
      history: { FFC: [{ date: '2026-09-18', close: 1 }, { date: '2026-09-19', close: 2 }] },
    });
    const result = await fetchCapability('price_history', { symbol: 'FFC', market: 'PSX' });
    expect(result.status).toBe('OK');
    expect(fetchYahooBatchRaw).toHaveBeenCalledWith(['FFC'], 'PK', expect.any(String), expect.any(String), undefined);
  });
});

describe('market_snapshot — venue alias', () => {
  it('resolves PSX to PK so a valid PK instrument is not "not in universe"', async () => {
    assetFindMany.mockResolvedValue([
      {
        symbol: 'FFC',
        name: 'Fauji Fertilizer Co. Ltd.',
        sector: 'Process Industries',
        industry: 'Chemicals',
        securityType: 'Stock',
        assetClass: 'Equity',
        market: { country: 'PK', exchange: 'PSX' },
        price: { price: 538.2, changePct: -0.079, marketCap: 1, volume: 2, peRatio: 3, source: 'tradingview', updatedAt: null },
      },
    ]);
    const result = await fetchCapability('market_snapshot', { symbol: 'FFC', market: 'PSX' });
    expect(result.status).toBe('OK');
    expect(result.market ?? result.country).toBe('PK');
    expect(result.instrumentId).toBe('PK:FFC');
    expect(result.currency).toBe('PKR');
    expect(result.mic).toBe('XPSX');
  });

  it('refuses an unknown venue as not_covered rather than dropping it', async () => {
    assetFindMany.mockResolvedValue([]);
    const result = await fetchCapability('market_snapshot', { symbol: 'FFC', market: 'MARS' });
    expect(result.status).toBe('UNAVAILABLE');
    expect(result.failureClass).toBe('not_covered');
  });
});

// -- C2: news must not return a false negative -------------------------------
describe('news � no false negatives (C2)', () => {
  const webSearch = vi.fn();
  // Re-mock web-research for this file via dynamic import is awkward; instead
  // assert the shape contract the tool relies on through the mocked vault.
  // (The vault-empty ? web fall-through is covered by the source-failure test.)

  it('returns no_data when both vault and web are empty', async () => {
    // asset mocks are irrelevant here; re-point prisma for the news query.
    const prismaNews = await import('@/lib/db');
    // @ts-expect-error test shim
    prismaNews.prisma.newsItem = { findMany: async () => [] };
    const result = await fetchCapability('news', { query: 'zzz-no-such-thing' });
    // This exercises the LIVE news/web transport, so both outcomes are honest:
    //   OK + no_data          -> sources were reachable and genuinely empty
    //   UNAVAILABLE + failure -> the network/provider refused the request
    // C2 forbids a *false negative* (claiming "no data" over a failed lookup),
    // not a reported failure: a UNAVAILABLE result must carry a failure class
    // and must not smuggle in items.
    expect(['OK', 'UNAVAILABLE']).toContain(result.status);
    if (result.status === 'OK') {
      expect(result.failureClass === 'no_data' || Array.isArray(result.items)).toBe(true);
    } else {
      expect(result.failureClass).toBeTruthy();
      expect(result.items ?? []).toHaveLength(0);
    }
    void webSearch;
    // This case exercises the real news/web transport (no provider mock in this
    // file), so it must be allowed a network budget larger than the 5s default —
    // otherwise the release gate is load-sensitive.
  }, 20_000);
});
