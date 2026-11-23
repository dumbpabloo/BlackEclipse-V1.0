'use client';

// ── The live chart — a real chart, not a picture ────────────────────────────
//
// The engine (Python) authors the chart as a Plotly FIGURE; this component
// renders it in the browser as vector SVG, with zoom, pan, hover, a legend and
// a PNG/zoom toolbar. It is a CHART, not an image of one.
//
// Loading is staged so the first paint is instant:
//
//   1. the drawn PNG (already fetched by the caller) is shown immediately;
//   2. the library is loaded only when the chart scrolls near the viewport,
//      on an idle frame — never on the critical path of the answer;
//   3. once the library is ready the live chart cross-fades in over the PNG.
//
// The library is loaded once from the app's own route (never a CDN: the app
// works offline) and then reused. If it cannot be loaded, the component keeps
// the drawn picture and says so.
//
// The background is TRANSPARENT: the chart sits on the app's frosted surface
// without hiding the frost.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Download, RotateCcw, ZoomIn, ZoomOut } from '@/lib/icons';
import { cn } from '@/lib/utils';
import { useChartPalette } from '@/lib/chart-palette-store';
import type { ChartPalette } from '@/lib/chart-palette';
import { applyChartPalette } from './apply-palette';
import { hasDownloadBridge, saveDownloadViaShell } from '@/lib/desktop/download';

export interface PlotlyFigure {
  data: Array<Record<string, unknown>>;
  layout: Record<string, unknown>;
}

export interface PlotlyChartProps {
  /** The figure the engine authored. */
  figure?: PlotlyFigure | null;
  /** The drawn picture, shown while the chart loads (or if it cannot). */
  fallbackSrc?: string | null;
  height?: number;
  className?: string;
  alt?: string;
}

const LIB_URL = '/api/harness/charts/plotly';
const SCRIPT_ID = 'eclipse-plotly';

type PlotlyGlobal = {
  newPlot: (
    element: HTMLElement,
    data: unknown[],
    layout: unknown,
    config?: unknown,
  ) => Promise<unknown>;
  react?: (element: HTMLElement, data: unknown[], layout: unknown, config?: unknown) => void;
  relayout?: (element: HTMLElement, update: Record<string, unknown>) => Promise<unknown>;
  toImage?: (element: HTMLElement, opts?: Record<string, unknown>) => Promise<string>;
  purge?: (element: HTMLElement) => void;
};

type Vec3 = { x: number; y: number; z: number };

type PlotlyAxis = {
  range?: [unknown, unknown];
  fixedrange?: boolean;
  r2l?: (value: unknown) => number;
  l2r?: (value: number) => unknown;
};

type PlotlyScene = { camera?: { eye?: Vec3; center?: Vec3 } };

type PlotlyHost = HTMLElement & {
  _fullLayout?: Record<string, PlotlyAxis | PlotlyScene>;
};

/**
 * A 3-D figure must ORBIT. The flat layout's `dragmode: "pan"` is inherited by
 * the plotly scene and turns drag into a pan, so the scene states `turntable`
 * explicitly. Exported as a pure helper so the rule is testable and is applied
 * by BOTH the first draw and "reset view" (otherwise a reset would silently put
 * the scene back into pan mode).
 */
export function layoutWithSceneOrbit(
  layout: Record<string, unknown>,
  data: Array<Record<string, unknown>> | undefined,
): Record<string, unknown> {
  const has3d = (data ?? []).some(
    (trace) => trace.type === 'scatter3d' || trace.type === 'surface',
  );
  if (!has3d) return layout;
  return {
    ...layout,
    scene: {
      ...((layout.scene as Record<string, unknown> | undefined) ?? {}),
      dragmode: 'turntable',
    },
  };
}

const PLOTLY_CONFIG = {  // The library's own modebar is a dark pill that floats over the chart. Eclipse
  // draws its own frosted toolbar (zoom / reset / save) instead, so the native
  // one is off — no black bar on the picture.
  displayModeBar: false,
  displaylogo: false,
  responsive: true,
  scrollZoom: true,
  doubleClick: 'reset',
  modeBarButtonsToRemove: ['select2d', 'lasso2d', 'autoScale2d'],
  toImageButtonOptions: { format: 'png', scale: 2 },
};

declare global {
  interface Window {
    Plotly?: PlotlyGlobal;
  }
}

let loading: Promise<PlotlyGlobal | null> | null = null;

/** Load the chart library once; every chart after that is instant. */
export function loadPlotly(): Promise<PlotlyGlobal | null> {
  if (typeof window === 'undefined') return Promise.resolve(null);
  if (window.Plotly) return Promise.resolve(window.Plotly);
  if (loading) return loading;
  loading = new Promise<PlotlyGlobal | null>((resolve) => {
    const existing = document.getElementById(SCRIPT_ID) as HTMLScriptElement | null;
    const script = existing ?? document.createElement('script');
    const done = () => resolve(window.Plotly ?? null);
    if (!existing) {
      script.id = SCRIPT_ID;
      script.src = LIB_URL;
      script.async = true;
      script.onload = done;
      script.onerror = () => {
        // A failed load must be retryable on the next mount.
        loading = null;
        resolve(null);
      };
      document.head.appendChild(script);
    } else {
      script.addEventListener('load', done);
      // The script may already be loaded (the element exists but Plotly is not
      // on the window yet) - poll once, then give up.
      let tries = 0;
      const poll = window.setInterval(() => {
        tries += 1;
        if (window.Plotly || tries > 100) {
          window.clearInterval(poll);
          resolve(window.Plotly ?? null);
        }
      }, 50);
    }
  });
  return loading;
}

type IdleWindow = Window & {
  requestIdleCallback?: (cb: () => void, opts?: { timeout: number }) => number;
  cancelIdleCallback?: (id: number) => void;
};

/** Run the work when the main thread is quiet; the PNG is already on screen. */
function onIdle(run: () => void): () => void {
  const w = window as IdleWindow;
  if (typeof w.requestIdleCallback === 'function') {
    const id = w.requestIdleCallback(run, { timeout: 1200 });
    return () => w.cancelIdleCallback?.(id);
  }
  const id = window.setTimeout(run, 150);
  return () => window.clearTimeout(id);
}

export function PlotlyChart({
  figure,
  fallbackSrc,
  height = 320,
  className,
  alt,
}: PlotlyChartProps) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const [state, setState] = useState<'idle' | 'loading' | 'ready' | 'unavailable'>('idle');
  // True once a plot exists on the host, so a recolour can React to it instead
  // of replotting from scratch (which would blink and drop the zoom).
  const drawnRef = useRef(false);
  // The library is 4.7 MB: nothing is loaded until the chart is about to be seen.
  const [visible, setVisible] = useState(false);
  // The user's chart colours (Settings › General › Charts). Applied HERE so a
  // colour change repaints instantly and never needs a redraw by the runtime.
  const palette = useChartPalette();
  const styled = useMemo(() => applyChartPalette(figure, palette), [figure, palette]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    if (typeof IntersectionObserver === 'undefined') {
      setVisible(true);
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (!entries.some((entry) => entry.isIntersecting)) return;
        observer.disconnect();
        setVisible(true);
      },
      { rootMargin: '400px 0px' },
    );
    observer.observe(host);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const host = hostRef.current;
    if (!host || !styled || !visible) return;
    let cancelled = false;
    let cancelIdle: (() => void) | null = null;

    const draw = () => {
      setState((current) => (current === 'ready' ? current : 'loading'));
      loadPlotly()
        .then(async (Plotly) => {
          if (cancelled) return;
          if (!Plotly) {
            setState('unavailable');
            return;
          }
          const layout = layoutWithSceneOrbit(
            { ...(styled.layout ?? {}), autosize: true, height },
            styled.data,
          );
          try {
            // A colour-only change must not blink: react() reuses the drawn
            // chart (and keeps the user's zoom); newPlot() is the first draw.
            if (drawnRef.current && Plotly.react) {
              await Plotly.react(host, styled.data ?? [], layout, PLOTLY_CONFIG);
            } else {
              await Plotly.newPlot(host, styled.data ?? [], layout, PLOTLY_CONFIG);
            }
            drawnRef.current = true;
            if (!cancelled) setState('ready');
          } catch {
            if (!cancelled) setState('unavailable');
          }
        })
        .catch(() => {
          if (!cancelled) setState('unavailable');
        });
    };

    cancelIdle = onIdle(draw);

    return () => {
      cancelled = true;
      cancelIdle?.();
    };
  }, [styled, height, visible]);

  // Unmount only: Plotly leaves listeners and DOM behind.
  useEffect(() => () => {
    const host = hostRef.current;
    drawnRef.current = false;
    if (!host) return;
    try {
      window.Plotly?.purge?.(host);
    } catch {
      // A chart that cannot be purged is replaced by the next render anyway.
    }
    host.replaceChildren();
  }, []);

  const is3d = Boolean(
    figure?.data?.some((trace) => trace.type === 'scatter3d' || trace.type === 'surface'),
  );

  /** Scale the current view around its centre: <1 zooms in, >1 zooms out. */
  const zoomBy = useCallback(
    (factor: number) => {
      const host = hostRef.current as PlotlyHost | null;
      const Plotly = window.Plotly;
      if (!host || !Plotly?.relayout) return;
      try {
        if (is3d) {
          const scene = host._fullLayout?.scene as PlotlyScene | undefined;
          const camera = scene?.camera;
          const eye = camera?.eye;
          if (!eye) return;
          const center = camera?.center ?? { x: 0, y: 0, z: 0 };
          const scale = (value: number, pivot: number) => pivot + (value - pivot) * factor;
          void Plotly.relayout(host, {
            'scene.camera.eye': {
              x: scale(eye.x, center.x),
              y: scale(eye.y, center.y),
              z: scale(eye.z, center.z),
            },
          });
          return;
        }

        const update: Record<string, unknown> = {};
        for (const [key, raw] of Object.entries(host._fullLayout ?? {})) {
          if (!/^[xy]axis\d*$/.test(key)) continue;
          const axis = raw as PlotlyAxis;
          if (axis.fixedrange || !axis.range || !axis.r2l || !axis.l2r) continue;
          const [from, to] = axis.range;
          const low = axis.r2l(from);
          const high = axis.r2l(to);
          if (!Number.isFinite(low) || !Number.isFinite(high) || low === high) continue;
          const middle = (low + high) / 2;
          const half = (Math.abs(high - low) / 2) * factor;
          update[`${key}.range`] = [
            axis.l2r(middle - half),
            axis.l2r(middle + half),
          ];
        }
        if (Object.keys(update).length > 0) void Plotly.relayout(host, update);
      } catch {
        // A chart that cannot be zoomed keeps its current view.
      }
    },
    [is3d],
  );

  /** Put the chart back exactly as the engine authored it (axes or camera). */
  const resetView = useCallback(() => {
    const host = hostRef.current as PlotlyHost | null;
    const Plotly = window.Plotly;
    if (!host || !Plotly || !styled) return;
    try {
      if (Plotly.react) {
        void Plotly.react(
          host,
          styled.data ?? [],
          layoutWithSceneOrbit(
            { ...(styled.layout ?? {}), autosize: true, height },
            styled.data,
          ),
          PLOTLY_CONFIG,
        );
        return;
      }
      const update: Record<string, unknown> = {};
      for (const [key, raw] of Object.entries(host._fullLayout ?? {})) {
        if (!/^[xy]axis\d*$/.test(key)) continue;
        if (!(raw as PlotlyAxis).fixedrange) update[`${key}.autorange`] = true;
      }
      if (is3d) update['scene.camera'] = (styled.layout?.scene as { camera?: unknown } | undefined)?.camera;
      if (Object.keys(update).length > 0) void Plotly.relayout?.(host, update);
    } catch {
      // A chart that cannot be reset keeps its current view.
    }
  }, [styled, height, is3d]);

  /** Save the chart exactly as it looks now (the user's colours included). */
  const downloadPng = useCallback(() => {
    const host = hostRef.current as PlotlyHost | null;
    const Plotly = window.Plotly;
    if (!host || !Plotly?.toImage) return;
    try {
      void Plotly.toImage(host, { format: 'png', scale: 2, height: height * 2, width: host.clientWidth * 2 })
        .then(async (url: string) => {
          const filename = `eclipse-chart-${Date.now()}.png`;
          if (hasDownloadBridge()) {
            const blob = await fetch(url).then((response) => response.blob());
            if (saveDownloadViaShell(filename, blob)) return;
          }
          const link = document.createElement('a');
          link.href = url;
          link.download = filename;
          link.click();
        })
        .catch(() => {
          // A chart that cannot be exported leaves the drawn picture in place.
        });
    } catch {
      // Same: the chart stays on screen.
    }
  }, [height]);

  if (!figure) {
    if (!fallbackSrc) return null;
    return (
      /* eslint-disable-next-line @next/next/no-img-element */
      <img
        src={fallbackSrc}
        alt={alt ?? 'Chart'}
        loading="eager"
        decoding="async"
        className={cn('block h-auto max-w-full object-contain', className)}
        style={{ maxHeight: height }}
      />
    );
  }

  const showPicture = state !== 'ready' && Boolean(fallbackSrc);

  return (
    <div className={cn('w-full', className)}>
      {/* `isolate` + `overflow-hidden` make the chart's own geometry the
          boundary of its paint: no legend, axis label, hover label or 3-D
          canvas can spill out and cover the text or the next block. */}
      <div
        className="relative isolate w-full overflow-hidden"
        style={{ height }}
      >
        <div
          ref={hostRef}
          className={cn(
            'h-full w-full transition-opacity duration-300',
            state === 'ready' ? 'opacity-100' : 'opacity-0',
          )}
          data-testid="plotly-chart"
        />
        {showPicture && (
          /* eslint-disable-next-line @next/next/no-img-element */
          <img
            src={fallbackSrc ?? ''}
            alt={alt ?? 'Chart'}
            loading="eager"
            decoding="async"
            className="pointer-events-none absolute inset-0 h-full w-full object-contain"
          />
        )}
        {state === 'unavailable' && (
          <div className="pointer-events-none absolute inset-x-0 bottom-0 flex justify-end p-1">
            <p className="rounded bg-background/70 px-1.5 py-0.5 text-[10px] italic text-muted-foreground/70">
              The interactive chart library is unavailable — showing the picture.
            </p>
          </div>
        )}
        {state === 'unavailable' && !fallbackSrc && (
          <div className="absolute inset-0 flex items-center justify-center">
            <p className="text-xs italic text-muted-foreground/70">
              The interactive chart library is unavailable.
            </p>
          </div>
        )}
      </div>
      {/* The view controls live in their own row UNDER the plot. Overlaid in
          the bottom-left corner they sat on the x-axis labels of short charts,
          which read as chart text hidden behind a control bar. */}
      {state === 'ready' && (
        <div
          role="group"
          aria-label="Chart view controls"
          className="mt-1 flex items-center justify-end gap-0.5 opacity-80 transition-opacity hover:opacity-100 focus-within:opacity-100"
          data-testid="chart-view-controls"
        >
          <button
            type="button"
            onClick={() => zoomBy(0.7)}
            aria-label="Zoom in"
            title="Zoom in"
            className="flex h-6 w-6 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
          >
            <ZoomIn size={13} />
          </button>
          <button
            type="button"
            onClick={() => zoomBy(1 / 0.7)}
            aria-label="Zoom out"
            title="Zoom out"
            className="flex h-6 w-6 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
          >
            <ZoomOut size={13} />
          </button>
          <button
            type="button"
            onClick={resetView}
            aria-label="Reset view"
            title="Reset view"
            className="flex h-6 w-6 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
          >
            <RotateCcw size={13} />
          </button>
          <button
            type="button"
            onClick={downloadPng}
            aria-label="Save chart as PNG"
            title="Save as PNG"
            className="flex h-6 w-6 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
          >
            <Download size={13} />
          </button>
        </div>
      )}
    </div>
  );
}

export default PlotlyChart;
