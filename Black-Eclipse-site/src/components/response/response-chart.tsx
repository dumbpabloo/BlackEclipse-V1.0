'use client';

// ── ResponseChart — a chat chart, drawn by the Python chart engine ──────────
//
// A chart block in an answer is drawn by the runtime's chart engine: the browser
// asks the engine for the picture (and the live Plotly figure) and shows the
// artifact it produced.
//
// The block may arrive in two shapes:
//
//   * the SIMPLE form — `chartType` plus `series` — which the engine renders
//     directly;
//   * the SCENE form — `panels` of `marks` — which is compositional, so the
//     model can draw ANY chart the user asks for (a violin, a box plot, a
//     Gantt, a funnel, a ridgeline, a dual-axis chart) without a new chart
//     kind existing for it.
//
// A block (or a scene mark) may NAME its series (`dataSource`); it is resolved
// through the app's own price-history route before the spec is built.
//
// Loading is fast by design: the drawn PNG paints the moment it arrives, and
// the live Plotly chart is loaded lazily (in the PlotlyChart component) only
// when the chart is about to be seen. A render is cached per spec, so chat
// history scrolls without asking the engine to draw the same chart again.

import { useCallback, useEffect, useMemo, useState } from 'react';
import { createPortal } from 'react-dom';
import { ChartRenderError, chartImageSrc, isRetryableChartError, renderChart, type ChartFigureData, type RenderedChart } from '@/lib/harness/charts';
import { PlotlyChart } from '@/components/charts/plotly-chart';
import { chartBlockToSpec, type ChartPlan } from '@/lib/response/chart-spec';
import { chartSpecWarning } from '@/lib/response/chart-capabilities';
import { fetchQuantFigure, hydrateChartBlock } from '@/lib/response/chart-data';
import { registerChartArtifact, unregisterChart } from '@/lib/response/chart-registry';
import { Code, Loader2, Maximize2, Minimize2, X } from '@/lib/icons';
import { generateChartSource } from '@/lib/harness/clients/notebook-client';
import { useNotebook } from '@/components/chat/notebook-provider';
import type { ChartBlock } from '@/lib/response/contract';
import { cn } from '@/lib/utils';

export interface ResponseChartProps {
  block: ChartBlock;
  height?: number;
  className?: string;
  /** When false the chart keeps its inline size and offers no focus control. */
  focusable?: boolean;
}

type State =
  | { status: 'loading' }
  | { status: 'starting'; message: string }
  | { status: 'ready'; src: string; chart?: ChartFigureData; plan: ChartPlan }
  | { status: 'empty'; message: string }
  | { status: 'error'; message: string; code?: string };

// A render is keyed on the resolved SPEC, not the block: the same numbers draw
// the same chart, so re-mounting a message (scrolling, focus) is instant.
const RENDER_CACHE = new Map<string, RenderedChart>();
const RENDER_IN_FLIGHT = new Map<string, Promise<RenderedChart>>();
const RENDER_CACHE_LIMIT = 40;

function cachedRender(specKey: string, spec: ChartPlan['spec']): Promise<RenderedChart> {
  const cached = RENDER_CACHE.get(specKey);
  if (cached) return Promise.resolve(cached);
  const inFlight = RENDER_IN_FLIGHT.get(specKey);
  if (inFlight) return inFlight;
  const run = renderChart(spec)
    .then((rendered) => {
      RENDER_CACHE.set(specKey, rendered);
      if (RENDER_CACHE.size > RENDER_CACHE_LIMIT) {
        const oldest = RENDER_CACHE.keys().next().value;
        if (oldest !== undefined) RENDER_CACHE.delete(oldest);
      }
      return rendered;
    })
    .finally(() => RENDER_IN_FLIGHT.delete(specKey));
  RENDER_IN_FLIGHT.set(specKey, run);
  return run;
}

export function ResponseChart({ block, height = 260, className, focusable = true }: ResponseChartProps) {
  const [state, setState] = useState<State>({ status: 'loading' });
  const [focused, setFocused] = useState(false);
  const [editingCode, setEditingCode] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);
  /** Bumped by "Try again"; re-runs the render effect. */
  const [retryNonce, setRetryNonce] = useState(0);
  const notebook = useNotebook();

  // The block is a fresh object on every render in most callers, so the work is
  // keyed on its CONTENT, not its identity.
  const blockKey = useMemo(() => JSON.stringify(block), [block]);
  // A computed quant figure has no series of its own; its spec is built from a
  // stored experiment. It still gets a registry id so PNG/PDF export works.
  const chartId = block.id ?? (
    block.quant ? `quant:${block.quant.experimentId}:${block.quant.figure}` : undefined
  );

  useEffect(() => {
    let cancelled = false;
    setState({ status: 'loading' });
    void (async () => {
      // A chart asked for while the Python runtime is booting (or restarting
      // after a code change) is not a failure: the runtime is installed and on
      // its way. Keep the surface in a calm "starting" state and retry, so the
      // user never sees "install the runtime" for a runtime that already
      // exists. Only a genuine, persistent failure becomes an error box.
      const MAX_RETRIES = 36;
      for (let attempt = 0; ; attempt += 1) {
        try {
          let plan: ChartPlan | null;
          if (block.quant) {
            const spec = await fetchQuantFigure(block.quant);
            if (cancelled) return;
            plan = { spec, kind: spec.kind, notes: [] };
          } else {
            const hydrated = await hydrateChartBlock(block);
            if (cancelled) return;
            plan = chartBlockToSpec(hydrated, { theme: 'eclipse-dark', width: 1000, height: 560 });
          }
          if (!plan) {
            setState({ status: 'empty', message: 'No data series available to chart.' });
            return;
          }
          const warning = chartSpecWarning(plan.spec);
          if (warning) {
            setState({ status: 'error', message: warning });
            return;
          }
          const specKey = JSON.stringify(plan.spec);
          const rendered = await cachedRender(specKey, plan.spec);
          if (cancelled) return;
          // A chart whose artifact could not be recorded still has bytes: show
          // them inline. Only a render that produced no picture is an error.
          const src = chartImageSrc(rendered);
          if (!src) {
            setState({ status: 'error', message: 'the chart engine returned no picture' });
            return;
          }
          const artifactId = rendered.artifact.id;
          // The export helpers fetch the picture by artifact id.
          if (chartId && artifactId) registerChartArtifact(chartId, artifactId);
          setState({ status: 'ready', src, chart: rendered.chart, plan });
          return;
        } catch (err) {
          if (cancelled) return;
          if (isRetryableChartError(err) && attempt < MAX_RETRIES) {
            setState({
              status: 'starting',
              message: 'Starting the chart engine — a first launch can take a minute.',
            });
            await new Promise((resolve) =>
              setTimeout(resolve, Math.min(1500 + attempt * 600, 6000)),
            );
            if (cancelled) return;
            continue;
          }
          setState({
            status: 'error',
            message: err instanceof Error ? err.message : 'the chart could not be drawn',
            code: err instanceof ChartRenderError ? err.code : undefined,
          });
          return;
        }
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [blockKey, retryNonce]);

  useEffect(() => {
    if (!chartId) return;
    return () => unregisterChart(chartId);
  }, [chartId]);

  const editCode = useCallback(async () => {
    if (!notebook || state.status !== 'ready' || editingCode) return;
    setEditingCode(true);
    setEditError(null);
    try {
      const generated = await generateChartSource(state.plan.spec);
      notebook.createChartCell({
        source: generated.source,
        chartBlockId: block.id,
        // Use the runtime's resolved spec: a named `dataSource` is now inline,
        // so the script's `inputs["chart"]` is exactly what it draws.
        inputs: { chart: generated.spec ?? state.plan.spec },
        unsupported: generated.unsupported,
      });
    } catch (err) {
      setEditError(err instanceof Error ? err.message : 'The chart code could not be opened.');
    } finally {
      setEditingCode(false);
    }
  }, [block.id, editingCode, notebook, state]);

  const body = (() => {
    if (state.status === 'ready') {
      return (
        <div className="space-y-1">
          {/* The LIVE chart: vector, zoomable, pannable. The drawn picture is
              shown immediately and is the fallback (and the export). */}
          <PlotlyChart
            figure={state.chart}
            fallbackSrc={state.src}
            height={
              focused
                ? typeof window !== 'undefined'
                  ? Math.round(window.innerHeight * 0.78)
                  : 640
                : Math.max(420, height)
            }
            alt={block.title ?? 'Chart'}
            className="w-full"
          />
          {state.plan.notes.length > 0 && (
            <p className="text-[10px] text-muted-foreground/70">{state.plan.notes.join(' ')}</p>
          )}
          {block.quant && (
            <p className="text-[10px] text-muted-foreground/60">
              {block.quant.caption
                || `Computed by the quant engine from ${block.quant.experimentId} · ${block.quant.figure}`}
            </p>
          )}
        </div>
      );
    }
    if (state.status === 'loading' || state.status === 'starting') {
      const starting = state.status === 'starting';
      return (
        <div
          className="flex w-full flex-col items-center justify-center gap-1 rounded-xl border border-border/10"
          style={{ height }}
          aria-busy="true"
          data-testid={starting ? 'response-chart-starting' : 'response-chart-loading'}
        >
          <span className="flex items-center gap-2 text-xs text-muted-foreground/70">
            <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-muted-foreground/30 border-t-primary" />
            {starting ? 'Starting the chart engine…' : 'Drawing the chart…'}
          </span>
          {starting && (
            <span className="text-[10px] text-muted-foreground/50">
              A first launch can take a minute — the chart appears on its own.
            </span>
          )}
        </div>
      );
    }
    // A named unavailability is a REPAIR instruction, not a vague failure.
    // `CHART_RENDER_UNAVAILABLE` means the runtime is up but its drawing
    // library is missing; showing only "unavailable" hid which capability was
    // gone. The route now supplies the interpreter + missing modules, and this
    // shows them as the cause rather than as passing italic prose.
    const engineDown = state.status === 'error' && state.code === 'CHART_RENDER_UNAVAILABLE';
    return (
      <div
        className={cn(
          'flex w-full items-center justify-center rounded-xl border border-border/10 px-3 text-center',
          state.status === 'error' && 'border-amber-500/20',
        )}
        style={{ height }}
        data-testid={state.status === 'error' ? 'response-chart-error' : 'response-chart-empty'}
        data-error-code={state.status === 'error' ? state.code ?? '' : undefined}
      >
        {engineDown ? (
          <div className="max-w-[560px] space-y-1 text-left">
            <p className="text-xs font-medium text-amber-400/90">The chart engine cannot draw</p>
            <p className="text-[11px] leading-relaxed text-amber-500/70">{state.message}</p>
            <p className="text-[10px] text-muted-foreground/60">
              Run <code className="rounded bg-foreground/10 px-1 py-0.5">npm run setup:python</code> to install the
              missing drawing libraries, then restart Eclipse.
            </p>
            <button
              type="button"
              onClick={() => setRetryNonce((n) => n + 1)}
              className="text-[11px] text-primary/80 underline-offset-2 hover:underline"
            >
              Try again
            </button>
          </div>
        ) : (
          <div className="flex flex-col items-center gap-1">
            <p className={cn('text-xs italic', state.status === 'error' ? 'text-amber-500/80' : 'text-muted-foreground/70')}>
              {state.message}
            </p>
            {state.status === 'error' && (
              <button
                type="button"
                onClick={() => setRetryNonce((n) => n + 1)}
                className="text-[11px] text-primary/80 underline-offset-2 hover:underline"
              >
                Try again
              </button>
            )}
          </div>
        )}
      </div>
    );
  })();

  const card = (
    <div
      className={cn('w-full', focused && 'mx-auto flex max-h-full max-w-[1600px] flex-col')}
      // The focus overlay closes on a backdrop click; the chart's own toolbar
      // (and its zoom/reset controls) must not count as a backdrop click.
      onClick={(event) => event.stopPropagation()}
    >
      {focusable && state.status === 'ready' && (
        <div className="mb-1 flex justify-end gap-1">
          {notebook && (
            <button
              type="button"
              onClick={() => void editCode()}
              disabled={editingCode}
              aria-label="Edit code"
              title="Edit the code behind this chart"
              className="flex h-6 w-6 items-center justify-center rounded-md border border-border/50 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:opacity-40"
            >
              {editingCode ? <Loader2 size={12} className="animate-spin" /> : <Code size={12} />}
            </button>
          )}
          <button
            type="button"
            onClick={() => setFocused((value) => !value)}
            aria-label={focused ? 'Leave focus view' : 'Open focus view'}
            aria-pressed={focused}
            className="flex h-6 w-6 items-center justify-center rounded-md border border-border/50 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
          >
            {focused ? <Minimize2 size={12} /> : <Maximize2 size={12} />}
          </button>
          {focused && (
            <button
              type="button"
              onClick={() => setFocused(false)}
              aria-label="Close focus view"
              className="flex h-6 w-6 items-center justify-center rounded-md border border-border/50 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
            >
              <X size={12} />
            </button>
          )}
        </div>
      )}
      <div className={cn('relative mx-auto w-full', focused ? 'max-w-[1400px]' : 'max-w-[1100px]')}>
        {body}
        {editError && <p className="mt-1 text-[10px] text-amber-400/80">{editError}</p>}
      </div>
    </div>
  );

  if (!focused) return card;

  // The full view is PORTALLED to <body>: inside the chat it would live in the
  // chat's stacking context, so the app's own header (the top bar and the
  // price ribbon) would paint OVER the chart and collide with its toolbar. On
  // <body> the overlay sits above the shell, so the chart's toolbar is clear of
  // the header - and the backdrop stays the same frosted blur it has always
  // been, so the app reads through it.
  if (typeof document === 'undefined') return card;
  return createPortal(
    <div
      className="fixed inset-0 z-[300] flex flex-col bg-black/55 p-4 backdrop-blur-sm"
      role="dialog"
      aria-modal="true"
      aria-label={block.title ? `Chart: ${block.title}` : 'Chart focus view'}
      onClick={() => setFocused(false)}
      data-testid="response-chart-focus"
    >
      {card}
    </div>,
    document.body,
  );
}

export default ResponseChart;
