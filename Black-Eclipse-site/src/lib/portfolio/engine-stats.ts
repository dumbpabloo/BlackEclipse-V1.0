// ── Engine read → UI stats ───────────────────────────────────────────────────
// The Python Portfolio Accounting Engine is the book of record. This pure
// mapper turns its `/portfolio/{id}/query` response into the `PortfolioStats`
// shape the existing widget layer renders, so the UI shows the SAME positions,
// cost basis, realized P&L and dividends the agent tools and tests use.
//
// It deliberately does NOT keep a second cost-basis implementation: every money
// value comes from the engine response. Only the day-change split is derived
// from live quote percentages, because that is a market observation, not an
// accounting fact.

import type { PortfolioRead } from '@/lib/harness/clients/portfolio-client';
import type { PortfolioStats } from '@/hooks/use-portfolio-engine';

export interface QuoteChange {
  changePercent?: number;
}

export function statNumber(value: unknown): number {
  const n = typeof value === 'number' ? value : Number(String(value ?? '').replace(/[,\s]/g, ''));
  return Number.isFinite(n) ? n : 0;
}

/**
 * Map one authoritative engine read into the UI's stats bundle.
 * `quotes` supplies only the daily percentage move; nothing about cost or
 * realized P&L is taken from it.
 */
export function portfolioReadToStats(
  read: PortfolioRead,
  quotes: Record<string, QuoteChange | undefined> = {},
): PortfolioStats {
  const base = read.portfolio?.baseCurrency || 'USD';
  const missingFX = new Set(
    (read.dataQuality?.missingFX ?? []).map((pair) => String(pair).split('/').pop()?.toUpperCase() ?? ''),
  );

  const holdings: PortfolioStats['holdings'] = read.positions.map((p) => {
    const price = p.price == null ? 0 : statNumber(p.price);
    const value = p.marketValue == null ? 0 : statNumber(p.marketValue);
    const valueUsd = p.marketValueBase == null ? value : statNumber(p.marketValueBase);
    const unrealized = p.unrealizedPnl == null ? 0 : statNumber(p.unrealizedPnl);
    const unrealizedUsd = p.unrealizedPnlBase == null ? unrealized : statNumber(p.unrealizedPnlBase);
    const realized = statNumber(p.realizedPnl);
    const realizedUsd = p.realizedPnlBase == null ? realized : statNumber(p.realizedPnlBase);
    const currency = (p.currency || base).toUpperCase();
    const change = quotes[p.symbol]?.changePercent ?? 0;
    return {
      symbol: p.symbol,
      name: p.name || p.symbol,
      currency,
      currentPrice: price,
      fxToUsd: value !== 0 ? valueUsd / value : 1,
      quantity: statNumber(p.quantity),
      avgCost: p.averageCost == null ? 0 : statNumber(p.averageCost),
      value,
      valueUsd,
      unrealizedPnL: unrealized,
      unrealizedPnLUsd: unrealizedUsd,
      allocation: 0,
      realizedPnL: realized,
      realizedPnLUsd: realizedUsd,
      dividendIncome: 0,
      dividendIncomeUsd: 0,
      dayPct: change,
      fxMissing: missingFX.has(currency),
      priced: p.price != null && p.marketValue != null,
    };
  });

  // Allocation is a share of invested holdings (cash excluded), matching the
  // existing widgets. The engine's own `weight` includes cash; both are true,
  // but the donut and per-symbol bars must sum to 100.
  const investedUsd = holdings.reduce((sum, h) => sum + h.valueUsd, 0);
  for (const h of holdings) {
    h.allocation = investedUsd > 0 ? (h.valueUsd / investedUsd) * 100 : 0;
  }
  holdings.sort((a, b) => b.valueUsd - a.valueUsd);

  const totalValue = statNumber(read.valuation?.totalValue);
  const costBasis = statNumber(read.pnl?.costBasis);
  const unrealizedPnL = statNumber(read.pnl?.unrealizedPnl);
  const realizedPnL = statNumber(read.pnl?.realizedPnl);
  const dividendsUsd = statNumber(read.pnl?.dividends ?? read.cash?.dividends);
  const roiPercent = costBasis > 0 ? (unrealizedPnL / costBasis) * 100 : 0;

  // Daily move is a market observation: value − value / (1 + pct).
  const dailyChangeValue = holdings.reduce((sum, h) => {
    if (!h.priced || h.dayPct === 0) return sum;
    const prev = h.valueUsd / (1 + h.dayPct / 100);
    return sum + (h.valueUsd - prev);
  }, 0);
  const prevTotal = totalValue - dailyChangeValue;
  const dailyChangePercent = prevTotal > 0 ? (dailyChangeValue / prevTotal) * 100 : 0;

  return {
    totalValue,
    costBasis,
    unrealizedPnL,
    realizedPnL,
    dividendsUsd,
    roiPercent,
    dailyChangeValue,
    dailyChangePercent,
    base,
    holdings,
  };
}
