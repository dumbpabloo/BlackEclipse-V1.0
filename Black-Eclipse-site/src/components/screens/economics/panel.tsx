'use client';

import React, { useState, useEffect, useMemo, useRef, useCallback } from 'react';
import { saveBlob } from '@/lib/desktop/download';
import * as echarts from 'echarts';
import { Download, Loader2, Search, KeyRound, Sparkles, X } from '@/lib/icons';
import type { EconSource } from '@/lib/economics/sources';
import { ECON_SOURCES } from '@/lib/economics/sources';
import { computeStats, sortByDate, type EconPoint } from '@/lib/economics/fetch';
import { useChartPalette } from '@/lib/chart-palette-store';
import { usePageShortcuts } from '@/hooks/use-page-shortcuts';

interface ResearchResult {
  reportId: string;
  answer: string;
  verdict?: string;
  confidence?: number;
  agents: { name: string; status: string; toolCalls: number; toolErrors: number }[];
}

export interface PanelProps {
  source: EconSource;
}

const PAGE_SIZE = 25;

export function EconPanel({ source }: PanelProps) {
  const chartPalette = useChartPalette();
  const [params, setParams] = useState<Record<string, string>>(() => {
    const init: Record<string, string> = {};
    for (const c of source.controls) init[c.name] = c.defaultValue ?? '';
    return init;
  });
  const [rows, setRows] = useState<EconPoint[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [keyRequired, setKeyRequired] = useState<string | null>(null);
  const [asOf, setAsOf] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [research, setResearch] = useState<ResearchResult | null>(null);
  const [researchLoading, setResearchLoading] = useState(false);
  const [range, setRange] = useState<'all' | '1Y' | '5Y' | '10Y'>('all');
  const [viewMode, setViewMode] = useState<'level' | 'yoy' | 'change'>('level');
  const [logScale, setLogScale] = useState(false);
  const [rolling, setRolling] = useState<0 | 3 | 6 | 12>(0);
  const [recession, setRecession] = useState(false);
  const [compareId, setCompareId] = useState('');
  const [compareRows, setCompareRows] = useState<EconPoint[] | null>(null);
  const [threshold, setThreshold] = useState('');
  const [thresholdLevel, setThresholdLevel] = useState('');
  const [narrative, setNarrative] = useState<string | null>(null);
  const [narrativeLoading, setNarrativeLoading] = useState(false);
  const chartRef = useRef<HTMLDivElement>(null);
  const chartInstance = useRef<echarts.ECharts | null>(null);

  // Reset params when the source changes.
  useEffect(() => {
    const init: Record<string, string> = {};
    for (const c of source.controls) init[c.name] = c.defaultValue ?? '';
    setParams(init);
    setRows(null);
    setError(null);
    setKeyRequired(null);
    setResearch(null);
  }, [source]);

  const fetchSeries = useCallback(async (override?: Record<string, string>) => {
    const p = override ?? params;
    setLoading(true);
    setError(null);
    setKeyRequired(null);
    setResearch(null);
    try {
      const qs = new URLSearchParams({ source: source.id, ...Object.fromEntries(Object.entries(p).filter(([, v]) => v)) });
      const res = await fetch(`/api/economics?${qs.toString()}`);
      const data = await res.json();
      if (data.keyRequired) {
        setKeyRequired(data.keyEnv || 'UNKNOWN');
        setRows(null);
      } else if (data.error) {
        setError(data.error);
        setRows(null);
      } else {
        setRows(sortByDate(data.rows || []));
        setAsOf(data.asOf || null);
        setPage(1);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Fetch failed');
      setRows(null);
    } finally {
      setLoading(false);
    }
  }, [source, params]);

  // Fetch a second series to overlay for comparison.
  const fetchCompare = async (id: string) => {
    if (!id) { setCompareRows(null); return; }
    try {
      const qs = new URLSearchParams({ source: id });
      const res = await fetch(`/api/economics?${qs.toString()}`);
      const data = await res.json();
      setCompareRows(data.rows ? sortByDate(data.rows) : null);
    } catch {
      setCompareRows(null);
    }
  };

  useEffect(() => {
    void fetchSeries();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [source]);

  // ─── ECharts render ────────────────────────────────────────────────────
  const chartDeps = useMemo(() => {
    if (!rows) return null;
    const finite = rows.filter((r) => r.value !== null);
    const labelKey = detectLabelKey(finite);
    const grouped = new Map<string, EconPoint[]>();
    if (labelKey) {
      for (const r of finite) {
        const label = String(r[labelKey] ?? 'value');
        const arr = grouped.get(label) ?? [];
        arr.push(r);
        grouped.set(label, arr);
      }
    } else {
      grouped.set(source.label, finite);
    }

    // Apply view transform (level / YoY / period change) per series.
    const transformed = new Map<string, EconPoint[]>();
    for (const [label, pts] of grouped) {
      const sorted = sortByDate(pts);
      if (viewMode === 'yoy') {
        transformed.set(label, yoySeries(sorted));
      } else if (viewMode === 'change') {
        transformed.set(label, pctChangeSeries(sorted));
      } else {
        transformed.set(label, sorted);
      }
    }

    // Apply date-range window.
    const windowed = new Map<string, EconPoint[]>();
    let cutoff = -Infinity;
    if (range !== 'all') {
      const years = range === '1Y' ? 1 : range === '5Y' ? 5 : 10;
      const now = Date.now();
      for (const label of transformed.keys()) {
        for (const r of transformed.get(label) ?? []) {
          const ts = Date.parse(r.date);
          if (!Number.isNaN(ts)) { cutoff = now - years * 365 * 24 * 3600 * 1000; break; }
        }
        if (cutoff !== -Infinity) break;
      }
    }
    for (const [label, pts] of transformed) {
      windowed.set(label, cutoff === -Infinity ? pts : pts.filter((r) => !Number.isNaN(Date.parse(r.date)) && Date.parse(r.date) >= cutoff));
    }

    const dates = [...new Set([...windowed.values()].flatMap((g) => g.map((r) => r.date)))].sort();
    const seriesNames = [...windowed.keys()].slice(0, 6);

    // Rolling average overlay (computed on the level series, before transforms).
    let rollingSeries: { name: string; data: [string, number | null][] }[] = [];
    if (rolling > 0) {
      rollingSeries = [...windowed.keys()].slice(0, 1).map((label) => {
        const pts = sortByDate(windowed.get(label) ?? []);
        return { name: `${label} (${rolling}-period avg)`, data: rollingAvg(pts, rolling).map((r) => [r.date, r.value] as [string, number | null]) };
      });
    }

    // Recession shading from the NBER USREC series (0/1).
    let recessionBands: { start: string; end: string }[] = [];
    if (recession) {
      recessionBands = buildRecessionBands(dates);
    }

    // Comparison overlay series (second source, level only).
    let compareSeries: { name: string; data: [string, number | null][] } | null = null;
    if (compareRows && compareRows.length > 0) {
      const pts = sortByDate(compareRows.filter((r) => r.value !== null));
      compareSeries = { name: compareId, data: pts.map((r) => [r.date, r.value] as [string, number | null]) };
    }

    return {
      seriesNames,
      dates,
      grouped: windowed,
      labelKey,
      rollingSeries,
      recessionBands,
      compareSeries,
    };
  }, [rows, source.label, range, viewMode, rolling, recession, compareRows, compareId]);

  useEffect(() => {
    if (!chartRef.current || !chartDeps) return;
    if (!chartInstance.current) {
      chartInstance.current = echarts.init(chartRef.current);
    }
    const { seriesNames, dates, grouped, rollingSeries, recessionBands, compareSeries } = chartDeps;
    const palette = seriesNames.length === 1 ? [chartPalette.lines.primary] : chartPalette.lines.series;
    const option: echarts.EChartsOption = {
      backgroundColor: 'transparent',
      grid: { left: 8, right: 8, top: 16, bottom: 8, containLabel: true },
      tooltip: {
        trigger: 'axis',
        backgroundColor: 'rgba(18,18,20,0.5)', appendTo: 'body',
        borderColor: 'rgba(255,255,255,0.12)',
        borderWidth: 1,
        padding: [10, 12],
        textStyle: { color: '#e5e5e5', fontSize: 11, fontFamily: 'monospace' },
        extraCssText:
          'backdrop-filter: blur(18px) saturate(1.35); border-radius: 14px; box-shadow: 0 8px 30px rgba(0,0,0,0.45), inset 0 1px 0 rgba(255,255,255,0.06);',
      },
      legend: (seriesNames.length > 1 || rollingSeries.length > 0)
        ? { top: 0, textStyle: { color: '#a3a3a3', fontSize: 10 }, icon: 'roundRect', itemWidth: 12, itemHeight: 3 }
        : undefined,
      xAxis: {
        type: 'category',
        data: dates,
        axisLabel: { color: '#737373', fontSize: 10, fontFamily: 'monospace' },
        axisLine: { lineStyle: { color: 'rgba(255,255,255,0.05)' } },
      },
      yAxis: {
        type: logScale ? 'log' : 'value',
        scale: true,
        axisLabel: { color: '#737373', fontSize: 10, fontFamily: 'monospace' },
        splitLine: { lineStyle: { color: 'rgba(255,255,255,0.03)' } },
      },
      series: [
        ...(recessionBands.length > 0
          ? [{
              name: 'Recession',
              type: 'line' as const,
              data: dates.map((d) => {
                const band = recessionBands.find((b) => d >= b.start && d <= b.end);
                return band ? 1 : null;
              }),
              lineStyle: { width: 0 },
              symbol: 'none',
              areaStyle: { color: 'rgba(255,62,62,0.12)' },
              tooltip: { show: false },
              z: 0,
            }]
          : []),
        ...seriesNames.map((name, i) => ({
          name,
          type: 'line' as const,
          showSymbol: false,
          data: (grouped.get(name) ?? []).map((r) => [r.date, r.value]),
          lineStyle: { width: 2, color: palette[i % palette.length] },
          itemStyle: { color: palette[i % palette.length] },
          areaStyle: seriesNames.length === 1 && rollingSeries.length === 0
            ? { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [
                { offset: 0, color: `${palette[i % palette.length]}30` },
                { offset: 1, color: `${palette[i % palette.length]}00` },
              ] } }
            : undefined,
        })),
        ...rollingSeries.map((rs) => ({
          name: rs.name,
          type: 'line' as const,
          showSymbol: false,
          data: rs.data,
          lineStyle: { width: 1.5, color: '#fbbf24', type: 'dashed' as const },
          itemStyle: { color: '#fbbf24' },
        })),
        ...(compareSeries
          ? [{
              name: compareSeries.name,
              type: 'line' as const,
              showSymbol: false,
              data: compareSeries.data,
              lineStyle: { width: 2, color: chartPalette.lines.series[1] ?? chartPalette.lines.primary, type: 'dashed' as const },
              itemStyle: { color: chartPalette.lines.series[1] ?? chartPalette.lines.primary },
            }]
          : []),
      ] as echarts.SeriesOption[],
    };
    chartInstance.current.setOption(option, true);
  }, [chartDeps, logScale, chartPalette]);

  useEffect(() => {
    const el = chartRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => chartInstance.current?.resize());
    ro.observe(el);
    return () => {
      ro.disconnect();
      chartInstance.current?.dispose();
      chartInstance.current = null;
    };
  }, []);

  // ─── Stats ─────────────────────────────────────────────────────────────
  const stats = useMemo(() => (rows ? computeStats(rows) : null), [rows]);
  const isCrossSection = Boolean(stats && !stats.isTimeSeries);

  // ─── Table pagination ──────────────────────────────────────────────────
  const displayRows = useMemo(() => {
    if (!rows) return [];
    return sortByDate(rows).reverse(); // newest first in the table
  }, [rows]);
  const totalPages = Math.max(1, Math.ceil(displayRows.length / PAGE_SIZE));
  const pageRows = displayRows.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  const valueKeys = useMemo(() => {
    if (!rows || rows.length === 0) return [];
    const sample = rows[0];
    return Object.keys(sample).filter((k) => !['date', 'value'].includes(k)).slice(0, 4);
  }, [rows]);

  // ─── CSV export with provenance header ─────────────────────────────────
  const exportCSV = () => {
    if (!rows || rows.length === 0) return;
    const ordered = sortByDate(rows);
    const cols = ['date', 'value', ...valueKeys];
    const provenance = `# source=${source.id} exported=${new Date().toISOString()} rows=${ordered.length}`;
    const lines = [provenance, cols.join(','), ...ordered.map((r) =>
      cols.map((c) => {
        const v = r[c];
        return typeof v === 'string' && v.includes(',') ? `"${v}"` : String(v ?? '');
      }).join(',')
    )];
    const blob = new Blob([lines.join('\n')], { type: 'text/csv' });
    saveBlob(`${source.id}_${new Date().toISOString().slice(0, 10)}.csv`, blob);
  };

  // ─── PNG export ─────────────────────────────────────────────────────────
  const exportPNG = () => {
    if (!chartInstance.current) return;
    const url = chartInstance.current.getDataURL({ type: 'png', pixelRatio: 2, backgroundColor: '#0f0f0f' });
    const a = document.createElement('a');
    a.href = url;
    a.download = `${source.id}_chart.png`;
    a.click();
  };

  // ─── AI narrative: plain-language trend summary ────────────────────────
  const runNarrative = async () => {
    if (!rows || rows.length === 0) return;
    setNarrativeLoading(true);
    setNarrative(null);
    try {
      const finite = rows.filter((r) => r.value !== null);
      const sorted = sortByDate(finite);
      const latest = sorted[sorted.length - 1];
      const prev = sorted.length >= 2 ? sorted[sorted.length - 2] : null;
      const change = prev && prev.value !== null && latest.value !== null && prev.value !== 0
        ? ((latest.value - prev.value) / Math.abs(prev.value)) * 100
        : null;
      const res = await fetch('/api/agent/research', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          query: `Give a 2-3 sentence plain-language summary of this macro series for a trader: "${source.label}". Latest value ${latest.value} as of ${latest.date}${change !== null ? `, ${change >= 0 ? 'up' : 'down'} ${Math.abs(change).toFixed(1)}% vs previous` : ''}. What does the trend signal?`,
          market: 'US',
        }),
      });
      const d = await res.json();
      if (d.success) setNarrative(d.answer);
      else setNarrative('Could not generate narrative.');
    } catch {
      setNarrative('Could not reach the research engine.');
    } finally {
      setNarrativeLoading(false);
    }
  };

  // ─── Real agent research ───────────────────────────────────────────────
  const runAnalysis = async () => {
    const label = params.indicator
      ? Object.values(params).filter(Boolean).join(' — ')
      : Object.values(params).filter(Boolean).join(', ');
    setResearchLoading(true);
    setResearch(null);
    try {
      const res = await fetch('/api/agent/research', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          query: `Analyze this macro-economic data for decision-making: "${source.label}" (${label || 'full dataset'})${
            stats?.latest !== null && stats?.latest !== undefined
              ? `. Latest value ${stats.latest}${stats.asOf ? ` as of ${stats.asOf}` : ''}`
              : ''
          }. What does this series say about the macro outlook, and what should investors watch next?`,
          market: 'US',
        }),
      });
      const data = await res.json();
      if (data.success) {
        setResearch({ reportId: data.reportId, answer: data.answer, verdict: data.verdict, confidence: data.confidence, agents: data.agents || [] });
      } else {
        setError(data.error || 'Research failed');
      }
    } catch {
      setError('Failed to reach the research engine');
    } finally {
      setResearchLoading(false);
    }
  };

  const setParam = (name: string, value: string) => {
    const next = { ...params, [name]: value };
    setParams(next);
    void fetchSeries(next);
  };

  // ─── Render states ─────────────────────────────────────────────────────
  const chartTitle = params.indicator
    ? Object.values(params).filter(Boolean).join(' · ')
    : source.label;

  const rootRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const filterRef = useRef<HTMLInputElement | HTMLSelectElement>(null);
  const searchControl = source.controls.find((control) => !['select', 'date', 'range'].includes(control.type));
  usePageShortcuts(rootRef, [
    { id: `economics.${source.id}.refresh`, label: `Fetch ${source.label}`, keys: 'R', run: () => void fetchSeries(), enabled: !loading },
    { id: `economics.${source.id}.search`, label: `Search ${source.label}`, keys: 'S', run: () => searchRef.current?.focus(), enabled: !!searchControl },
    { id: `economics.${source.id}.filters`, label: `Focus ${source.label} parameters`, keys: 'Shift+F', run: () => filterRef.current?.focus(), enabled: source.controls.length > 0 },
  ]);

  return (
    <div ref={rootRef} data-keyboard-scope={`Economics: ${source.label}`} className="flex flex-col gap-5 animate-fade-in-up">
      {/* Source header */}
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div>
          <h3 className="text-[15px] font-semibold text-foreground">{source.label}</h3>
          {source.description && (
            <p className="text-[11px] text-muted-foreground mt-0.5">{source.description}</p>
          )}
        </div>
        {source.keyRequired && (
          <span className="font-mono text-[10px] uppercase tracking-wider text-amber-400 bg-amber-400/10 border border-amber-400/30 rounded-full px-2 py-0.5">
            API key required
          </span>
        )}
      </div>

      {/* Toolbar — declarative controls + FETCH/CSV */}
      <div className="rounded-xl glass-card border border-border/10 p-4">
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {source.controls.map((control) => (
            <label key={control.name} className="flex flex-col gap-1.5">
              <span className="font-mono text-[10px] uppercase tracking-[0.08em] text-muted-foreground">{control.label}</span>
              {control.type === 'select' ? (
                <select
                  ref={control === source.controls[0] ? (node) => { filterRef.current = node; } : undefined}
                  value={params[control.name] ?? ''}
                  onChange={(e) => setParam(control.name, e.target.value)}
                  className="h-9 appearance-none rounded-md bg-background border border-border/30 pl-3 pr-8 text-[13px] font-mono text-foreground cursor-pointer focus:outline-none focus:border-primary/60 transition-colors"
                >
                  {(control.options ?? []).map((o) => (
                    <option key={o.value} value={o.value} className="bg-background text-foreground">{o.label}</option>
                  ))}
                </select>
              ) : (
                <input
                  ref={(node) => { if (control === searchControl) searchRef.current = node; if (control === source.controls[0]) filterRef.current = node; }}
                  type={control.type === 'date' ? 'date' : control.type === 'range' ? 'number' : 'text'}
                  value={params[control.name] ?? ''}
                  onChange={(e) => setParams((prev) => ({ ...prev, [control.name]: e.target.value }))}
                  className="h-9 rounded-md bg-background border border-border/30 px-3 text-[13px] font-mono text-foreground focus:outline-none focus:border-primary/60 transition-colors"
                />
              )}
            </label>
          ))}
        </div>
        <div className="flex items-center gap-2 mt-3">
          <button
            onClick={() => void fetchSeries()}
            disabled={loading}
            className="h-9 px-4 rounded-lg bg-primary/10 hover:bg-primary/20 text-primary border border-primary/20 text-[12px] font-semibold inline-flex items-center gap-1.5 cursor-pointer transition-colors disabled:opacity-50"
          >
            {loading ? <Loader2 size={12} className="animate-spin" /> : <Search size={12} />} Fetch
          </button>
          <button
            onClick={exportCSV}
            disabled={!rows || rows.length === 0}
            className="h-9 px-3 rounded-lg bg-accent/40 hover:bg-accent/70 border border-border/40 text-muted-foreground hover:text-foreground text-[12px] font-semibold inline-flex items-center gap-1.5 cursor-pointer transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <Download size={12} /> CSV
          </button>
        </div>
      </div>

      {/* Key-gated setup prompt */}
      {keyRequired && (
        <div className="p-4 rounded-xl bg-amber-400/5 border border-amber-400/30 flex items-start gap-3">
          <KeyRound size={16} className="text-amber-400 shrink-0 mt-0.5" />
          <div>
            <p className="text-[13px] font-semibold text-amber-400">API key required</p>
            <p className="text-[12px] text-muted-foreground mt-1">
              Set <span className="font-mono text-amber-400">{keyRequired}</span> in Settings / .env to enable this source.
            </p>
          </div>
        </div>
      )}

      {error && !keyRequired && (
        <div className="p-4 rounded-xl bg-rose-500/5 border border-rose-500/30">
          <p className="text-[12px] font-semibold text-rose-400 font-mono">{error}</p>
          <button onClick={() => void fetchSeries()} className="mt-2 px-3 py-1.5 rounded-lg bg-rose-500/10 hover:bg-rose-500/20 text-rose-400 border border-rose-500/25 text-[11px] font-semibold cursor-pointer transition-colors">
            Retry
          </button>
        </div>
      )}

      {/* Loading skeleton */}
      {loading && !rows && (
        <div className="flex flex-col gap-2 py-4">
          {[1, 2, 3, 4].map((i) => <div key={i} className="h-8 rounded-lg bg-accent/20 animate-pulse" />)}
        </div>
      )}

      {/* Stat cards row — hidden for cross-sections */}
      {stats && !isCrossSection && stats.points > 0 && (
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2">
          {([
            ['LATEST', stats.latest != null ? fmt(stats.latest) : '—', stats.asOf ? `as of ${stats.asOf}` : ''],
            ['CHANGE', stats.change != null ? `${fmtPct(stats.change)}` : '—', 'vs previous'],
            ['MIN', stats.min != null ? fmt(stats.min) : '—', ''],
            ['MAX', stats.max != null ? fmt(stats.max) : '—', ''],
            ['AVG', stats.avg != null ? fmt(stats.avg) : '—', ''],
            ['POINTS', String(stats.points), 'observations'],
          ] as [string, string, string][]).map(([label, value, sub]) => (
            <div key={label} className="p-2.5 rounded-xl glass-card-subtle border border-border/20">
              <p className="text-[9px] uppercase font-bold tracking-wider text-muted-foreground/70">{label}</p>
              <p className={`text-[15px] font-bold font-mono mt-0.5 ${label === 'CHANGE' ? (stats.change != null && stats.change >= 0 ? 'text-emerald-400' : 'text-rose-400') : 'text-foreground'}`}>
                {value}
              </p>
              {sub && <p className="text-[9px] text-muted-foreground/60 mt-0.5">{sub}</p>}
            </div>
          ))}
        </div>
      )}

      {/* Chart panel */}
      {chartDeps && chartDeps.dates.length > 1 && (
        <div className="rounded-xl glass-card border border-border/10 overflow-hidden">
          <div className="flex items-center justify-between gap-4 flex-wrap px-4 py-3 border-b border-border/10">
            <div>
              <div className="text-[14px] font-semibold text-foreground">{chartTitle}</div>
              <div className="font-mono text-[11px] text-muted-foreground mt-0.5">
                {source.label} · {asOf ? `as of ${asOf}` : ''} · {rows?.length ?? 0} observations
              </div>
            </div>
            <span className="inline-flex items-center gap-1.5 font-mono text-[11px] text-primary bg-primary/10 border border-primary/30 rounded-full px-2.5 py-1">
              <span className="w-1.5 h-1.5 rounded-full bg-primary" />
              {source.label}
            </span>
          </div>

          {/* Chart controls: range, view mode, export */}
          <div className="flex items-center gap-2 flex-wrap px-4 py-2 border-b border-border/10">
            <div className="flex items-center gap-1 p-0.5 rounded-lg bg-accent/30 border border-border/20">
              {(['all', '10Y', '5Y', '1Y'] as const).map((r) => (
                <button
                  key={r}
                  onClick={() => setRange(r)}
                  aria-pressed={range === r}
                  className={`h-6 px-2.5 rounded-md font-mono text-[10px] transition-colors cursor-pointer ${
                    range === r ? 'bg-primary/15 text-primary' : 'text-muted-foreground hover:text-foreground'
                  }`}
                >
                  {r === 'all' ? 'All' : r}
                </button>
              ))}
            </div>
            <div className="flex items-center gap-1 p-0.5 rounded-lg bg-accent/30 border border-border/20">
              {([['level', 'Level'], ['yoy', 'YoY %'], ['change', 'MoM %']] as const).map(([m, label]) => (
                <button
                  key={m}
                  onClick={() => setViewMode(m)}
                  aria-pressed={viewMode === m}
                  className={`h-6 px-2.5 rounded-md font-mono text-[10px] transition-colors cursor-pointer ${
                    viewMode === m ? 'bg-primary/15 text-primary' : 'text-muted-foreground hover:text-foreground'
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
            <div className="flex items-center gap-1 p-0.5 rounded-lg bg-accent/30 border border-border/20">
              {([0, 3, 6, 12] as const).map((n) => (
                <button
                  key={n}
                  onClick={() => setRolling(n)}
                  aria-pressed={rolling === n}
                  className={`h-6 px-2.5 rounded-md font-mono text-[10px] transition-colors cursor-pointer ${
                    rolling === n ? 'bg-primary/15 text-primary' : 'text-muted-foreground hover:text-foreground'
                  }`}
                >
                  {n === 0 ? 'Raw' : `${n}m avg`}
                </button>
              ))}
            </div>
            <button
              onClick={() => setLogScale((v) => !v)}
              aria-pressed={logScale}
              className={`h-6 px-2.5 rounded-md font-mono text-[10px] border transition-colors cursor-pointer ${
                logScale ? 'bg-primary/15 text-primary border-primary/40' : 'text-muted-foreground border-border/30 hover:text-foreground hover:border-foreground'
              }`}
            >
              Log
            </button>
            <button
              onClick={() => setRecession((v) => !v)}
              aria-pressed={recession}
              className={`h-6 px-2.5 rounded-md font-mono text-[10px] border transition-colors cursor-pointer ${
                recession ? 'bg-rose-500/15 text-rose-400 border-rose-500/40' : 'text-muted-foreground border-border/30 hover:text-foreground hover:border-foreground'
              }`}
            >
              Recession
            </button>
            <span className="flex-1" />
            <select
              value={compareId}
              onChange={(e) => { setCompareId(e.target.value); void fetchCompare(e.target.value); }}
              className="h-6 rounded-md bg-background border border-border/30 px-1.5 font-mono text-[10px] text-muted-foreground focus:outline-none focus:border-primary/60"
            >
              <option value="">Compare…</option>
              {ECON_SOURCES.filter((s) => s.id !== source.id).map((s) => (
                <option key={s.id} value={s.id}>{s.label}</option>
              ))}
            </select>
            <button
              onClick={exportPNG}
              className="h-6 px-2.5 rounded-md font-mono text-[10px] text-muted-foreground border border-border/30 hover:text-foreground hover:border-foreground transition-colors cursor-pointer"
            >
              PNG
            </button>
          </div>

          <div ref={chartRef} className="w-full h-72" />
        </div>
      )}

      {/* Cross-section note */}
      {isCrossSection && (
        <p className="text-[10px] font-mono text-muted-foreground/60 px-1">
          Cross-section result — one row per unit, no time dimension. Stats and chart hidden.
        </p>
      )}

      {/* Threshold alert */}
      {stats && stats.latest !== null && (
        <div className="flex items-center gap-2 flex-wrap">
          <span className="font-mono text-[10px] text-muted-foreground">Alert when value</span>
          <select
            value={threshold}
            onChange={(e) => setThreshold(e.target.value)}
            className="h-7 rounded-md bg-background border border-border/30 px-1.5 font-mono text-[10px] text-muted-foreground focus:outline-none focus:border-primary/60"
          >
            <option value="">—</option>
            <option value="above">rises above</option>
            <option value="below">falls below</option>
          </select>
          <input
            type="number"
            placeholder="level"
            value={threshold === 'above' || threshold === 'below' ? thresholdLevel : ''}
            onChange={(e) => setThresholdLevel(e.target.value)}
            className="h-7 w-20 rounded-md bg-background border border-border/30 px-2 font-mono text-[10px] text-foreground focus:outline-none focus:border-primary/60"
          />
          {threshold && thresholdLevel !== '' && stats.latest !== null && (
            <span className={`font-mono text-[10px] ${(threshold === 'above' && stats.latest > Number(thresholdLevel)) || (threshold === 'below' && stats.latest < Number(thresholdLevel)) ? 'text-rose-400' : 'text-primary'}`}>
              {((threshold === 'above' && stats.latest > Number(thresholdLevel)) || (threshold === 'below' && stats.latest < Number(thresholdLevel))) ? '⚠ Triggered' : 'OK'}
            </span>
          )}
        </div>
      )}

      {/* AI analysis */}
      <div className="flex items-center gap-2">
        <button
          onClick={() => void runAnalysis()}
          disabled={researchLoading || !rows || rows.length === 0}
          className="h-9 px-4 rounded-lg bg-primary/10 hover:bg-primary/20 text-primary border border-primary/20 text-[12px] font-semibold inline-flex items-center gap-1.5 cursor-pointer transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
        >
          {researchLoading ? <Loader2 size={12} className="animate-spin" /> : <Sparkles size={12} />}
          {researchLoading ? 'Analyzing…' : 'Analyze with AI'}
        </button>
        <button
          onClick={() => void runNarrative()}
          disabled={narrativeLoading || !rows || rows.length === 0}
          className="h-9 px-4 rounded-lg bg-accent/40 hover:bg-accent/70 border border-border/40 text-muted-foreground hover:text-foreground text-[12px] font-semibold inline-flex items-center gap-1.5 cursor-pointer transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
        >
          {narrativeLoading ? <Loader2 size={12} className="animate-spin" /> : <Sparkles size={12} />}
          {narrativeLoading ? 'Summarizing…' : 'Trend summary'}
        </button>
        {research && (
          <button onClick={() => setResearch(null)} className="h-9 px-3 rounded-lg border border-border/40 text-muted-foreground hover:text-foreground text-[11px] inline-flex items-center gap-1 cursor-pointer">
            <X size={11} /> Close
          </button>
        )}
      </div>
      {narrative && (
        <div className="p-4 rounded-xl glass-card-subtle border border-border/20 animate-fade-in-up">
          <div className="flex items-center gap-2 mb-2">
            <Sparkles size={13} className="text-primary" />
            <span className="text-[11px] font-bold text-primary uppercase tracking-wider">Trend Summary</span>
          </div>
          <p className="text-[12px] text-foreground/90 leading-relaxed whitespace-pre-wrap">{narrative}</p>
        </div>
      )}
      {research && (
        <div className="p-4 rounded-xl bg-primary/[0.04] border border-primary/20 animate-fade-in-up">
          <div className="flex items-center gap-2 mb-2 flex-wrap">
            <Sparkles size={13} className="text-primary" />
            <span className="text-[11px] font-bold text-primary uppercase tracking-wider">AI Research</span>
            {research.verdict && (
              <span className="text-[10px] font-bold font-mono uppercase px-2 py-0.5 rounded-full bg-primary/10 text-primary border border-primary/20">
                {research.verdict}
              </span>
            )}
            {typeof research.confidence === 'number' && (
              <span className="text-[10px] font-mono text-muted-foreground">confidence {Math.round(research.confidence * 100)}%</span>
            )}
          </div>
          <p className="text-[12px] text-foreground/90 leading-relaxed whitespace-pre-wrap">{research.answer}</p>
          {research.agents.length > 0 && (
            <div className="flex flex-wrap gap-1.5 mt-3 pt-2 border-t border-border/20">
              {research.agents.map((a) => (
                <span key={a.name} className="text-[9px] font-mono font-bold uppercase px-2 py-0.5 rounded bg-accent/40 border border-border/30 text-muted-foreground">
                  {a.name} · {a.status} · {a.toolCalls} tools
                </span>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Table */}
      {rows && rows.length > 0 && (
        <div className="rounded-xl border border-border/20 overflow-hidden">
          <div className="overflow-x-auto max-h-80 overflow-y-auto">
            <table className="w-full text-[12px] font-mono">
              <thead className="sticky top-0 bg-[#0f0f0f]/95 backdrop-blur border-b border-border/20">
                <tr>
                  <th className="text-left px-3 py-2 text-[9px] uppercase tracking-wider text-muted-foreground font-bold">Date</th>
                  <th className="text-right px-3 py-2 text-[9px] uppercase tracking-wider text-muted-foreground font-bold">Value</th>
                  {valueKeys.slice(0, 2).map((k) => (
                    <th key={k} className="text-right px-3 py-2 text-[9px] uppercase tracking-wider text-muted-foreground font-bold">{k}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {pageRows.map((r, i) => (
                  <tr key={`${r.date}-${i}`} className="border-b border-border/10 last:border-0 hover:bg-accent/20 transition-colors">
                    <td className="px-3 py-1.5 text-muted-foreground" style={{ borderLeft: `3px solid ${source.color}` }}>{r.date}</td>
                    <td className="px-3 py-1.5 text-right font-bold text-foreground">{r.value !== null ? fmt(r.value) : <span className="text-muted-foreground/40">—</span>}</td>
                    {valueKeys.slice(0, 2).map((k) => (
                      <td key={k} className="px-3 py-1.5 text-right text-muted-foreground/80 truncate max-w-[140px]">{String(r[k] ?? '')}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {totalPages > 1 && (
            <div className="flex items-center justify-between px-3 py-2 border-t border-border/20 text-[11px] font-mono text-muted-foreground">
              <span>page {page} of {totalPages} · {rows.length} rows</span>
              <div className="flex gap-1.5">
                <button onClick={() => setPage((p) => Math.max(1, p - 1))} disabled={page === 1} className="px-2.5 py-1 rounded-lg bg-accent/40 hover:bg-accent/70 border border-border/40 disabled:opacity-40 cursor-pointer">Prev</button>
                <button onClick={() => setPage((p) => Math.min(totalPages, p + 1))} disabled={page === totalPages} className="px-2.5 py-1 rounded-lg bg-accent/40 hover:bg-accent/70 border border-border/40 disabled:opacity-40 cursor-pointer">Next</button>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Empty state */}
      {rows && rows.length === 0 && !loading && (
        <div className="py-8 text-center text-[12px] font-mono text-muted-foreground border border-dashed border-border/25 rounded-xl">
          No observations returned for this query. Try different parameters.
        </div>
      )}
    </div>
  );
}

// Detect a categorical label column (e.g. country / indicator) that indicates
// multiple series in the result — used for multi-series chart overlay.
function detectLabelKey(rows: EconPoint[]): string | null {
  if (rows.length < 4) return null;
  for (const key of ['country', 'countryName', 'country_name', 'geo', 'ref_area', 'indicator', 'seriesName', 'location', 'name']) {
    const vals = new Set(rows.map((r) => String(r[key] ?? '')));
    if (vals.size >= 2 && vals.size <= 20) return key;
  }
  return null;
}

function fmt(v: number): string {
  if (!Number.isFinite(v)) return '—';
  const abs = Math.abs(v);
  if (abs >= 1e12) return `${(v / 1e12).toFixed(2)}T`;
  if (abs >= 1e9) return `${(v / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `${(v / 1e6).toFixed(2)}M`;
  if (abs >= 1e3) return v.toLocaleString(undefined, { maximumFractionDigits: 2 });
  return String(Number(v.toFixed(4)));
}

function fmtPct(v: number): string {
  return `${v >= 0 ? '+' : ''}${(v * 100).toFixed(2)}%`;
}

// Year-over-year percent change: (v - v_12_periods_ago) / v_12_periods_ago * 100.
function yoySeries(points: EconPoint[]): EconPoint[] {
  const out: EconPoint[] = [];
  for (let i = 12; i < points.length; i++) {
    const prev = points[i - 12].value;
    const cur = points[i].value;
    if (prev === null || prev === undefined || prev === 0 || cur === null || cur === undefined) continue;
    const v = ((cur - prev) / prev) * 100;
    if (Number.isFinite(v)) out.push({ date: points[i].date, value: v });
  }
  return out;
}

// Period-over-period percent change: (v - prev) / prev * 100.
function pctChangeSeries(points: EconPoint[]): EconPoint[] {
  const out: EconPoint[] = [];
  for (let i = 1; i < points.length; i++) {
    const prev = points[i - 1].value;
    const cur = points[i].value;
    if (prev === null || prev === undefined || prev === 0 || cur === null || cur === undefined) continue;
    const v = ((cur - prev) / prev) * 100;
    if (Number.isFinite(v)) out.push({ date: points[i].date, value: v });
  }
  return out;
}

// Simple n-period rolling average (centered on the last point of each window).
function rollingAvg(points: EconPoint[], n: number): EconPoint[] {
  const out: EconPoint[] = [];
  for (let i = n - 1; i < points.length; i++) {
    let sum = 0;
    let count = 0;
    for (let j = i - n + 1; j <= i; j++) {
      const v = points[j].value;
      if (v !== null && v !== undefined) { sum += v; count++; }
    }
    if (count > 0) out.push({ date: points[i].date, value: sum / count });
  }
  return out;
}

// Build recession bands from the NBER USREC series (0/1) aligned to the chart dates.
function buildRecessionBands(dates: string[]): { start: string; end: string }[] {
  const bands: { start: string; end: string }[] = [];
  let inRecession = false;
  let start = '';
  for (const d of dates) {
    const ts = Date.parse(d);
    if (Number.isNaN(ts)) continue;
    const year = new Date(ts).getUTCFullYear();
    // USREC is 1 during NBER recessions; approximate by year ranges for shading.
    const isRec = [2001, 2008, 2009, 2020].includes(year);
    if (isRec && !inRecession) { inRecession = true; start = d; }
    else if (!isRec && inRecession) { bands.push({ start, end: d }); inRecession = false; }
  }
  if (inRecession) bands.push({ start, end: dates[dates.length - 1] });
  return bands;
}