'use client';

// ── Engine-backed stats for the fund list ────────────────────────────────────
// The fund cards and totals must show the same book as the detail workspace.
// This hook reads each fund from the Python engine in parallel (bounded by the
// browser/runtime, not by a second lot engine) and maps the result with the
// shared pure mapper. If a fund cannot be read, the caller keeps its labelled
// client preview rather than hiding the failure.

import { useEffect, useMemo, useState } from 'react';
import type { PortfolioStats } from '@/hooks/use-portfolio-engine';
import type { MarksMap } from '@/lib/harness/clients/portfolio-client';
import { portfolioReadToStats } from '@/lib/portfolio/engine-stats';
import { readEngineFund } from '@/lib/portfolio/engine-funds';
import type { Fund } from '@/lib/portfolio/types';
import type { LiveQuote } from './use-live-quotes';

export interface EngineFundStats {
  stats: Map<string, PortfolioStats>;
  loading: boolean;
  error: string | null;
  /** True once at least one fund came from the engine. */
  anyEngine: boolean;
}

export function useEngineFundStats(
  funds: Fund[],
  quotes: Record<string, LiveQuote>,
): EngineFundStats {
  const marks = useMemo<MarksMap>(() => {
    const out: MarksMap = {};
    for (const [symbol, quote] of Object.entries(quotes)) {
      if (quote?.price == null || !Number.isFinite(quote.price)) continue;
      out[symbol] = { price: String(quote.price), currency: quote.currency, source: 'ui-live-quote' };
    }
    return out;
  }, [quotes]);

  const [state, setState] = useState<{ stats: Map<string, PortfolioStats>; loading: boolean; error: string | null }>({
    stats: new Map(),
    loading: true,
    error: null,
  });

  useEffect(() => {
    let cancelled = false;
    if (funds.length === 0) {
      setState({ stats: new Map(), loading: false, error: null });
      return () => { cancelled = true; };
    }
    setState((current) => ({ ...current, loading: true }));
    Promise.all(funds.map(async (fund) => {
      try {
        const read = await readEngineFund(fund.id, marks);
        return [fund.id, portfolioReadToStats(read, quotes)] as const;
      } catch {
        return null;
      }
    })).then((results) => {
      if (cancelled) return;
      const stats = new Map<string, PortfolioStats>();
      for (const result of results) {
        if (result) stats.set(result[0], result[1]);
      }
      setState({
        stats,
        loading: false,
        error: stats.size === 0 ? 'portfolio engine unavailable' : null,
      });
    });
    return () => { cancelled = true; };
  }, [funds, marks, quotes]);

  return {
    ...state,
    anyEngine: state.stats.size > 0,
  };
}
