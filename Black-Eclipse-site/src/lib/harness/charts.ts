// The chart engine's browser client.
//
// Eclipse draws its charts with Python (matplotlib) in the runtime. This
// module is the browser's half of that contract: it describes a chart (data
// only — never code, never a callback) and asks the front door to draw it.
//
// The picture comes back as an ARTIFACT, not as bytes: the chart is recorded
// in the Artifact Center, so it can be downloaded, versioned and re-shared
// like any other produced file. The browser then fetches it back through the
// artifact route.
//
// A chart is always `computed` — it is never evidence. That label is part of
// the contract, not a caption the UI adds.

export type ChartKind =
  | "line" | "area" | "bar" | "stackedBar" | "pie" | "donut" | "scatter"
  | "histogram" | "heatmap" | "candlestick" | "waterfall" | "radar"
  | "gauge" | "treemap"
  // The compositional form: panels of marks (any chart).
  | "scene";

export type ChartTheme = "eclipse-dark" | "eclipse-light";

export interface ChartBand {
  /** The lower edge of the range. `null` is a missing reading. */
  lower: Array<number | null>;
  /** The upper edge of the range. */
  upper: Array<number | null>;
}

export interface ChartSeries {
  name: string;
  /** Scalars, or points written as pairs/triples/OHLC (a scatter, a candle). */
  data: Array<
    number | null | [number, number] | [number, number, number] | [number, number, number, number]
  >;
  color?: string;
  style?: "line" | "area" | "bar" | "step" | "scatter";
  /** An optional range drawn around the series (a forecast band). */
  band?: ChartBand;
  /** Per-point colours, in the same order as `data` (a sign, a slice). */
  colors?: Array<string | null>;
  /** Draw the series as a dashed line (a benchmark, a reference series). */
  dashed?: boolean;
}

/** One tile of a treemap: a name, a size (the area) and a colour. */
export interface ChartTile {
  name: string;
  value: number;
  color?: string;
  label?: string;
}

export interface ChartPoint {
  x: number;
  y: number;
  label?: string;
  size?: number;
  color?: string;
}

export interface ChartCandle {
  open: number;
  high: number;
  low: number;
  close: number;
}

export interface ChartReferenceLine {
  label: string;
  value: number;
  color?: string;
  dashed?: boolean;
}

/**
 * WHERE a mark's numbers come from, instead of the numbers themselves.
 *
 * The engine fetches the series through the same market gateway every price
 * comes from. A chart that names its source therefore draws real numbers
 * without the model ever typing 250 of them into a message - a long message
 * gets truncated, and a truncated chart block is a lost chart.
 */
export interface ChartDataSource {
  symbol: string;
  market?: string;
  interval?: string;
  start?: string;
  end?: string;
  field?: "close" | "open" | "high" | "low" | "volume";
  name?: string;
}

/** One drawn primitive of a scene. */
export interface ChartMark {
  type:
    | "line" | "area" | "bar" | "scatter" | "band" | "rule" | "label"
    | "box" | "violin" | "histogram" | "errorbar" | "heatmap" | "pie"
    | "donut" | "candlestick" | "waterfall" | "ridge" | "stem"
    | "treemap" | "sunburst" | "funnel" | "sankey"
    // The 3D family.
    | "scatter3d" | "surface"
    // The points, the least-squares line through them, and its R-squared.
    | "regression";
  name?: string;
  /** The series to fetch instead of carrying the values. */
  dataSource?: ChartDataSource;
  y?: Array<number | string | null | [number, number] | [number, number, number] | [number, number, number, number]>;
  /** The same values under the name a series uses. An alias for `y`. */
  data?: Array<number | string | null | [number, number] | [number, number, number] | [number, number, number, number]>;
  x?: Array<number | string | null>;
  values?: Array<number | null>;
  lower?: Array<number | null>;
  upper?: Array<number | null>;
  sizes?: Array<number | null>;
  /** The third dimension (a 3D mark's z). */
  z?: Array<number | null>;
  matrix?: number[][];
  labels?: string[];
  rowLabels?: string[];
  colLabels?: string[];
  /** A hierarchy's parent labels (treemap/sunburst: "" is the root). */
  parents?: string[];
  /** Per-item colours (a treemap's tiles, a sunburst's branches). */
  colors?: Array<string | null>;
  /** A sankey's links: source index, target index, weight. */
  sources?: number[];
  targets?: number[];
  weights?: number[];
  color?: string;
  alpha?: number;
  width?: number;
  style?: "solid" | "dashed" | "dotted";
  fill?: boolean;
  label?: string;
  yAxis?: "left" | "right";
  stack?: string;
  bins?: number;
}

/** One panel of a scene: its own axes, its own marks. */
export interface ChartPanel {
  title?: string;
  xLabel?: string;
  yLabel?: string;
  marks: ChartMark[];
  valueFormat?: "percent" | "currency" | "compact" | "r" | "number";
  logScale?: boolean;
  height?: number;
}

export interface ChartSpec {
  kind: ChartKind;
  title?: string;
  subtitle?: string;
  /** A gauge's reading. */
  value?: number;
  /** A gauge's scale (100 by default). */
  gaugeMax?: number;
  /** A treemap's tiles. */
  tiles?: ChartTile[];
  footnote?: string;
  source?: string;
  xLabel?: string;
  yLabel?: string;
  categories?: string[];
  series?: ChartSeries[];
  labels?: string[];
  values?: number[];
  /** Per-slice colours for a pie/donut, in the same order as `values`. */
  colors?: Array<string | null>;
  points?: ChartPoint[];
  matrix?: number[][];
  rowLabels?: string[];
  colLabels?: string[];
  candles?: ChartCandle[];
  stacked?: boolean;
  normalized?: boolean;
  smooth?: boolean;
  showPoints?: boolean;
  theme?: ChartTheme;
  /** How the value axis is written (the engine draws numbers). */
  valueFormat?: "percent" | "currency" | "compact" | "r" | "number";
  /** The currency symbol used by the `currency` value format. */
  currencySymbol?: string;
  /** Named horizontal lines drawn across the chart (an entry, a stop). */
  referenceLines?: ChartReferenceLine[];
  /** Shaded spans of the category axis (a recession, a regime). */
  highlights?: Array<{ start: number; end: number; color?: string; label?: string }>;
  /** A compositional chart: panels of marks (any chart). */
  panels?: ChartPanel[];
  /** An optional volume pane under the chart (a price chart's volume bars). */
  volumeData?: Array<number | null>;
  /** The volume bars' colours (one per bar). */
  volumeColors?: Array<string | null>;
  /** Draw the value axis on a log scale. */
  logScale?: boolean;
  width?: number;
  height?: number;
}

/** Where each category sits on the picture, so the browser can hover it. */
export interface ChartInteraction {
  width: number;
  height: number;
  plot: { left: number; right: number; top: number; bottom: number };
  columns: Array<{
    label: string;
    x: number;
    values: Array<{ name: string; value: number; color: string }>;
  }>;
}

export interface ChartFigure {
  kind: ChartKind;
  label: string;
  /** Always false: a picture is a computed artifact, never evidence. */
  isEvidence: false;
  artifact: string;
  artifactId?: string | null;
  sha256: string;
  bytes: number;
  width: number;
  height: number;
  theme: ChartTheme;
  title: string | null;
  series: Array<{ name: string; points: number }>;
  summary: string;
  /** Present when the picture can be hovered (a category axis). */
  interaction?: ChartInteraction;
}

/** A live chart: the Plotly figure the engine authored. */
export interface ChartFigureData {
  data: Array<Record<string, unknown>>;
  layout: Record<string, unknown>;
}

/** A drawn picture carried inline when the Artifact Center could not record it. */
export interface RenderedChartImage {
  mimeType: string;
  /** Base64 PNG bytes. */
  data: string;
}

export interface RenderedChart {
  figure: ChartFigure;
  /** The LIVE chart (vector, zoomable, pannable). Absent when plotly is off. */
  chart?: ChartFigureData;
  artifact: {
    id: string | null;
    name: string;
    bytes: number;
    sha256: string;
    downloadUrl: string | null;
  };
  /**
   * Present only when the picture was drawn but the Artifact Center could not
   * store it. The chart is still real: render this instead of an error box.
   */
  image?: RenderedChartImage;
}

/**
 * The URL to display for a rendered chart.
 *
 * Prefers the recorded artifact (durable, versioned, downloadable); falls back
 * to the inline bytes when registration was unavailable. `null` means the
 * engine drew no picture at all — which is the only case the UI may report as
 * a failed chart.
 */
export function chartImageSrc(rendered: RenderedChart): string | null {
  const id = rendered.artifact.id;
  if (id) return `/api/artifacts/${encodeURIComponent(id)}/download`;
  if (rendered.image?.data) {
    const mime = rendered.image.mimeType || "image/png";
    return `data:${mime};base64,${rendered.image.data}`;
  }
  return null;
}

/**
 * A chart refusal that keeps the runtime's stable CODE, not just its prose.
 *
 * The code is what lets the UI distinguish "the engine cannot draw here"
 * (`CHART_RENDER_UNAVAILABLE`, a repair) from "this spec is invalid" (the
 * model's mistake) instead of showing every failure as the same italic line.
 */
export class ChartRenderError extends Error {
  readonly code?: string;

  constructor(message: string, code?: string) {
    super(message);
    this.name = "ChartRenderError";
    this.code = code;
  }
}

/**
 * A chart failure that fixes itself: the Python runtime is booting (or
 * restarting after a source change) and will answer once it is up. The chart
 * surface RETRIES these with a calm "starting" state; it never shows them as an
 * error box asking the user to run an install command for something that is
 * already installed.
 */
export const RETRYABLE_CHART_ERROR_CODES: ReadonlySet<string> = new Set([
  "RUNTIME_STARTING",
  "RUNTIME_UNAVAILABLE",
]);

export function isRetryableChartError(err: unknown): boolean {
  return (
    err instanceof ChartRenderError &&
    typeof err.code === "string" &&
    RETRYABLE_CHART_ERROR_CODES.has(err.code)
  );
}

/**
 * Draw a chart. The request is a SPEC — the runtime validates it and refuses
 * a malformed one with the missing part named.
 */
export async function renderChart(spec: ChartSpec): Promise<RenderedChart> {
  let res: Response;
  try {
    res = await fetch("/api/harness/charts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(spec),
    });
  } catch {
    // The connection itself dropped — exactly what a runtime restart looks
    // like from the page. Retryable, not a broken chart.
    throw new ChartRenderError("the chart engine did not answer — retrying", "RUNTIME_UNAVAILABLE");
  }
  const body = (await res.json().catch(() => ({}))) as Partial<RenderedChart> & {
    chart?: ChartFigureData;
    image?: RenderedChartImage;
    status?: string;
    error?: string;
    detail?: { code?: string; message?: string } | string;
  };
  if (!res.ok) {
    const detail = body.detail;
    const code =
      typeof detail === "object" && detail && "code" in detail
        ? String(detail.code)
        : body.status === "STARTING" || res.status === 503 || res.status >= 500
          ? // A 503/5xx without a named code is infrastructure (a booting or
            // restarting runtime), not a verdict about this chart.
            "RUNTIME_UNAVAILABLE"
          : undefined;
    const message =
      typeof detail === "object" && detail && "message" in detail
        ? String(detail.message)
        : typeof detail === "string"
          ? detail
          : body.error ?? `the chart could not be drawn (${res.status})`;
    throw new ChartRenderError(message, code);
  }
  if (!body.figure) throw new ChartRenderError("the chart engine returned no figure", "CHART_RENDER_FAILED");
  return {
    figure: body.figure,
    ...(body.chart ? { chart: body.chart } : {}),
    ...(body.image ? { image: body.image } : {}),
    artifact: body.artifact ?? {
      id: null,
      name: body.figure.artifact,
      bytes: body.figure.bytes,
      sha256: body.figure.sha256,
      downloadUrl: null,
    },
  };
}
