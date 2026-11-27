'use client';

// ── Fund manager (portfolio overview) ────────────────────────────────────────
// Fund tabs + overall performance chart + market-news-style sentiment cards.

import React, { useMemo, useRef, useState } from 'react';
import {
  Briefcase, Car, ChartPie, Coins, FileJson, FileSpreadsheet, Flame, Gem, GraduationCap,
  Heart, Home, Landmark, PiggyBank, Plane, PencilLine, Plus, Rocket, Shield,
  Sprout, Target, Trash2, TrendingUp, Upload,
} from '@/lib/icons';
import { cn } from '@/lib/utils';
import { usePageShortcuts } from '@/hooks/use-page-shortcuts';
import { useChartPalette } from '@/lib/chart-palette-store';
import { fmtK, fmtPct, fmtUsd } from '@/lib/portfolio-analysis';
import { computePortfolioStats, type PortfolioStats, type LiveAssetInput } from '@/hooks/use-portfolio-engine';
import { usePriceHistories } from '@/hooks/use-price-histories';
import { computeRiskMetrics } from '@/lib/portfolio/risk';
import { baseChartOption, useEChart } from './use-echart';
import { marketHintFor } from './use-live-quotes';
import { DotGrid } from '@/components/ui/ascii-loader';
import { PageHeader } from '@/components/layout/page-header';
import {
  exportAllFundsJson, exportFundCsv, exportFundJson, isDemoFund, parseFundsPayload,
} from './fund-store';
import { useLiveQuotes } from './use-live-quotes';
import { useEngineFundStats } from './use-engine-fund-stats';
import { AddTransactionDialog, ConfirmDialog, CreateFundDialog, RenameFundDialog } from './fund-dialogs';
import type { Fund, Transaction } from '@/lib/portfolio/types';

const ICONS: Record<string, React.ComponentType<{ size?: number; className?: string; style?: React.CSSProperties }>> = {
  Briefcase, Rocket, Shield, Coins, Landmark, Sprout, Gem, Flame,
  Target, Home, GraduationCap, Plane, Car, Heart, PiggyBank, TrendingUp,
};

type Sentiment = 'profitable' | 'losing' | 'flat' | 'empty';

const BENCHES: Array<{ symbol: string; name: string; market: string }> = [
  { symbol: 'SPY', name: 'S&P 500', market: 'GLOB' },
  { symbol: 'QQQ', name: 'Nasdaq 100', market: 'GLOB' },
  { symbol: 'BTC-USD', name: 'Bitcoin', market: 'CRYPTO' },
];

function fundSentiment(s: PortfolioStats | undefined, pricesReady: boolean): Sentiment {
  // Never judge profitability from incomplete prices (engine returns 0 value).
  if (!s || (s.holdings.length > 0 && !pricesReady)) return 'flat';
  if (s.holdings.length === 0) return 'empty';
  if (s.unrealizedPnL > 0.005) return 'profitable';
  if (s.unrealizedPnL < -0.005) return 'losing';
  return 'flat';
}

const SENTIMENT_META: Record<Sentiment, { label: string; border: string; bg: string; glow: string; text: string }> = {
  profitable: {
    label: 'Profitable',
    border: 'border-emerald-500/25 dark:border-emerald-500/15 hover:border-emerald-500/60',
    bg: 'bg-gradient-to-br from-emerald-500/[0.03] via-[#0f0f0f]/60 to-[#0f0f0f]/30',
    glow: 'bg-emerald-500', text: 'text-emerald-400',
  },
  losing: {
    label: 'Losing',
    border: 'border-rose-500/25 dark:border-rose-500/15 hover:border-rose-500/60',
    bg: 'bg-gradient-to-br from-rose-500/[0.03] via-[#0f0f0f]/60 to-[#0f0f0f]/30',
    glow: 'bg-rose-500', text: 'text-rose-400',
  },
  flat: {
    label: 'Flat',
    border: 'border-white/10 dark:border-white/5 hover:border-primary/50',
    bg: 'glass-card',
    glow: 'bg-primary', text: 'text-muted-foreground',
  },
  empty: {
    label: 'No Positions',
    border: 'border-white/10 dark:border-white/5 hover:border-primary/50',
    bg: 'glass-card',
    glow: 'bg-primary', text: 'text-muted-foreground',
  },
};

interface FundManagerProps {
  funds: Fund[] | null;
  onOpenFund: (fundId: string) => void;
  onCreate: (input: { name: string; icon: string; accent: string; goalValue: number | null; startingCash: number }) => Promise<void> | void;
  onUpdate: (fundId: string, patch: { name?: string; goalValue?: number | null }) => Promise<void> | void;
  onDelete: (fundId: string) => Promise<void> | void;
  onAddTransaction: (fundId: string, tx: Transaction) => Promise<void> | void;
  onImport: (funds: Fund[]) => Promise<void> | void;
}

export function FundManager({ funds, onOpenFund, onCreate, onUpdate, onDelete, onAddTransaction, onImport }: FundManagerProps) {
  const palette = useChartPalette();
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const [dialog, setDialog] = useState<
    { kind: 'create' } | { kind: 'rename'; fund: Fund } | { kind: 'delete'; fund: Fund }
    | { kind: 'addtx'; fund: Fund } | null
  >(null);
  const fileRef = useRef<HTMLInputElement>(null);

  // Live prices across every symbol in every fund for the ribbon, cards & chart.
  const symbols = useMemo(
    () => [...new Set((funds ?? []).flatMap(f => f.transactions.map(t => t.symbol.toUpperCase())))].filter(s => s && s !== 'CASH'),
    [funds],
  );
  const { quotes, loading } = useLiveQuotes(symbols);
  const histories = usePriceHistories(symbols, Object.fromEntries(symbols.map(s => [s, marketHintFor(s)])));
  // The cards/totals read the same engine as the detail workspace. A fund the
  // engine cannot answer keeps a client preview, shown as such in the header.
  const engineStats = useEngineFundStats(funds ?? [], quotes);

  const statsByFund = useMemo(() => {
    const map = new Map<string, PortfolioStats>();
    for (const f of funds ?? []) {
      const fromEngine = engineStats.stats.get(f.id);
      if (fromEngine) {
        map.set(f.id, fromEngine);
        continue;
      }
      const live: LiveAssetInput[] = [...new Set(f.transactions.map(t => t.symbol.toUpperCase()))]
        .filter(s => s && s !== 'CASH')
        .map(sym => ({
          symbol: sym,
          price: quotes[sym]?.price,
          changePercent: quotes[sym]?.changePercent,
          currency: quotes[sym]?.currency,
          name: quotes[sym]?.name,
        }));
      map.set(f.id, computePortfolioStats(f.transactions, live, 'FIFO'));
    }
    return map;
  }, [funds, quotes, engineStats.stats]);

  const totals = useMemo(() => {
    let value = 0, pnl = 0, holdings = 0;
    for (const f of funds ?? []) {
      const s = statsByFund.get(f.id);
      if (s) { value += s.totalValue; pnl += s.unrealizedPnL; holdings += s.holdings.length; }
    }
    return { value, pnl, holdings, count: funds?.length ?? 0 };
  }, [funds, statsByFund]);

  /** True once at least one live quote landed (until then, don't trust P/L math). */
  const pricesReady = Object.keys(quotes).length > 0;
  /**
   * Show placeholders only when neither the engine nor live quotes have priced
   * the book. Once the engine answers, its valuation is authoritative even if a
   * particular UI quote feed is still warming up.
   */
  const hasPositionsNeedingPrices = symbols.length > 0 && !pricesReady && !engineStats.anyEngine;
  const cardPlaceholder = hasPositionsNeedingPrices;

  // ── Overall performance: one NAV line per fund + standard benchmarks ────────
  const benchSymbols = useMemo(() => BENCHES.map(b => b.symbol), []);
  const benchHist = usePriceHistories(benchSymbols, Object.fromEntries(BENCHES.map(b => [b.symbol, b.market])));

  const series = useMemo(() => {
    const out: Array<{ name: string; color: string; nav: number[]; dates: string[]; dashed: boolean }> = [];
    for (const f of funds ?? []) {
      const s = statsByFund.get(f.id);
      if (!s || s.holdings.length === 0) continue;
      const r = computeRiskMetrics(
        s.holdings.map(h => ({ symbol: h.symbol, weight: h.valueUsd })),
        histories.closes, histories.dates, [],
      );
      if (r.nav.length >= 2) out.push({ name: f.name, color: f.accent, nav: r.nav, dates: r.navDates, dashed: false });
    }
    for (const b of BENCHES) {
      const r = computeRiskMetrics([{ symbol: b.symbol, weight: 1 }], benchHist.closes, benchHist.dates, []);
      if (r.nav.length >= 2) out.push({ name: b.name, color: palette.lines.series[BENCHES.indexOf(b) % palette.lines.series.length], nav: r.nav, dates: r.navDates, dashed: true });
    }
    return out;
  }, [funds, statsByFund, histories, benchHist, palette.lines.series]);

  const chartRef = useEChart(() => {
    if (series.length === 0) return null;
    const len = Math.min(...series.map(x => x.nav.length));
    const navs = series.map(x => x.nav.slice(-len));
    return {
      ...baseChartOption(),
      grid: { left: 8, right: 8, top: 14, bottom: 30, containLabel: true },
      xAxis: {
        type: 'category',
        data: series[0].dates.length >= len ? series[0].dates.slice(-len) : Array.from({ length: len }, (_, i) => i),
        axisLine: { lineStyle: { color: 'rgba(148,163,184,0.04)' } },
        axisLabel: { color: '#94a3b8', fontSize: 9 },
      },
      yAxis: {
        type: 'value', scale: true,
        splitLine: { lineStyle: { color: 'rgba(148,163,184,0.03)' } },
        axisLabel: { color: '#94a3b8', fontSize: 9, formatter: (v: number) => v.toFixed(2) },
      },
      series: navs.map((nav, i) => ({
        name: series[i].name,
        type: 'line' as const,
        data: nav.map(v => Number(v.toFixed(4))),
        showSymbol: false,
        lineStyle: { width: series[i].dashed ? 1.2 : 2, color: series[i].color, type: series[i].dashed ? ('dashed' as const) : ('solid' as const) },
        emphasis: { focus: 'series' as const },
      })),
      legend: {
        bottom: 0, itemWidth: 14, textStyle: { color: '#94a3b8', fontSize: 9 },
        data: series.map(x => x.name),
      },
      tooltip: {
        trigger: 'axis',
        backgroundColor: 'rgba(18,18,20,0.5)', appendTo: 'body', borderColor: 'rgba(255,255,255,0.12)', borderWidth: 1, padding: [10, 12],
        textStyle: { color: '#e5e7eb', fontSize: 11 },
        extraCssText: 'backdrop-filter: blur(18px) saturate(1.35); border-radius: 14px; box-shadow: 0 8px 30px rgba(0,0,0,0.45), inset 0 1px 0 rgba(255,255,255,0.06);',
        valueFormatter: (v: unknown) => (typeof v === 'number' ? `${((v - 1) * 100).toFixed(2)}%` : String(v)),
      },
    };
  }, [series]);

  const onImportFile = async (file: File) => {
    const text = await file.text();
    const res = parseFundsPayload(text);
    if (res.funds.length) await onImport(res.funds);
    setImportStatus(res.errors.length ? `Imported with warnings: ${res.errors.join(' ')}` : `Imported ${res.funds.length} fund(s).`);
    setTimeout(() => setImportStatus(null), 6000);
  };
  const [importStatus, setImportStatus] = useState<string | null>(null);

  const rootRef = useRef<HTMLDivElement>(null);
  usePageShortcuts(rootRef, [
    { id: 'funds.new', label: 'Create fund', keys: 'Shift+N', run: () => setDialog({ kind: 'create' }), enabled: funds !== null && dialog === null },
  ]);

  if (funds === null) {
    return (
      <div className="flex flex-col items-center gap-3 py-24 animate-fade-in">
        <DotGrid size={32} />
        <span className="text-xs text-muted-foreground font-mono">loading funds…</span>
      </div>
    );
  }

  return (
    <div ref={rootRef} data-keyboard-scope="Fund manager" className="animate-fade-in-up w-full">
      {/* Header + toolbar */}
      <PageHeader
        icon={ChartPie}
        title="Portfolio"
        description="Funds and custom workspaces, backed by your local Eclipse portfolio engine."
        size="lg"
        actions={
          <div className="flex flex-wrap items-center gap-1.5 pr-40">
            <input
              ref={fileRef} type="file" accept=".json,.csv" className="hidden"
              onChange={(e) => { const f = e.target.files?.[0]; if (f) void onImportFile(f); e.target.value = ''; }}
            />
            <ToolbarButton icon={Upload} label="Import" onClick={() => fileRef.current?.click()} />
            <ToolbarButton icon={FileJson} label="Export all" onClick={() => exportAllFundsJson(funds)} />
            <ToolbarButton icon={Plus} label="Create fund" primary onClick={() => setDialog({ kind: 'create' })} />
          </div>
        }
      />
      {importStatus && (
        <p className="mb-4 rounded-xl border border-primary/20 bg-primary/5 px-3 py-2 text-xs font-semibold text-primary animate-fade-in">{importStatus}</p>
      )}

      {/* Fund tabs: Overview + one tab per fund */}
      <div className="fund-tabs-glass mb-5 flex items-center gap-2 p-1.5 rounded-3xl shrink-0 overflow-x-auto scrollbar-hide">
        <span className="pl-3 pr-1 text-[9px] uppercase tracking-widest font-bold text-muted-foreground shrink-0">
          {funds.length} fund{funds.length === 1 ? '' : 's'}
        </span>
        {funds.map((fund) => {
          const Icon = ICONS[fund.icon] ?? Briefcase;
          const sent = fundSentiment(statsByFund.get(fund.id), pricesReady);
          return (
            <button
              key={fund.id}
              onClick={() => onOpenFund(fund.id)}
              className={cn(
                'flex items-center gap-1.5 px-4 py-2 rounded-2xl text-xs font-bold transition-all cursor-pointer whitespace-nowrap shrink-0',
                'bg-accent/30 text-muted-foreground hover:text-foreground hover:bg-accent/60',
              )}
              title={SENTIMENT_META[sent].label}
            >
              <Icon size={12} />
              {fund.name}
            </button>
          );
        })}
        <span
          title={engineStats.error
            ? `${engineStats.error}; fund cards use a labelled client preview.`
            : engineStats.anyEngine
              ? 'Card totals come from the Python Portfolio Accounting Engine.'
              : 'Waiting for the Python engine…'}
          className={cn(
            'ml-auto shrink-0 rounded-full border px-2.5 py-1 font-mono text-[9px] font-bold uppercase tracking-wider',
            engineStats.anyEngine
              ? 'border-emerald-400/30 bg-emerald-400/10 text-emerald-300'
              : 'border-amber-400/30 bg-amber-400/10 text-amber-300',
          )}
        >
          {engineStats.anyEngine ? 'engine-backed' : 'preview'}
        </span>
      </div>

      {/* Stats ribbon */}
      <div className="mb-5 grid grid-cols-2 md:grid-cols-4 gap-3">
        <RibbonCard label="Total Value" value={fmtUsd(totals.value)} loading={hasPositionsNeedingPrices} />
        <RibbonCard label="Unrealized P/L" value={fmtUsd(totals.pnl)} tone={hasPositionsNeedingPrices ? undefined : totals.pnl} loading={hasPositionsNeedingPrices} />
        <RibbonCard label="Funds" value={String(totals.count)} />
        <RibbonCard label="Holdings" value={String(totals.holdings)} />
      </div>

      {/* Overall performance chart: every fund vs standard benchmarks */}
      <div className="mb-6 rounded-3xl glass-card p-4 md:p-5 animate-fade-in-up">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border/10 pb-2.5 mb-3">
          <div className="flex items-center gap-2">
            <TrendingUp size={14} className="text-primary" />
            <h3 className="font-serif text-sm font-semibold text-foreground">Overall Performance</h3>
            <span className="font-mono text-[9px] text-muted-foreground/70">FUNDS vs BENCHMARKS · 3MO · INDEXED</span>
          </div>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            {series.map(x => {
              const ret = ((x.nav[x.nav.length - 1] / x.nav[0]) - 1) * 100;
              return (
                <span key={x.name} className="flex items-center gap-1.5 font-mono text-[10px]">
                  <span className="h-1.5 w-1.5 rounded-full" style={{ background: x.color }} />
                  <span className="text-muted-foreground">{x.name}</span>
                  <span className={cn('font-bold', ret >= 0 ? 'text-emerald-400' : 'text-rose-400')}>{fmtPct(ret)}</span>
                </span>
              );
            })}
          </div>
        </div>
        {series.length > 0
          ? <div ref={chartRef} className="h-64" />
          : (
            <div className="h-32 flex items-center justify-center gap-2">
              {(hasPositionsNeedingPrices || loading) ? (
                <span className="flex items-center gap-2 font-mono text-[10px] text-muted-foreground/70">
                  <span className="h-4 w-4 animate-spin rounded-full border-2 border-muted-foreground/30 border-t-muted-foreground/70" aria-hidden />
                  <span className="font-bold">{hasPositionsNeedingPrices ? 'waiting for live prices' : 'loading price history'}</span>
                </span>
              ) : (
                <p className="text-xs text-muted-foreground/70 italic">Add holdings across your funds to see combined performance.</p>
              )}
            </div>
          )}
      </div>

      {/* Fund cards (market-news style) */}
      {funds.length === 0 ? (
        <div className="rounded-3xl glass-card p-12 text-center animate-fade-in-up">
          <Briefcase className="mx-auto text-primary" size={28} />
          <h3 className="mt-3 font-serif text-lg text-foreground">No funds yet</h3>
          <p className="mt-1 text-xs text-muted-foreground">Create your first fund to start tracking positions and performance.</p>
          <button
            onClick={() => setDialog({ kind: 'create' })}
            className="mt-5 inline-flex items-center gap-1.5 rounded-xl border border-primary/40 bg-primary/15 text-primary px-4 py-2.5 text-xs font-bold hover:bg-primary/25 transition-all cursor-pointer"
          >
            <Plus size={13} /> Create fund
          </button>
        </div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {funds.map((fund, i) => {
            const Icon = ICONS[fund.icon] ?? Briefcase;
            const s = statsByFund.get(fund.id);
            const sent = fundSentiment(s, pricesReady);
            const meta = SENTIMENT_META[sent];
            const goalPct = pricesReady && fund.goalValue && s ? Math.min(100, (s.totalValue / fund.goalValue) * 100) : null;
            const dayPnl = s?.dailyChangeValue ?? 0;
            const dividends = s?.dividendsUsd ?? 0;
            return (
              <div
                key={fund.id}
                onClick={() => onOpenFund(fund.id)}
                className={cn(
                  'group relative cursor-pointer rounded-2xl glass-card hover:shadow-lg transition-all duration-300',
                  'flex flex-col justify-between p-5 overflow-hidden shadow-sm hover:scale-[1.005] animate-fade-in-up',
                  meta.bg,
                )}
                style={{ animationDelay: `${i * 60}ms` }}
              >
                {/* Ambient sentiment glow (market-news style corner blur) */}
                <div className={cn(
                  'absolute top-0 right-0 -translate-y-1/2 translate-x-1/2 w-48 h-48 rounded-full blur-[60px] pointer-events-none opacity-20 transition-all duration-300',
                  meta.glow,
                )} />

                <div className="relative z-10 space-y-2.5">
                  {/* Badge row */}
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="flex items-center gap-1.5 text-[10px] font-bold text-muted-foreground font-mono uppercase bg-accent/60 px-2 py-0.5 rounded border border-white/10 dark:border-white/10">
                      <Icon size={10} style={{ color: fund.accent }} /> {isDemoFund(fund) ? 'demo' : 'custom'}
                    </span>
                    <span className={cn('text-[9px] font-bold uppercase tracking-wider px-2.5 py-0.5 rounded-full border', meta.text,
                      sent === 'profitable' ? 'border-emerald-500/25 bg-emerald-500/5'
                        : sent === 'losing' ? 'border-rose-500/25 bg-rose-500/5'
                          : 'border-white/10 dark:border-white/10 bg-accent/40')}>
                      {meta.label}
                    </span>
                    <span className="text-[10px] font-bold text-primary/80 font-mono uppercase bg-primary/5 border border-primary/15 px-2 py-0.5 rounded">
                      {fund.transactions.length} tx
                    </span>
                    <span className="text-[10px] text-muted-foreground font-mono">
                      since {new Date(fund.createdAt).toLocaleDateString(undefined, { month: 'short', year: 'numeric' })}
                    </span>
                  </div>

                  {/* Big icon + title + sentiment readout */}
                  <div className="flex items-center justify-between gap-3">
                    <div className="flex items-center gap-3 min-w-0">
                      <span className="flex h-12 w-12 shrink-0 items-center justify-center rounded-2xl border border-white/10 dark:border-white/10"
                        style={{ background: `${fund.accent}1f`, color: fund.accent }}>
                        <Icon size={24} />
                      </span>
                      <h3 className="font-serif text-lg font-semibold leading-snug text-foreground group-hover:text-primary transition-colors truncate">
                        {fund.name}
                      </h3>
                    </div>
                    <div className="text-right shrink-0">
                      {cardPlaceholder ? (
                        <span className="block h-5 w-5 ml-auto animate-spin rounded-full border-2 border-muted-foreground/30 border-t-muted-foreground/70" aria-hidden />
                      ) : (
                        <p className="font-mono text-base font-bold text-foreground">
                          {s && s.totalValue ? fmtUsd(s.totalValue) : '—'}
                        </p>
                      )}
                      <p className={cn('font-mono text-[11px] font-bold',
                        !cardPlaceholder && (s?.roiPercent ?? 0) > 0 ? 'text-emerald-400' : !cardPlaceholder && (s?.roiPercent ?? 0) < 0 ? 'text-rose-400' : 'text-muted-foreground')}>
                        {cardPlaceholder ? '' : s && s.costBasis ? `${fmtPct(s.roiPercent)} ROI` : 'no cost basis'}
                      </p>
                    </div>
                  </div>

                  {/* Detail grid */}
                  <div className="grid grid-cols-3 gap-2 pt-0.5">
                    <CardStat label="Holdings" value={String(s?.holdings.length ?? 0)} />
                    <CardStat label="Day P/L" value={s && (s.totalValue || dayPnl) ? fmtUsd(dayPnl) : '—'} tone={dayPnl} loading={cardPlaceholder} />
                    <CardStat label="Dividends" value={dividends ? fmtK(dividends) : '—'} tone={dividends > 0 ? 1 : undefined} />
                  </div>

                  {/* Top positions line */}
                  {s && s.holdings.length > 0 && pricesReady && (
                    <p className="text-[10px] text-muted-foreground font-mono truncate">
                      Top: {s.holdings.slice(0, 3).map(h => `${h.symbol} ${(h.allocation).toFixed(0)}%`).join(' · ')}
                      {s.holdings.length > 3 ? ` · +${s.holdings.length - 3} more` : ''}
                    </p>
                  )}

                  {/* Goal progress */}
                  {goalPct != null && (
                    <div>
                      <div className="flex items-center justify-between text-[9px] font-bold text-muted-foreground mb-1">
                        <span>GOAL {fmtUsd(fund.goalValue ?? 0, 0)}</span>
                        <span className="font-mono">{goalPct.toFixed(0)}%</span>
                      </div>
                      <div className="h-1 rounded-full bg-border/40 overflow-hidden">
                        <div
                          className={cn('h-full rounded-full transition-all duration-500',
                            sent === 'profitable' ? 'bg-emerald-400' : sent === 'losing' ? 'bg-rose-400' : 'bg-muted-foreground/60')}
                          style={{ width: `${goalPct}%` }}
                        />
                      </div>
                    </div>
                  )}
                </div>

                {/* Card actions */}
                <div className="relative z-10 mt-3 flex items-center justify-between border-t border-border/10 pt-3">
                  <span className="inline-flex items-center gap-1 text-xs font-semibold text-primary opacity-0 group-hover:opacity-100 transition-opacity">
                    Open workspace →
                  </span>
                  <button
                    onClick={(e) => { e.stopPropagation(); setMenuFor(menuFor === fund.id ? null : fund.id); }}
                    className="rounded-lg p-1 text-muted-foreground hover:text-foreground hover:bg-accent cursor-pointer transition-all"
                    aria-label="Fund actions"
                  >
                    <span className="text-xs font-bold tracking-widest">•••</span>
                  </button>
                </div>

                {menuFor === fund.id && (
                  <div
                    onClick={e => e.stopPropagation()}
                    className="fund-menu-glass absolute right-3 bottom-12 z-20 w-48 rounded-xl p-1 animate-fade-in"
                  >
                    <MenuItem icon={PencilLine} label="Rename / goal" onClick={() => { setMenuFor(null); setDialog({ kind: 'rename', fund }); }} />
                    <MenuItem icon={Plus} label="Add transaction" onClick={() => { setMenuFor(null); setDialog({ kind: 'addtx', fund }); }} />
                    <MenuItem icon={FileJson} label="Export JSON" onClick={() => { setMenuFor(null); exportFundJson(fund); }} />
                    <MenuItem icon={FileSpreadsheet} label="Export CSV" onClick={() => { setMenuFor(null); exportFundCsv(fund); }} />
                    <MenuItem
                      icon={Trash2} label={isDemoFund(fund) ? 'Delete (locked demo)' : 'Delete'} danger
                      disabled={isDemoFund(fund)}
                      onClick={() => { setMenuFor(null); setDialog({ kind: 'delete', fund }); }}
                    />
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* Dialogs */}
      {dialog?.kind === 'create' && (
        <CreateFundDialog
          onClose={() => setDialog(null)}
          onCreate={(name, icon, accent, goal, startingCash) => {
            void onCreate({ name, icon, accent, goalValue: goal, startingCash });
            setDialog(null);
          }}
        />
      )}
      {dialog?.kind === 'rename' && (
        <RenameFundDialog
          current={dialog.fund}
          onClose={() => setDialog(null)}
          onRename={(name, goal) => {
            void onUpdate(dialog.fund.id, { name, goalValue: goal });
            setDialog(null);
          }}
        />
      )}
      {dialog?.kind === 'delete' && (
        <ConfirmDialog
          title={`Delete "${dialog.fund.name}"?`}
          body="The fund and its transaction history will be removed from the engine. This cannot be undone."
          confirmLabel="Delete fund"
          onClose={() => setDialog(null)}
          onConfirm={() => {
            void onDelete(dialog.fund.id);
            setDialog(null);
          }}
        />
      )}
      {dialog?.kind === 'addtx' && (
        <AddTransactionDialog
          onClose={() => setDialog(null)}
          onAdd={(tx: Transaction) => {
            void onAddTransaction(dialog.fund.id, tx);
            setDialog(null);
          }}
        />
      )}
    </div>
  );
}

function CardStat({ label, value, tone, loading }: { label: string; value: string; tone?: number; loading?: boolean }) {
  return (
    <div className="rounded-xl glass-card-subtle border border-white/10 dark:border-white/10 px-2.5 py-1.5 min-w-0">
      <p className="text-[8px] uppercase tracking-widest font-bold text-muted-foreground truncate">{label}</p>
      {loading ? (
        <span className="mt-0.5 block h-3.5 w-3.5 animate-spin rounded-full border-2 border-muted-foreground/30 border-t-muted-foreground/70" aria-hidden />
      ) : (
        <p className={cn('font-mono text-[11px] font-bold truncate',
          tone == null ? 'text-foreground' : tone > 0 ? 'text-emerald-400' : tone < 0 ? 'text-rose-400' : 'text-foreground')}>
          {value}
        </p>
      )}
    </div>
  );
}

function ToolbarButton({ icon: Icon, label, onClick, primary }: {
  icon: React.ComponentType<{ size?: number; className?: string }>; label: string; onClick: () => void; primary?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        'inline-flex items-center gap-1.5 rounded-xl border px-3 py-2 text-xs font-bold transition-all cursor-pointer',
        primary
          ? 'border-primary/40 bg-primary/15 text-primary hover:bg-primary/25'
          : 'border-white/15 dark:border-white/10 bg-accent/30 text-muted-foreground hover:text-primary hover:border-primary/40 hover:bg-accent/60',
      )}
    >
      <Icon size={13} /> {label}
    </button>
  );
}

function RibbonCard({ label, value, tone, loading }: { label: string; value: string; tone?: number; loading?: boolean }) {
  return (
    <div className="rounded-2xl glass-card p-3.5">
      <p className="text-[9px] uppercase tracking-widest font-bold text-muted-foreground">{label}</p>
      {loading ? (
        <span className="mt-2 block h-5 w-5 animate-spin rounded-full border-2 border-muted-foreground/30 border-t-muted-foreground/70" aria-hidden />
      ) : (
        <p className={cn('mt-1 font-mono text-lg font-bold truncate',
          tone == null ? 'text-foreground' : tone > 0 ? 'text-emerald-400' : tone < 0 ? 'text-rose-400' : 'text-foreground')}>
          {value}
        </p>
      )}
    </div>
  );
}

function MenuItem({ icon: Icon, label, onClick, danger, disabled }: {
  icon: React.ComponentType<{ size?: number; className?: string }>; label: string; onClick: () => void; danger?: boolean; disabled?: boolean;
}) {
  return (
    <button
      onClick={disabled ? undefined : onClick}
      disabled={disabled}
      className={cn(
        'flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-xs font-semibold transition-all',
        disabled ? 'opacity-40 cursor-not-allowed text-muted-foreground'
          : danger ? 'text-rose-400 hover:bg-rose-500/10 cursor-pointer'
            : 'text-muted-foreground hover:text-foreground hover:bg-accent/50 cursor-pointer',
      )}
    >
      <Icon size={13} /> {label}
    </button>
  );
}




