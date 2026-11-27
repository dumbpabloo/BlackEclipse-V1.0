'use client';

// ── Fund widgets (pure) ──────────────────────────────────────────────────────
// Every widget consumes the engines' computed outputs via props and reports
// null/empty where data is missing — never fabricates. All edit chrome lives
// in the kernel shells; these components render content only.

import React, { useMemo, useState } from 'react';
import { createPortal } from 'react-dom';
import type { EChartsOption } from 'echarts';
import {
  Activity, Brain, ChartLine, Gauge, Grid3x3, HeartPulse, Layers, Maximize2,
  Minimize2, PieChart, Table2, Wallet,
} from '@/lib/icons';
import { cn } from '@/lib/utils';
import { fmtK, fmtPct, fmtUsd } from '@/lib/portfolio-analysis';
import { computeMonteCarlo } from '@/lib/portfolio-analysis';
import { MIN_OBSERVATIONS } from '@/lib/portfolio/matrix';
import { useChartPalette } from '@/lib/chart-palette-store';
import { normalizeLayout } from '@/lib/workspace/ops';
import { rebalanceColumns } from '@/lib/workspace/pack';
import type { WorkspaceColumn, WorkspaceLayout, WorkspaceSection, WidgetPlacement } from '@/lib/workspace/types';
import type { WidgetDefinition } from '@/lib/workspace/widget-def';
import { baseChartOption, useEChart } from './use-echart';
import { EmptyNote, Tile, WidgetHeader, type WidgetProps } from './widget-primitives';
import { TaxLotsWidget } from './tax-lots-widget';

export type { WidgetProps } from './widget-primitives';

const UP = 'text-emerald-400';
const DOWN = 'text-rose-400';
const toneClass = (n: number) => (n > 0 ? UP : n < 0 ? DOWN : 'text-muted-foreground');

// ── KPI board ────────────────────────────────────────────────────────────────

export function KpiBoardWidget({ data }: WidgetProps) {
  const s = data.stats;
  const holdingsCount = s.holdings.length;
  const ld = data.loading && !data.ready;
  return (
    <div className="flex h-full flex-col p-4">
      <WidgetHeader icon={Wallet} title="Key Metrics" hint={s.base} />
      <div className="grid flex-1 auto-rows-fr grid-cols-2 gap-2.5 lg:grid-cols-4">
        <Tile label="Total Value" value={fmtUsd(s.totalValue)} sub={`Cash ${fmtUsd(data.cash)}`} loading={ld} />
        <Tile label="Unrealized P/L" value={fmtUsd(s.unrealizedPnL)} tone={toneClass(s.unrealizedPnL)} sub={fmtPct(s.roiPercent)} loading={ld} />
        <Tile label="Day Change" value={fmtUsd(s.dailyChangeValue)} tone={toneClass(s.dailyChangeValue)} sub={fmtPct(s.dailyChangePercent)} loading={ld} />
        <Tile label="Holdings" value={String(holdingsCount)} sub={`Realized ${fmtUsd(s.realizedPnL)}`} />
      </div>
      {holdingsCount === 0 && <EmptyNote>No open positions — add a transaction to this fund.</EmptyNote>}
      {data.engine.dataQuality && data.engine.dataQuality.status !== 'OK' && (
        <p
          className="mt-2 shrink-0 rounded-lg border border-amber-400/20 bg-amber-400/5 px-2 py-1 font-mono text-[9px] leading-relaxed text-amber-300/90"
          title={(data.engine.dataQuality.warnings ?? []).join(' · ')}
        >
          Data quality {data.engine.dataQuality.status}
          {data.engine.dataQuality.missingPrices.length > 0
            ? ` · no price: ${data.engine.dataQuality.missingPrices.join(', ')}`
            : ''}
          {data.engine.dataQuality.missingFX.length > 0
            ? ` · no FX: ${data.engine.dataQuality.missingFX.join(', ')}`
            : ''}
        </p>
      )}
      {data.engine.source === 'preview' && (
        <p className="mt-2 shrink-0 rounded-lg border border-amber-400/20 bg-amber-400/5 px-2 py-1 font-mono text-[9px] leading-relaxed text-amber-300/90">
          Preview only — the Python portfolio engine has not answered, so cost basis and realized P&amp;L
          may differ from the book of record.
        </p>
      )}
    </div>
  );
}

// ── Performance (NAV vs benchmark) ───────────────────────────────────────────

export function PerformanceWidget({ data }: WidgetProps) {
  const primary = useChartPalette().lines.primary;
  const benchColor = useChartPalette().lines.series[0];
  const hasNav = data.risk.nav.length >= 2;
  const ref = useEChart(() => {
    if (!hasNav) return null;
    const grid = { left: 8, right: 8, top: 10, bottom: 0, containLabel: true };
    return {
      ...baseChartOption(),
      grid,
      xAxis: { type: 'category', data: data.risk.navDates.length ? data.risk.navDates : data.risk.nav.map((_, i) => i), axisLine: { lineStyle: { color: 'rgba(148,163,184,0.04)' } }, axisLabel: { color: '#94a3b8', fontSize: 9 } },
      yAxis: { type: 'value', scale: true, splitLine: { lineStyle: { color: 'rgba(148,163,184,0.03)' } }, axisLabel: { color: '#94a3b8', fontSize: 9, formatter: (v: number) => v.toFixed(2) } },
      series: [
        { name: 'Fund NAV', type: 'line', data: data.risk.nav, showSymbol: false, lineStyle: { width: 2, color: primary }, areaStyle: { color: primary, opacity: 0.08 } },
        ...(data.risk.benchmarkNav.length === data.risk.nav.length
          ? [{ name: 'SPY', type: 'line' as const, data: data.risk.benchmarkNav, showSymbol: false, lineStyle: { width: 1, color: benchColor, type: 'dashed' as const } }]
          : []),
      ],
      legend: data.risk.benchmarkNav.length ? { bottom: 0, textStyle: { color: '#94a3b8', fontSize: 9 }, itemWidth: 12 } : undefined,
    };
  }, [hasNav, data.risk.nav, data.risk.benchmarkNav, data.risk.navDates, primary]);

  return (
    <div className="flex h-full flex-col p-4">
      <WidgetHeader icon={ChartLine} title="Performance" hint="NAV · 3MO" />
      {hasNav ? <div ref={ref} className="min-h-[160px] flex-1" />
        : <EmptyNote>Price history unavailable for current holdings yet.</EmptyNote>}
    </div>
  );
}

// ── Allocation ───────────────────────────────────────────────────────────────

const DONUT_COLORS = ['#fafafa', '#a1a1aa', '#d4d4d8', '#71717a', '#e4e4e7', '#52525b', '#c4c4cc', '#8b8b94', '#a3a3a3'];

export function AllocationWidget({ data }: WidgetProps) {
  const holdings = data.stats.holdings;
  // The user's categorical palette (Settings › General › Charts), so an
  // allocation donut is not the one chart that ignores their colours.
  const paletteSeries = useChartPalette().lines.series;
  const donutColors = paletteSeries.length > 0 ? paletteSeries : DONUT_COLORS;
  const ref = useEChart(() => {
    if (!holdings.length) return null;
    return {
      ...baseChartOption(),
      tooltip: { trigger: 'item', backgroundColor: 'rgba(18,18,20,0.5)', appendTo: 'body', borderColor: 'rgba(255,255,255,0.12)', borderWidth: 1, padding: [10, 12], textStyle: { color: '#e5e7eb', fontSize: 11 }, extraCssText: 'backdrop-filter: blur(18px) saturate(1.35); border-radius: 14px; box-shadow: 0 8px 30px rgba(0,0,0,0.45), inset 0 1px 0 rgba(255,255,255,0.06);' },
      series: [{
        type: 'pie', radius: ['58%', '82%'], center: ['50%', '50%'],
        label: { show: false }, silent: false,
        data: holdings.slice(0, 9).map((h, i) => ({ name: h.symbol, value: Number(h.valueUsd.toFixed(2)), itemStyle: { color: donutColors[i % donutColors.length] } })),
      }],
    };
  }, [holdings]);

  if (!holdings.length) {
    return (
      <div className="flex h-full flex-col p-4">
        <WidgetHeader icon={PieChart} title="Allocation" />
        <EmptyNote>No holdings to allocate yet.</EmptyNote>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col p-4">
      <WidgetHeader icon={PieChart} title="Allocation" hint={`${holdings.length} POS`} />
      <div ref={ref} className="min-h-[120px] flex-1" />
      <div className="mt-2 flex min-h-0 shrink-0 flex-col gap-1">
        {holdings.map((h, i) => (
          <div key={h.symbol} className="flex items-center gap-2 text-xs">
            <span className="w-2 h-2 rounded-full shrink-0" style={{ background: donutColors[i % donutColors.length] }} />
            <span className="font-mono font-bold text-foreground w-16 truncate">{h.symbol}</span>
            <div className="flex-1 h-1 rounded-full bg-border/40 overflow-hidden">
              <div className="h-full rounded-full" style={{ width: `${Math.min(100, h.allocation)}%`, background: donutColors[i % donutColors.length] }} />
            </div>
            <span className="font-mono text-[10px] text-muted-foreground w-10 text-right">{h.allocation.toFixed(1)}%</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Holdings table ───────────────────────────────────────────────────────────

export function HoldingsWidget({ fund, data }: WidgetProps) {
  const holdings = data.stats.holdings;
  return (
    <div className="flex h-full flex-col p-4">
      <WidgetHeader icon={Table2} title="Holdings" hint={`${holdings.length}`} />
      {holdings.length === 0 ? <EmptyNote>No open positions.</EmptyNote> : (
        <div className="flex-1 overflow-auto custom-scrollbar rounded-xl glass-card-subtle shadow-inner shadow-black/20">
          <table className="w-full text-left border-separate border-spacing-0 text-xs">
            <thead className="sticky top-0 z-10">
              <tr className="glass-card-subtle">
                {['Symbol', 'Qty', 'Avg', 'Price', 'Value', 'P/L'].map((h, i) => (
                  <th key={h} className={cn(
                    'border-b border-white/10 py-2.5 px-3 text-[9px] uppercase tracking-widest text-muted-foreground/80 font-mono font-semibold whitespace-nowrap',
                    i === 0 ? 'text-left' : 'text-right',
                  )}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody className="font-mono">
              {holdings.map(h => (
                <tr key={h.symbol} className="group transition-colors hover:bg-white/[0.05]">
                  <td className="border-b border-white/5 py-2.5 px-3 font-bold text-foreground whitespace-nowrap">
                    <span className="flex items-center gap-1.5">
                      <span className="h-1.5 w-1.5 rounded-full shrink-0" style={{ background: fund?.accent ?? '#34d399' }} />
                      {h.symbol}
                      <span className="rounded border border-border/30 bg-accent/30 px-1 text-[8px] font-semibold text-muted-foreground">{h.currency}</span>
                    </span>
                  </td>
                  <td className="border-b border-white/5 py-2.5 px-3 text-right text-muted-foreground tabular-nums whitespace-nowrap">{h.quantity.toLocaleString()}</td>
                  <td className="border-b border-white/5 py-2.5 px-3 text-right text-muted-foreground tabular-nums whitespace-nowrap">{h.avgCost.toFixed(2)}</td>
                  <td className="border-b border-white/5 py-2.5 px-3 text-right text-foreground tabular-nums whitespace-nowrap">{h.currentPrice ? h.currentPrice.toFixed(2) : '—'}</td>
                  <td className="border-b border-white/5 py-2.5 px-3 text-right text-foreground font-bold tabular-nums whitespace-nowrap">{fmtK(h.valueUsd)}</td>
                  <td className={cn('border-b border-white/5 py-2.5 px-3 text-right font-bold tabular-nums whitespace-nowrap',
                    toneClass(h.unrealizedPnLUsd))}>
                    {h.unrealizedPnLUsd !== 0 ? fmtPct(h.avgCost > 0 ? (h.unrealizedPnLUsd / (h.avgCost * h.quantity)) * 100 : 0) : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ── Fund transactions ────────────────────────────────────────────────────────

export function TransactionsWidget({ fund, data }: WidgetProps) {
  const txs = useMemo(
    () => [...fund.transactions].sort((a, b) => b.date.localeCompare(a.date)).slice(0, 40),
    [fund.transactions],
  );

  return (
    <div className="flex h-full flex-col p-4">
      <WidgetHeader icon={Layers} title="Transactions" hint={`${fund.transactions.length} TX`} />
      {txs.length === 0 ? <EmptyNote>No transactions recorded yet.</EmptyNote> : (
        <div className="flex-1 overflow-auto custom-scrollbar rounded-xl glass-card-subtle shadow-inner shadow-black/20">
          <table className="w-full text-left border-separate border-spacing-0 text-xs">
            <thead className="sticky top-0 z-10">
              <tr className="glass-card-subtle">
                {['Date', 'Type', 'Symbol', 'Qty', 'Price', 'Fee'].map((h, i) => (
                  <th key={h} className={cn(
                    'border-b border-white/10 py-2.5 px-3 text-[9px] uppercase tracking-widest text-muted-foreground/80 font-mono font-semibold whitespace-nowrap',
                    i < 3 ? 'text-left' : 'text-right',
                  )}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody className="font-mono">
              {txs.map(t => (
                <tr key={t.id} className="transition-colors hover:bg-white/[0.05]">
                  <td className="border-b border-white/5 py-2.5 px-3 text-muted-foreground tabular-nums whitespace-nowrap">{t.date}</td>
                  <td className="border-b border-white/5 py-2.5 px-3 whitespace-nowrap">
                    <span className={cn('rounded-md border px-1.5 py-0.5 text-[9px] font-bold tracking-wide',
                      t.type === 'BUY' ? 'border-emerald-500/25 bg-emerald-500/10 text-emerald-400'
                        : t.type === 'SELL' ? 'border-rose-500/25 bg-rose-500/10 text-rose-400'
                          : t.type === 'DIVIDEND' ? 'border-amber-500/25 bg-amber-500/10 text-amber-400'
                            : 'border-sky-500/25 bg-sky-500/10 text-sky-400')}>
                      {t.type}
                    </span>
                  </td>
                  <td className="border-b border-white/5 py-2.5 px-3 font-bold text-foreground whitespace-nowrap">{t.symbol}</td>
                  <td className="border-b border-white/5 py-2.5 px-3 text-right text-muted-foreground tabular-nums whitespace-nowrap">{t.quantity.toLocaleString()}</td>
                  <td className="border-b border-white/5 py-2.5 px-3 text-right text-foreground tabular-nums whitespace-nowrap">{t.price.toLocaleString()}</td>
                  <td className="border-b border-white/5 py-2.5 px-3 text-right text-muted-foreground tabular-nums whitespace-nowrap">{t.fee ? t.fee.toFixed(2) : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="mt-2 font-mono text-[9px] text-muted-foreground/60">Cash position: {fmtUsd(data.cash)}</p>
    </div>
  );
}

// ── Risk metrics ─────────────────────────────────────────────────────────────

export function RiskWidget({ data }: WidgetProps) {
  const r = data.risk;
  const ld = data.loading && !data.ready;
  const metrics: Array<{ label: string; value: string; tone?: string }> = [
    { label: 'Beta vs SPY', value: r.beta != null ? r.beta.toFixed(2) : '—' },
    { label: 'Ann. Volatility', value: r.vol != null ? `${r.vol.toFixed(1)}%` : '—' },
    { label: 'Sharpe', value: r.sharpe != null ? r.sharpe.toFixed(2) : '—' },
    { label: 'Max Drawdown', value: r.mdd != null ? `${r.mdd.toFixed(1)}%` : '—', tone: r.mdd != null ? DOWN : undefined },
  ];
  return (
    <div className="flex h-full flex-col p-4">
      <WidgetHeader icon={Activity} title="Risk Metrics" hint="3MO DAILY" />
      <div className="grid flex-1 auto-rows-fr grid-cols-2 gap-2.5 lg:grid-cols-4">
        {metrics.map(m => <Tile key={m.label} label={m.label} value={m.value} tone={m.tone} loading={ld} />)}
      </div>
      {r.vol == null && <EmptyNote>Not enough price history to compute risk yet.</EmptyNote>}
    </div>
  );
}

// ── Portfolio health ─────────────────────────────────────────────────────────

export function HealthWidget({ data }: WidgetProps) {
  const health = data.health;
  const primary = useChartPalette().lines.primary;
  const ref = useEChart(() => {
    if (!health) return null;
    const value = Math.round(health.total);
    return {
      ...baseChartOption(),
      series: [{
        type: 'gauge', startAngle: 210, endAngle: -30, min: 0, max: 100,
        progress: { show: true, width: 10, itemStyle: { color: primary } },
        axisLine: { lineStyle: { width: 10, color: [[1, 'rgba(148,163,184,0.08)']] } },
        axisTick: { show: false }, splitLine: { show: false }, axisLabel: { show: false },
        pointer: { show: false }, anchor: { show: false },
        detail: { valueAnimation: false, offsetCenter: [0, '5%'], formatter: '{value}', color: '#e5e7eb', fontSize: 22, fontFamily: 'monospace' },
        data: [{ value }],
      }],
    };
  }, [health, primary]);

  if (!health) {
    return (
      <div className="flex h-full flex-col p-4">
        <WidgetHeader icon={HeartPulse} title="Portfolio Health" />
        <EmptyNote>Add holdings to score portfolio health.</EmptyNote>
      </div>
    );
  }

  const rows: Array<[string, number | null]> = [
    ['Diversification', health.breakdown.diversification],
    ['Risk', health.breakdown.risk],
    ['Momentum', health.breakdown.momentum],
    ['Concentration', health.breakdown.concentration],
    ['Valuation', health.breakdown.valuation],
    ['Quality', health.breakdown.quality],
  ];

  return (
    <div className="flex h-full flex-col p-4">
      <WidgetHeader icon={HeartPulse} title="Portfolio Health" />
      <div ref={ref} className="min-h-[120px] flex-1" />
      <div className="grid shrink-0 grid-cols-2 content-start gap-x-4 gap-y-1.5 mt-1">
        {rows.map(([label, v]) => (
          <div key={label} className="flex items-center gap-2 text-[10px]">
            <span className="w-20 shrink-0 text-muted-foreground font-semibold">{label}</span>
            {v == null
              ? <span className="font-mono text-muted-foreground/50">n/a</span>
              : <>
                  <div className="flex-1 h-1 rounded-full bg-border/40 overflow-hidden">
                    <div className={cn('h-full rounded-full', v >= 60 ? 'bg-emerald-400' : v >= 35 ? 'bg-amber-400' : 'bg-rose-400')} style={{ width: `${v}%` }} />
                  </div>
                  <span className="font-mono w-6 text-right text-foreground">{Math.round(v)}</span>
                </>}
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Monte Carlo projection ───────────────────────────────────────────────────

export function MonteCarloWidget({ data }: WidgetProps) {
  const [years, setYears] = useState(5);
  const [monthly, setMonthly] = useState(250);
  const vol = data.risk.vol ?? 40;
  const result = useMemo(
    () => data.stats.totalValue > 0
      ? computeMonteCarlo({ totalValue: data.stats.totalValue, vol, years, monthly })
      : null,
    [data.stats.totalValue, vol, years, monthly],
  );
  const palette = useChartPalette();

  const ref = useEChart(() => {
    if (!result) return null;
    return {
      ...baseChartOption(),
      grid: { left: 8, right: 8, top: 10, bottom: 0, containLabel: true },
      xAxis: { type: 'category', data: result.months.map(m => `${m}m`), axisLabel: { color: '#94a3b8', fontSize: 9, interval: Math.floor(result.months.length / 6) } },
      yAxis: { type: 'value', scale: true, splitLine: { lineStyle: { color: 'rgba(148,163,184,0.03)' } }, axisLabel: { color: '#94a3b8', fontSize: 9, formatter: (v: number) => fmtK(v) } },
      series: [
        { name: 'P95', type: 'line', data: result.p95, showSymbol: false, itemStyle: { color: palette.lines.primary }, lineStyle: { width: 1, color: palette.lines.primary }, areaStyle: { color: palette.lines.primary, opacity: 0.12 }, stack: 'band' },
        { name: 'P50', type: 'line', data: result.p50, showSymbol: false, itemStyle: { color: palette.lines.primary }, lineStyle: { width: 2, color: palette.lines.primary } },
        { name: 'P05', type: 'line', data: result.p05, showSymbol: false, itemStyle: { color: palette.candles.down }, lineStyle: { width: 1, color: palette.candles.down } },
      ],
      legend: { bottom: 0, textStyle: { color: '#94a3b8', fontSize: 9 }, itemWidth: 12 },
    };
  }, [result, palette]);

  const inputCls = 'w-full bg-background/60 border border-border/40 hover:border-primary/50 rounded-xl px-2.5 py-1.5 text-xs font-mono focus:ring-1 focus:ring-primary outline-none';

  return (
    <div className="flex h-full flex-col p-4">
      <WidgetHeader icon={Gauge} title="Monte Carlo Projection" hint="GBM P05/P50/P95" />
      {result ? <>
        <div className="grid grid-cols-3 gap-2 mb-2">
          <label className="flex flex-col gap-1 text-[9px] uppercase tracking-widest font-bold text-muted-foreground">
            Years
            <input type="number" min={1} max={30} value={years} onChange={e => setYears(Math.max(1, Math.min(30, Number(e.target.value) || 1)))} className={inputCls} />
          </label>
          <label className="flex flex-col gap-1 text-[9px] uppercase tracking-widest font-bold text-muted-foreground">
            Monthly $
            <input type="number" min={0} step={50} value={monthly} onChange={e => setMonthly(Math.max(0, Number(e.target.value) || 0))} className={inputCls} />
          </label>
          <label className="flex flex-col gap-1 text-[9px] uppercase tracking-widest font-bold text-muted-foreground">
            Vol (from risk)
            <div className={cn(inputCls, 'opacity-60')}>{vol.toFixed(0)}%</div>
          </label>
        </div>
        <div ref={ref} className="min-h-[140px] flex-1" />
        <p className="mt-2 shrink-0 font-mono text-[10px] text-muted-foreground">
          {years}y range: <span className={DOWN}>{fmtK(result.end.lo)}</span> → <span className="text-foreground">{fmtK(result.end.mid)}</span> → <span className={UP}>{fmtK(result.end.hi)}</span>
        </p>
      </> : <EmptyNote>Add holdings to project outcomes.</EmptyNote>}
    </div>
  );
}

// ── AI insights (brief + change feed from the analysis engine) ───────────────

export function InsightsWidget({ data }: WidgetProps) {
  const hasContent = data.brief.length > 0 || data.changes.length > 0;
  return (
    <div className="flex h-full flex-col p-4">
      <WidgetHeader icon={Brain} title="AI Insights" hint="ENGINE BRIEF" />
      {!hasContent ? <EmptyNote>Insights appear once the fund holds positions.</EmptyNote> : (
        <div className="flex-1 overflow-y-auto scrollbar-hide flex flex-col gap-2">
          {data.brief.map((line, i) => (
            <p key={`b${i}`} className="text-xs text-muted-foreground leading-relaxed border-l-2 border-primary/30 pl-2.5">{line}</p>
          ))}
          {data.changes.map(c => (
            <p key={c.id} className={cn(
              'text-xs leading-relaxed rounded-xl border px-2.5 py-2',
              c.severity === 'positive' ? 'border-emerald-500/10 bg-emerald-500/5 text-emerald-400'
                : c.severity === 'negative' ? 'border-rose-500/10 bg-rose-500/5 text-rose-400'
                  : 'border-border/20 bg-accent/20 text-muted-foreground',
            )}>{c.text}</p>
          ))}
          {data.attribution.length > 0 && (
            <div className="mt-1">
              <p className="text-[9px] uppercase tracking-widest font-bold text-muted-foreground mb-1.5">Top P/L contributors</p>
              {data.attribution.slice(0, 3).map(a => (
                <div key={a.symbol} className="flex items-center justify-between font-mono text-[11px] py-0.5">
                  <span className="font-bold text-foreground">{a.symbol}</span>
                  <span className={toneClass(a.dollar)}>{fmtUsd(a.dollar)} · {a.pts.toFixed(1)} pts</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── Rebalancing suggestions (drift vs equal-weight target) ───────────────────

export function RebalanceWidget({ data }: WidgetProps) {
  const holdings = data.stats.holdings;
  if (holdings.length === 0) {
    return (
      <div className="flex h-full flex-col p-4">
        <WidgetHeader icon={Layers} title="Rebalancing Suggestions" />
        <EmptyNote>Add holdings to compute allocation drift.</EmptyNote>
      </div>
    );
  }
  const target = 100 / holdings.length;
  const rows = holdings
    .map(h => ({ symbol: h.symbol, cur: h.allocation, drift: h.allocation - target, value: h.valueUsd }))
    .sort((a, b) => Math.abs(b.drift) - Math.abs(a.drift));

  return (
    <div className="flex h-full flex-col p-4">
      <WidgetHeader icon={Layers} title="Rebalancing Suggestions" hint="DRIFT vs EQUAL WEIGHT" />
      <div className="grid flex-1 auto-rows-fr grid-cols-1 gap-2 md:grid-cols-2 xl:grid-cols-3">
        {rows.map(r => {
          const dollar = (Math.abs(r.drift) / 100) * data.stats.totalValue;
          const quiet = Math.abs(r.drift) < 1.5;
          return (
            <div key={r.symbol} className="flex min-h-0 flex-col justify-center rounded-xl bg-neutral-500/10 bg-white/[0.05] px-3 py-2.5 min-w-0">
              <div className="flex items-baseline justify-between gap-2 mb-1.5">
                <span className="font-mono text-xs font-bold text-foreground">{r.symbol}</span>
                <span className={cn('text-[10px] font-bold', quiet ? 'text-muted-foreground' : r.drift > 0 ? 'text-rose-400' : 'text-emerald-400')}>
                  {quiet ? 'Hold' : r.drift > 0 ? `Trim ${fmtK(dollar)}` : `Add ${fmtK(dollar)}`}
                </span>
              </div>
              <div className="flex gap-3 flex-wrap font-mono text-[10px] text-muted-foreground">
                <span>Now {r.cur.toFixed(1)}%</span>
                <span>Target {target.toFixed(1)}%</span>
                <span className={cn('font-bold', quiet ? 'text-muted-foreground' : r.drift > 0 ? 'text-rose-400' : 'text-emerald-400')}>
                  {r.drift >= 0 ? '+' : ''}{r.drift.toFixed(1)} pts
                </span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── Portfolio heatmap (treemap: area = value, colour = day move) ─────────────

/**
 * Finviz-style treemap of the fund's own holdings. Area is the position's USD
 * value (so the map is the portfolio's shape, not the index's), colour is the
 * day move on the shared −3…+3% gradient the market heatmap uses. A single
 * holding fills the tile; the tile label degrades from symbol+%+value to just
 * the symbol as the tile shrinks, so nothing is ever drawn illegibly.
 */
export function PortfolioHeatmapWidget({ data }: WidgetProps) {
  const holdings = data.stats.holdings;
  const [expanded, setExpanded] = useState(false);

  const tree = useMemo(
    () => holdings
      .filter(h => h.valueUsd > 0)
      .map(h => ({
        name: h.symbol,
        value: [h.valueUsd, h.dayPct],
        symbol: h.symbol,
        fullName: h.name,
        currency: h.currency,
        price: h.currentPrice,
        changePct: h.dayPct,
        valueUsd: h.valueUsd,
        allocation: h.allocation,
      })),
    [holdings],
  );

  const weightedDay = useMemo(() => {
    const total = holdings.reduce((a, h) => a + h.valueUsd, 0);
    return total > 0 ? holdings.reduce((a, h) => a + (h.valueUsd / total) * h.dayPct, 0) : 0;
  }, [holdings]);

  const ref = useEChart(() => {
    if (!tree.length) return null;
    return {
      ...baseChartOption(),
      tooltip: {
        trigger: 'item',
        confine: true,
        backgroundColor: 'rgba(18,18,20,0.5)', appendTo: 'body',
        borderColor: 'rgba(255,255,255,0.12)', borderWidth: 1, padding: [10, 12],
        textStyle: { color: '#e5e7eb', fontSize: 11 },
        extraCssText: 'backdrop-filter: blur(18px) saturate(1.35); border-radius: 14px; box-shadow: 0 8px 30px rgba(0,0,0,0.45), inset 0 1px 0 rgba(255,255,255,0.06);',
        formatter: (params: unknown) => {
          const p = params as { data?: Record<string, unknown> };
          const d = p.data ?? {};
          const chg = Number(d.changePct ?? 0);
          const color = chg >= 0 ? '#34d399' : '#fb7185';
          const row = (label: string, value: string) =>
            `<tr><td style="color:#9ca3af;padding:1px 12px 1px 0">${label}</td><td style="text-align:right;color:#e5e7eb">${value}</td></tr>`;
          return `
            <div style="min-width:172px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace">
              <div style="display:flex;align-items:baseline;justify-content:space-between;gap:14px">
                <span style="font-weight:800;font-size:13px;color:#f8fafc">${String(d.symbol ?? '')}</span>
                <span style="font-weight:700;font-size:12px;color:${color}">${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%</span>
              </div>
              <div style="color:#9ca3af;font-size:10px;margin-top:1px;max-width:210px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${String(d.fullName ?? '')}</div>
              <div style="height:1px;background:rgba(255,255,255,.08);margin:7px 0"></div>
              <table style="font-size:10px;border-collapse:collapse;width:100%">
                ${row('Value', fmtK(Number(d.valueUsd ?? 0)))}
                ${row('Weight', `${Number(d.allocation ?? 0).toFixed(1)}%`)}
                ${row('Price', `${String(d.currency ?? '')} ${Number(d.price ?? 0).toFixed(2)}`)}
              </table>
            </div>`;
        },
      },
      visualMap: {
        type: 'continuous', min: -3, max: 3, calculable: false,
        orient: 'horizontal', left: 'center', bottom: 2,
        itemWidth: 12, itemHeight: 90, precision: 0,
        text: ['+3%', '-3%'], textStyle: { color: '#94a3b8', fontSize: 9 },
        inRange: { color: ['#b91c1c', '#7f1d1d', '#27272a', '#166534', '#22c55e'] },
      },
      series: [{
        type: 'treemap',
        roam: expanded,
        nodeClick: false,
        breadcrumb: { show: false },
        width: '100%', height: '100%',
        top: 2, left: 0, right: 0, bottom: 22,
        visualDimension: 1, visualMin: -3, visualMax: 3,
        squareRatio: 1.15,
        itemStyle: { borderWidth: 2, gapWidth: 3, borderColor: 'rgba(8,8,10,0.92)' },
        label: {
          show: true, position: 'insideTopLeft', color: '#f8fafc', fontSize: 10, lineHeight: 13,
          formatter: (params: unknown) => {
            const p = params as { data?: Record<string, unknown> };
            const d = p.data ?? {};
            if (d.symbol == null) return '';
            const chg = Number(d.changePct ?? 0);
            return `{sym|${String(d.symbol)}}\n{pct|${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%}`;
          },
          rich: {
            sym: { fontSize: 11, fontWeight: 'bold', color: '#f8fafc' },
            pct: { fontSize: 9, color: '#dbeafe' },
          },
        },
        data: tree,
      }],
    };
  }, [tree, expanded]);

  if (!holdings.length) {
    return (
      <div className="flex h-full flex-col p-4">
        <WidgetHeader icon={Grid3x3} title="Portfolio Heatmap" />
        <EmptyNote>Add holdings to map the portfolio.</EmptyNote>
      </div>
    );
  }

  const body = (
    <div className="flex h-full flex-col p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2 border-b border-border/10 pb-2.5">
        <div className="flex items-center gap-2 min-w-0">
          <Grid3x3 size={14} className="text-primary shrink-0" />
          <h3 className="font-serif text-sm font-semibold text-foreground truncate">Portfolio Heatmap</h3>
          <span className={cn('font-mono text-[10px] font-bold', toneClass(weightedDay))}>
            {weightedDay >= 0 ? '+' : ''}{weightedDay.toFixed(2)}%
          </span>
        </div>
        <div className="flex items-center gap-1.5">
          <span className="font-mono text-[9px] text-muted-foreground/70">
            area = value · colour = day
          </span>
          <button
            type="button"
            onClick={() => setExpanded(v => !v)}
            className="rounded-xl border border-white/15 bg-accent/30 p-1.5 text-muted-foreground transition-all hover:border-primary/40 hover:text-primary cursor-pointer"
            title={expanded ? 'Exit fullscreen' : 'Expand heatmap'}
          >
            {expanded ? <Minimize2 size={13} /> : <Maximize2 size={13} />}
          </button>
        </div>
      </div>
      <div ref={ref} className={expanded ? 'h-full w-full flex-1' : 'min-h-[220px] flex-1'} />
    </div>
  );

  if (expanded && typeof document !== 'undefined') {
    return createPortal(
      <div className="fixed inset-0 z-[100] animate-fade-in">
        <div className="absolute inset-0 bg-black/70 backdrop-blur-sm" onClick={() => setExpanded(false)} />
        <div className="absolute inset-2 sm:inset-5 flex flex-col overflow-hidden rounded-2xl border border-white/10 glass-card-strong shadow-2xl shadow-black/50">
          {body}
        </div>
      </div>,
      document.body,
    );
  }
  return body;
}

// ── Correlation matrix ───────────────────────────────────────────────────────

/**
 * Pairwise correlation of holding returns. Every cell is computed on the dates
 * THAT PAIR shares, so an NYSE stock and a PSX stock are compared only on days
 * both traded — a positional (index-against-index) matrix would compare
 * different calendar days and is the classic way this chart lies.
 *
 * Cells with too little overlap render as an explicit "·" rather than a
 * plausible-looking 0.00.
 */
export function CorrelationMatrixWidget({ data }: WidgetProps) {
  const corr = data.correlation;

  const option = useMemo(() => {
    if (!corr) return null;
    const n = corr.symbols.length;
    // ECharts heatmap data: [xIndex, yIndex, value].
    const cells: Array<[number, number, number]> = [];
    for (let y = 0; y < n; y++) {
      for (let x = 0; x < n; x++) cells.push([x, y, corr.matrix[y][x]]);
    }
    const heatOption: EChartsOption = {
      ...baseChartOption(),
      tooltip: {
        trigger: 'item',
        confine: true,
        backgroundColor: 'rgba(18,18,20,0.5)', appendTo: 'body',
        borderColor: 'rgba(255,255,255,0.12)', borderWidth: 1, padding: [10, 12],
        textStyle: { color: '#e5e7eb', fontSize: 11 },
        extraCssText: 'backdrop-filter: blur(18px) saturate(1.35); border-radius: 14px; box-shadow: 0 8px 30px rgba(0,0,0,0.45), inset 0 1px 0 rgba(255,255,255,0.06);',
        formatter: (params: unknown) => {
          const p = params as { data?: [number, number, number] };
          const [x, y, v] = p.data ?? [0, 0, 0];
          const obs = corr.observations[y]?.[x] ?? 0;
          const enough = obs >= MIN_OBSERVATIONS || y === x;
          const a = corr.symbols[x]; const b = corr.symbols[y];
          return `
            <div style="font-family:ui-monospace,SFMono-Regular,Menlo,monospace;min-width:176px">
              <div style="font-weight:800;font-size:12px;color:#f8fafc">${a} · ${b}</div>
              <div style="height:1px;background:rgba(255,255,255,.08);margin:6px 0"></div>
              <div style="font-size:11px;color:#e5e7eb">ρ ${y === x ? '1.000' : v.toFixed(3)}</div>
              <div style="font-size:10px;color:#9ca3af;margin-top:2px">${obs} shared observations</div>
              ${enough ? '' : `<div style="font-size:10px;color:#fbbf24;margin-top:3px">Not enough overlap for a reliable estimate</div>`}
            </div>`;
        },
      },
      grid: { left: 6, right: 6, top: 8, bottom: 46, containLabel: true },
      xAxis: {
        type: 'category', data: corr.symbols, splitArea: { show: false },
        axisLabel: { color: '#94a3b8', fontSize: 9, rotate: 45, fontFamily: 'monospace' },
        axisLine: { lineStyle: { color: 'rgba(148,163,184,0.15)' } },
        axisTick: { show: false },
      },
      yAxis: {
        type: 'category', data: corr.symbols, splitArea: { show: false },
        axisLabel: { color: '#94a3b8', fontSize: 9, fontFamily: 'monospace' },
        axisLine: { lineStyle: { color: 'rgba(148,163,184,0.15)' } },
        axisTick: { show: false },
      },
      visualMap: {
        type: 'continuous', min: -1, max: 1, calculable: false,
        orient: 'horizontal', left: 'center', bottom: 0,
        itemWidth: 12, itemHeight: 90, precision: 1,
        text: ['+1', '-1'], textStyle: { color: '#94a3b8', fontSize: 9 },
        inRange: { color: ['#b91c1c', '#7f1d1d', '#27272a', '#166534', '#22c55e'] },
      },
      series: [{
        type: 'heatmap',
        data: cells,
        label: {
          show: true, fontSize: 8, fontFamily: 'monospace', color: '#e5e7eb',
          formatter: (p: unknown) => {
            const pp = p as { data?: [number, number, number] };
            const [x, y, v] = pp.data ?? [0, 0, 0];
            const obs = corr.observations[y]?.[x] ?? 0;
            // Hide the number when the pair has too little overlap — a "0.00"
            // there would be inventing a relationship the data can't support.
            if (y !== x && obs < MIN_OBSERVATIONS) return '·';
            return v.toFixed(2);
          },
        },
        itemStyle: { borderColor: 'rgba(8,8,10,0.85)', borderWidth: 1 },
        emphasis: { itemStyle: { borderColor: '#34d399', borderWidth: 1.5 } },
      }],
    };
    return heatOption;
  }, [corr]);

  const ref = useEChart(() => option, [option]);

  if (!corr) {
    return (
      <div className="flex h-full flex-col p-4">
        <WidgetHeader icon={Grid3x3} title="Correlation Matrix" />
        <EmptyNote>
          {data.stats.holdings.length < 2
            ? 'Two or more holdings are needed to correlate returns.'
            : 'Not enough overlapping price history yet.'}
        </EmptyNote>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col p-4">
      <WidgetHeader
        icon={Grid3x3}
        title="Correlation Matrix"
        hint={`${corr.symbols.length} × ${corr.symbols.length} · ${corr.minObservations}–${corr.maxObservations} obs`}
      />
      <div ref={ref} className="min-h-[260px] flex-1" />
      <p className="mt-2 shrink-0 font-mono text-[9px] leading-relaxed text-muted-foreground/60">
        Pearson ρ of daily returns · each pair uses only the days BOTH traded (native currency — returns are currency-neutral)
      </p>
    </div>
  );
}

// ── Risk matrix ──────────────────────────────────────────────────────────────

/**
 * Per-holding risk and each position's share of TOTAL portfolio risk, using the
 * standard Euler decomposition over the same correlation estimate the matrix
 * widget shows. This is the number that matters for sizing: a small position in
 * a very volatile, very correlated asset can contribute more portfolio risk
 * than a large position in a calm one.
 */
export function RiskMatrixWidget({ data }: WidgetProps) {
  const rm = data.riskMatrix;
  const holdings = data.stats.holdings;

  if (!rm || !holdings.length) {
    return (
      <div className="flex h-full flex-col p-4">
        <WidgetHeader icon={Activity} title="Risk Matrix" />
        <EmptyNote>
          {holdings.length < 2
            ? 'Two or more holdings are needed to decompose portfolio risk.'
            : 'Not enough overlapping price history yet.'}
        </EmptyNote>
      </div>
    );
  }

  const bySymbol = new Map(holdings.map(h => [h.symbol, h]));
  const rows = rm.vol
    .map((v, i) => ({
      symbol: rm.weights[i] !== undefined ? (data.correlation?.symbols[i] ?? '') : '',
      weight: rm.weights[i] * 100,
      vol: v,
      beta: rm.betaToPortfolio[i],
      contrib: rm.riskContributionPct[i],
      valueUsd: bySymbol.get(data.correlation?.symbols[i] ?? '')?.valueUsd ?? 0,
      currency: bySymbol.get(data.correlation?.symbols[i] ?? '')?.currency ?? 'USD',
    }))
    .filter(r => r.symbol)
    // Rank by risk contribution — the whole point is to see where risk lives.
    .sort((a, b) => b.contrib - a.contrib);

  const maxContrib = Math.max(...rows.map(r => r.contrib), 1);
  const div = rm.diversificationPct;

  return (
    <div className="flex h-full flex-col p-4">
      <WidgetHeader
        icon={Activity}
        title="Risk Matrix"
        hint={`σ ${rm.portfolioVol != null ? rm.portfolioVol.toFixed(1) : '—'}% · ${rm.observations} obs`}
      />

      {/* Headline decomposition */}
      <div className="mb-3 grid grid-cols-2 gap-2.5 lg:grid-cols-4 shrink-0">
        <Tile
          label="Portfolio Vol"
          value={rm.portfolioVol != null ? `${rm.portfolioVol.toFixed(1)}%` : '—'}
          sub="annualized"
        />
        <Tile
          label="Diversification"
          value={div != null ? `${div.toFixed(1)}%` : '—'}
          tone={div == null ? undefined : div > 0 ? 'text-emerald-400' : 'text-rose-400'}
          sub={div != null && div > 0 ? 'risk removed vs avg holding' : 'no benefit detected'}
        />
        <Tile
          label="Top Risk"
          value={rows[0] ? rows[0].symbol : '—'}
          sub={rows[0] ? `${rows[0].contrib.toFixed(1)}% of total` : undefined}
        />
        <Tile
          label="Effective Bets"
          value={effectiveBets(rows.map(r => r.contrib))}
          sub="1 / Σ(share²)"
        />
      </div>

      {/* Per-holding table */}
      <div className="flex-1 overflow-auto custom-scrollbar rounded-xl glass-card-subtle">
        <table className="w-full text-left border-separate border-spacing-0 text-xs">
          <thead className="sticky top-0 z-10">
            <tr className="glass-card-subtle">
              {['Symbol', 'Weight', 'Vol', 'β to Port', 'Risk Share'].map((h, i) => (
                <th key={h} className={cn(
                  'border-b border-white/10 py-2.5 px-3 text-[9px] uppercase tracking-widest text-muted-foreground/80 font-mono font-semibold whitespace-nowrap',
                  i === 0 ? 'text-left' : 'text-right',
                )}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody className="font-mono">
            {rows.map(r => (
              <tr key={r.symbol} className="transition-colors hover:bg-white/[0.05]">
                <td className="border-b border-white/5 py-2.5 px-3 font-bold text-foreground whitespace-nowrap">{r.symbol}</td>
                <td className="border-b border-white/5 py-2.5 px-3 text-right text-muted-foreground tabular-nums whitespace-nowrap">{r.weight.toFixed(1)}%</td>
                <td className="border-b border-white/5 py-2.5 px-3 text-right text-muted-foreground tabular-nums whitespace-nowrap">{r.vol.toFixed(1)}%</td>
                <td className="border-b border-white/5 py-2.5 px-3 text-right text-muted-foreground tabular-nums whitespace-nowrap">{r.beta.toFixed(2)}</td>
                <td className="border-b border-white/5 py-2.5 px-3 text-right whitespace-nowrap">
                  <span className="flex items-center justify-end gap-2">
                    <span className="hidden h-1.5 w-16 overflow-hidden rounded-full bg-border/40 sm:block">
                      <span className="block h-full rounded-full bg-primary" style={{ width: `${(r.contrib / maxContrib) * 100}%` }} />
                    </span>
                    <span className="w-12 text-right font-bold text-foreground tabular-nums">{r.contrib.toFixed(1)}%</span>
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="mt-2 shrink-0 font-mono text-[9px] leading-relaxed text-muted-foreground/60">
        Risk share = wᵢ · (Σw)ᵢ / σₚ (Euler decomposition) · β is measured against this portfolio, not an index
      </p>
    </div>
  );
}

/**
 * Effective number of independent bets = 1 / Σ(share²). A portfolio whose risk
 * is spread evenly across N positions scores ≈ N; one dominated by a single
 * position scores ≈ 1. This is the honest reading of "how diversified am I".
 */
function effectiveBets(sharesPct: number[]): string {
  const total = sharesPct.reduce((a, s) => a + s, 0);
  if (!(total > 0)) return '—';
  const norm = sharesPct.map(s => s / total);
  const hhi = norm.reduce((a, s) => a + s * s, 0);
  return hhi > 0 ? (1 / hhi).toFixed(1) : '—';
}

// ── Registry ─────────────────────────────────────────────────────────────────

export const FUND_WIDGETS: WidgetDefinition[] = [
  { id: 'kpi-board', category: 'portfolio', label: 'Key Metrics', desc: 'Value, unrealized P/L, cash, holdings count.', icon: 'wallet', sizes: ['medium', 'large'] },
  { id: 'performance', category: 'portfolio', label: 'Performance', desc: 'Fund NAV vs SPY benchmark (indexed).', icon: 'chart-line', sizes: ['large', 'full'] },
  { id: 'allocation', category: 'portfolio', label: 'Allocation', desc: 'Donut + per-symbol weights.', icon: 'pie', sizes: ['medium', 'large'] },
  { id: 'portfolio-heatmap', category: 'portfolio', label: 'Portfolio Heatmap', desc: 'Treemap of holdings — area by position value, colour by day move.', icon: 'grid', sizes: ['large', 'full'] },
  { id: 'correlation-matrix', category: 'portfolio', label: 'Correlation Matrix', desc: 'Pairwise return correlation between holdings (date-aligned).', icon: 'grid', sizes: ['large', 'full'] },
  { id: 'risk-matrix', category: 'portfolio', label: 'Risk Matrix', desc: 'Per-holding volatility, beta and contribution to portfolio risk.', icon: 'activity', sizes: ['large', 'full'] },
  { id: 'holdings', category: 'portfolio', label: 'Holdings', desc: 'Open positions with cost basis and P/L.', icon: 'table', sizes: ['large', 'full'] },
  { id: 'tax-lots', category: 'portfolio', label: 'Tax Lots & Realized Gains', desc: 'Engine open lots, realized disposals and holding periods.', icon: 'layers', sizes: ['large', 'full'] },
  { id: 'rebalance', category: 'portfolio', label: 'Rebalancing', desc: 'Drift vs target weights with trim/add actions.', icon: 'layers', sizes: ['large', 'full'] },
  { id: 'ledger', category: 'portfolio', label: 'Transactions', desc: 'Full transaction ledger + cash.', icon: 'layers', sizes: ['large', 'full'] },
  { id: 'risk-metrics', category: 'portfolio', label: 'Risk Metrics', desc: 'Beta, volatility, Sharpe, drawdown.', icon: 'activity', sizes: ['medium', 'large'] },
  { id: 'health', category: 'portfolio', label: 'Portfolio Health', desc: 'Gauge + transparent sub-scores.', icon: 'heart', sizes: ['medium', 'large'] },
  { id: 'monte-carlo', category: 'portfolio', label: 'Monte Carlo', desc: 'Projected outcome bands with contributions.', icon: 'gauge', sizes: ['large', 'full'] },
  { id: 'ai-insights', category: 'ai', label: 'AI Insights', desc: 'Engine brief, change feed, attribution.', icon: 'brain', sizes: ['medium', 'large'] },
];

export const FUND_WIDGET_IDS = FUND_WIDGETS.map(w => w.id);

/** Resolve a fund widget's icon by name (lucide). */
export function fundWidgetIcon(name: string): React.ComponentType<{ size?: number; className?: string }> {
  const map: Record<string, React.ComponentType<{ size?: number; className?: string }>> = {
    wallet: Wallet, 'chart-line': ChartLine, pie: PieChart, table: Table2,
    layers: Layers, activity: Activity, heart: HeartPulse, gauge: Gauge, brain: Brain,
    grid: Grid3x3,
  };
  return map[name] ?? Wallet;
}

export function renderFundWidget(widgetId: string, props: WidgetProps): React.ReactNode {
  switch (widgetId) {
    case 'kpi-board': return <KpiBoardWidget {...props} />;
    case 'performance': return <PerformanceWidget {...props} />;
    case 'allocation': return <AllocationWidget {...props} />;
    case 'portfolio-heatmap': return <PortfolioHeatmapWidget {...props} />;
    case 'correlation-matrix': return <CorrelationMatrixWidget {...props} />;
    case 'risk-matrix': return <RiskMatrixWidget {...props} />;
    case 'holdings': return <HoldingsWidget {...props} />;
    case 'tax-lots': return <TaxLotsWidget {...props} />;
    case 'rebalance': return <RebalanceWidget {...props} />;
    case 'ledger': return <TransactionsWidget {...props} />;
    case 'risk-metrics': return <RiskWidget {...props} />;
    case 'health': return <HealthWidget {...props} />;
    case 'monte-carlo': return <MonteCarloWidget {...props} />;
    case 'ai-insights': return <InsightsWidget {...props} />;
    default: return <div className="p-4 text-xs text-muted-foreground">Unknown widget: {widgetId}</div>;
  }
}

/**
 * Intrinsic sizing per fund widget. Charts/heatmaps/tables that fill their card
 * get a FRAME (`defaultHeight`); compact stat boards GROW (`minHeight`).
 */
const FUND_WIDGET_FRAME: Record<string, number> = {
  'performance': 340,
  'allocation': 380,
  'portfolio-heatmap': 400,
  'correlation-matrix': 420,
  'risk-matrix': 400,
  'holdings': 380,
  'tax-lots': 400,
  'ledger': 380,
  'health': 360,
  'monte-carlo': 380,
  'ai-insights': 340,
};
const FUND_WIDGET_MIN_HEIGHT: Record<string, number> = {
  'kpi-board': 220,
  'rebalance': 320,
  'risk-metrics': 200,
};
const FUND_MIN_DEFAULT = 260;

function fundSizing(widgetId: string): { defaultHeight?: number; minHeight?: number } {
  const frame = FUND_WIDGET_FRAME[widgetId];
  if (frame != null) return { defaultHeight: frame };
  return { minHeight: FUND_WIDGET_MIN_HEIGHT[widgetId] ?? FUND_MIN_DEFAULT };
}

/**
 * Clean default arrangement on the track board: cards size to content with an
 * intrinsic floor, and the masonry packer balances each band so there is no
 * dead space. A one-track KPI strip, then balanced bands, then a full-width
 * rebalancing action row.
 */
export function defaultFundLayout(): WorkspaceLayout {
  const board = (widgets: string[], tracks = 3): WorkspaceSection => {
    const ids = widgets.filter(w => FUND_WIDGET_IDS.includes(w));
    const columnCount = Math.max(1, Math.min(4, tracks, ids.length || 1));
    const columns: WorkspaceColumn[] = Array.from({ length: columnCount }, () => ({
      id: `col-${Math.random().toString(36).slice(2, 8)}`,
      weight: 1,
      widgets: [] as WidgetPlacement[],
    }));
    ids.forEach((widgetId, i) => {
      columns[i % columns.length].widgets.push({
        id: `wid-${widgetId}-${Math.random().toString(36).slice(2, 6)}`,
        widgetId,
        height: null,
        ...fundSizing(widgetId),
      });
    });
    rebalanceColumns(columns);
    return { id: `sec-${Math.random().toString(36).slice(2, 6)}`, zone: 'main', columns };
  };

  const layout: WorkspaceLayout = {
    version: 2,
    sections: [
      board(['kpi-board'], 1),
      board(['performance', 'allocation']),
      board(['portfolio-heatmap', 'correlation-matrix'], 2),
      board(['risk-matrix', 'risk-metrics', 'health']),
      board(['holdings', 'ai-insights']),
      board(['tax-lots', 'ledger']),
      board(['monte-carlo'], 1),
      board(['rebalance'], 1),
    ],
  };
  const normalized = normalizeLayout(layout, new Set(FUND_WIDGET_IDS));
  return normalized ?? layout;
}

