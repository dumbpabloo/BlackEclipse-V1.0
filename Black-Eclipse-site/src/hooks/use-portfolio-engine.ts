import { useMemo } from 'react';
import type { Transaction } from '@/lib/portfolio/types';

/**
 * Approximate USD per unit of currency, used ONLY when a live rate is missing
 * (before `/api/forex` lands, or for a currency it doesn't carry). These are
 * documented order-of-magnitude figures, not quoted rates — every holding that
 * falls back is flagged `fxMissing` so the UI can say the USD value is an
 * estimate. Without a table a PKR position would be valued 278× too high, which
 * is a far worse lie than an approximate one.
 */
const FALLBACK_USD_PER_UNIT: Record<string, number> = {
  USD: 1,
  PKR: 1 / 278,
  INR: 1 / 87,
  EUR: 1.08,
  GBP: 1.27,
  JPY: 1 / 156,
  CNY: 1 / 7.2,
  HKD: 1 / 7.8,
  KRW: 1 / 1380,
  TWD: 1 / 32,
  SGD: 1 / 1.35,
  MYR: 1 / 4.7,
  AUD: 0.66,
  CAD: 0.72,
  BRL: 1 / 5.4,
  MXN: 1 / 18,
  ZAR: 1 / 18,
  TRY: 1 / 34,
  AED: 1 / 3.67,
  SAR: 1 / 3.75,
  IDR: 1 / 16000,
  THB: 1 / 34,
  VND: 1 / 25000,
  EGP: 1 / 48,
  NGN: 1 / 1500,
  RUB: 1 / 90,
};

export interface PortfolioStats {
  /** Total portfolio value expressed in the base (reporting) currency USD. */
  totalValue: number;
  /** Blended portfolio cost basis in USD-equivalent. */
  costBasis: number;
  unrealizedPnL: number;
  /** Realized P/L realized in each asset's native currency and converted at the live FX rate (approximation). */
  realizedPnL: number;
  /** Cumulative dividend income in USD (from DIVIDEND transactions). */
  dividendsUsd: number;
  roiPercent: number;
  dailyChangeValue: number;
  dailyChangePercent: number;
  /** Reporting currency of the values above (engine base, else USD). */
  base: string;
  holdings: Array<{
    symbol: string;
    name: string;
    /** ISO currency code the instrument quotes in (USD, PKR, INR, …). */
    currency: string;
    /** Native currency price. */
    currentPrice: number;
    /** Live FX rate: 1 unit of `currency` = fxToUsd USD. */
    fxToUsd: number;
    quantity: number;
    avgCost: number;
    value: number;
    /** USD-equivalent value for cross-asset aggregation. */
    valueUsd: number;
    unrealizedPnL: number;
    unrealizedPnLUsd: number;
    allocation: number;
    realizedPnL: number;
    realizedPnLUsd: number;
    /** Cumulative dividend income for this position (native currency). */
    dividendIncome: number;
    dividendIncomeUsd: number;
    dayPct: number;
    /** True when a non-USD position had no FX rate and was converted 1:1. */
    fxMissing: boolean;
    /** False when the engine has no usable mark for this position. */
    priced?: boolean;
  }>;
}

export interface LiveAssetInput {
  symbol: string;
  price?: number;
  changePercent?: number;
  /** ISO currency code; anything other than USD must carry `fxToUsd`. */
  currency?: string;
  /** 1 unit of `currency` = fxToUsd USD. Required for non-USD instruments. */
  fxToUsd?: number;
  name?: string;
}

const EPS = 1e-9;
const precisionThreshold = 1e-6;

// ── FIFO/LIFO costs in native currency, then converted at live FX ───────────
export function computePortfolioStats(
  transactions: Transaction[],
  liveAssets: LiveAssetInput[],
  taxMethod: 'FIFO' | 'LIFO' = 'FIFO'
): PortfolioStats {
  const liveMap = new Map<string, LiveAssetInput>();
  for (const a of liveAssets) liveMap.set(a.symbol, a);

  const grouped = new Map<string, Transaction[]>();
  for (const tx of transactions) {
    if (tx.type === 'DEPOSIT' || tx.type === 'WITHDRAWAL') continue;
    const list = grouped.get(tx.symbol) || [];
    list.push(tx);
    grouped.set(tx.symbol, list);
  }

  const holdings: PortfolioStats['holdings'] = [];
  let totalValueUsd = 0;
  let totalCostBasisUsd = 0;
  let totalRealizedUsd = 0;
  let totalDividendUsd = 0;
  let dailyChangeValueUsd = 0;

  for (const [symbol, txs] of grouped) {
    const sorted = [...txs].sort((a, b) => new Date(a.date).getTime() - new Date(b.date).getTime());
    const live = liveMap.get(symbol);
    const currency: string = live?.currency || 'USD';
    // USD converts 1:1. Any other currency prefers its live rate; when that is
    // missing we fall back to a documented approximation and FLAG the row, so
    // the UI can label the USD value as an estimate instead of implying a quote.
    const liveRate = live?.fxToUsd && live.fxToUsd > 0 ? live.fxToUsd : undefined;
    const fallbackRate = FALLBACK_USD_PER_UNIT[currency];
    const fxMissing = currency !== 'USD' && liveRate === undefined;
    const fxToUsd = currency === 'USD' ? 1 : (liveRate ?? fallbackRate ?? 1);

    // Dividends are pure income — additive to realized P/L, never affect lots.
    const dividendIncome = txs
      .filter(t => t.type === 'DIVIDEND')
      .reduce((a, t) => a + (t.quantity * t.price) - t.fee, 0);
    if (dividendIncome !== 0) totalDividendUsd += dividendIncome * fxToUsd;

    const tradeTxs = sorted.filter(t => t.type !== 'DIVIDEND');

    const buyLots: Array<{ qty: number; price: number }> = [];
    let realized = 0;
    let qty = 0;
    let costBasis = 0;

    for (const tx of tradeTxs) {
      if (tx.type === 'BUY') {
        buyLots.push({ qty: tx.quantity, price: tx.price });
        qty += tx.quantity;
        costBasis += tx.quantity * tx.price + tx.fee;
      } else if (tx.type === 'SELL') {
        let toSell = tx.quantity;
        let costOfSold = 0;
        while (toSell > EPS && buyLots.length > 0) {
          const i = taxMethod === 'FIFO' ? 0 : buyLots.length - 1;
          const lot = buyLots[i];
          if (lot.qty <= toSell) {
            toSell -= lot.qty;
            costOfSold += lot.qty * lot.price;
            buyLots.splice(i, 1);
          } else {
            lot.qty -= toSell;
            costOfSold += toSell * lot.price;
            toSell = 0;
          }
        }
        const proceeds = tx.quantity * tx.price - tx.fee;
        realized += proceeds - costOfSold;
        qty -= tx.quantity;
        costBasis -= costOfSold;
      }
    }
    // Realized P/L counted once per symbol (open or closed positions alike).
    if (realized !== 0) totalRealizedUsd += realized * fxToUsd;

    if (qty <= precisionThreshold) {
      continue;
    }

    const currentPrice = live?.price ?? 0;
    const dayPct = live?.changePercent ?? 0;
    const value = qty * currentPrice;
    const avgCost = costBasis / qty;
    const unrealized = value - costBasis;
    const valueUsd = value * fxToUsd;

    totalValueUsd += valueUsd;
    totalCostBasisUsd += costBasis * fxToUsd;

    const prevPrice = currentPrice / (1 + dayPct / 100);
    dailyChangeValueUsd += (value - qty * prevPrice) * fxToUsd;

    holdings.push({
      symbol,
      name: live?.name ?? symbol,
      currency,
      currentPrice,
      fxToUsd,
      quantity: qty,
      avgCost,
      value,
      valueUsd,
      unrealizedPnL: unrealized,
      unrealizedPnLUsd: unrealized * fxToUsd,
      allocation: 0,
      realizedPnL: realized,
      realizedPnLUsd: realized * fxToUsd,
    dividendIncome: dividendIncome,
    dividendIncomeUsd: dividendIncome * fxToUsd,
    dayPct,
    fxMissing,
    });
  }

  for (const h of holdings) {
    h.allocation = totalValueUsd > 0 ? (h.valueUsd / totalValueUsd) * 100 : 0;
  }
  holdings.sort((a, b) => b.valueUsd - a.valueUsd);

  const unrealizedPnLUsd = totalValueUsd - totalCostBasisUsd;
  const roiPercent = totalCostBasisUsd > 0 ? (unrealizedPnLUsd / totalCostBasisUsd) * 100 : 0;
  const prevTotalUsd = totalValueUsd - dailyChangeValueUsd;
  const dailyChangePercent = prevTotalUsd > 0 ? (dailyChangeValueUsd / prevTotalUsd) * 100 : 0;

  return {
    totalValue: totalValueUsd,
    costBasis: totalCostBasisUsd,
    unrealizedPnL: unrealizedPnLUsd,
    realizedPnL: totalRealizedUsd + totalDividendUsd,
    dividendsUsd: totalDividendUsd,
    roiPercent,
    dailyChangeValue: dailyChangeValueUsd,
    dailyChangePercent,
    base: 'USD',
    holdings,
  };
}

export function usePortfolioEngine(
  transactions: Transaction[],
  liveAssets: LiveAssetInput[],
  taxMethod: 'FIFO' | 'LIFO' = 'FIFO'
): PortfolioStats {
  return useMemo(
    () => computePortfolioStats(transactions, liveAssets, taxMethod),
    [transactions, liveAssets, taxMethod]
  );
}