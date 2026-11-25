// ── Deck schema (the TypeScript mirror of python/harness/presentations/model.py)
//
// A deck is data. The editor, the preview and the exporter all speak this one
// shape. Validation is strict: a typo is a refusal, not a silently dropped
// slide field. Python remains canonical for export; the shared fixtures in
// `src/lib/presentation/__fixtures__/` pin the two validators together.

import { z } from 'zod';

export const DECK_SCHEMA_VERSION = 1;

export const DECK_LIMITS = {
  slides: 150,
  bullets: 7,
  bulletChars: 180,
  bodyChars: 700,
  metrics: 4,
  tableRows: 12,
  tableCols: 8,
  notesChars: 4000,
  chartPoints: 400,
} as const;

export const SLIDE_LAYOUTS = [
  'title', 'section', 'agenda', 'bullets', 'two_column', 'statement', 'quote',
  'metric_row', 'table', 'chart', 'image_full', 'image_right', 'closing',
] as const;
export const DECK_SIZES = ['16:9', '4:3'] as const;
export const THEME_PRESETS = ['eclipse', 'paper', 'midnight', 'slate'] as const;
export const CHART_KINDS = [
  'line', 'area', 'bar', 'stackedBar', 'pie', 'donut', 'scatter',
  'candlestick', 'waterfall', 'histogram', 'heatmap',
] as const;

const hex = z.string().regex(/^#[0-9a-fA-F]{6}$/, 'accent must be #rrggbb');

export const deckThemeSchema = z.object({
  preset: z.enum(THEME_PRESETS).default('eclipse'),
  accent: hex.optional(),
  logoArtifactId: z.string().max(120).optional(),
}).strict();

export const dataSourceSchema = z.object({
  kind: z.literal('price').default('price'),
  symbol: z.string().min(1).max(24),
  range: z.string().max(24).default('1y'),
  interval: z.string().max(12).default('1d'),
}).strict();

export const deckChartSeriesSchema = z.object({
  name: z.string().min(1).max(120),
  data: z.array(z.number().nullable()).max(DECK_LIMITS.chartPoints).default([]),
  color: hex.optional(),
}).strict();

export const deckChartSchema = z.object({
  kind: z.enum(CHART_KINDS).default('line'),
  title: z.string().max(200).optional(),
  categories: z.array(z.string()).max(DECK_LIMITS.chartPoints).default([]),
  series: z.array(deckChartSeriesSchema).default([]),
  labels: z.array(z.string()).max(DECK_LIMITS.chartPoints).default([]),
  values: z.array(z.number()).max(DECK_LIMITS.chartPoints).default([]),
  dataSource: z.string().max(60).optional(),
  valueFormat: z.enum(['percent', 'currency', 'compact', 'number']).optional(),
  unit: z.string().max(24).optional(),
}).strict();

export const deckMetricSchema = z.object({
  label: z.string().min(1).max(80),
  value: z.string().min(1).max(40),
  delta: z.string().max(40).optional(),
  note: z.string().max(160).optional(),
  source: z.string().max(200).optional(),
}).strict();

export const deckTableSchema = z.object({
  headers: z.array(z.string()).max(DECK_LIMITS.tableCols).default([]),
  rows: z.array(z.array(z.string())).max(DECK_LIMITS.tableRows).default([]),
  caption: z.string().max(200).optional(),
}).strict();

export const deckColumnSchema = z.object({
  heading: z.string().max(120).optional(),
  bullets: z.array(z.string()).max(DECK_LIMITS.bullets).default([]),
  body: z.string().max(DECK_LIMITS.bodyChars).optional(),
}).strict();

export const deckImageSchema = z.object({
  artifactId: z.string().max(120).optional(),
  alt: z.string().max(300).default(''),
  fit: z.enum(['cover', 'contain']).default('cover'),
}).strict();

export const slideSchema = z.object({
  id: z.string().max(40).default(''),
  layout: z.enum(SLIDE_LAYOUTS).default('bullets'),
  title: z.string().max(200).optional(),
  subtitle: z.string().max(240).optional(),
  eyebrow: z.string().max(80).optional(),
  bullets: z.array(z.string().max(DECK_LIMITS.bulletChars)).max(DECK_LIMITS.bullets).default([]),
  body: z.string().max(DECK_LIMITS.bodyChars).optional(),
  statement: z.string().max(300).optional(),
  attribution: z.string().max(160).optional(),
  left: deckColumnSchema.optional(),
  right: deckColumnSchema.optional(),
  metrics: z.array(deckMetricSchema).max(DECK_LIMITS.metrics).default([]),
  table: deckTableSchema.optional(),
  chart: deckChartSchema.optional(),
  image: deckImageSchema.optional(),
  notes: z.string().max(DECK_LIMITS.notesChars).optional(),
  source: z.string().max(240).optional(),
}).strict().superRefine((slide, ctx) => {
  const need = (condition: boolean, path: string, message: string) => {
    if (!condition) ctx.addIssue({ code: 'custom', path: [path], message });
  };
  need(slide.layout !== 'two_column' || Boolean(slide.left || slide.right || slide.body),
    'layout', 'a two_column slide needs left/right columns or body text');
  need(slide.layout !== 'statement' || Boolean(slide.statement || slide.title),
    'layout', 'a statement slide needs statement text');
  need(slide.layout !== 'chart' || Boolean(slide.chart), 'chart', 'a chart slide needs a chart block');
  need(slide.layout !== 'table' || Boolean(slide.table), 'table', 'a table slide needs a table block');
  need(slide.layout !== 'metric_row' || slide.metrics.length > 0, 'metrics', 'a metric_row slide needs metrics');
  for (const [index, row] of slide.table?.rows.entries() ?? []) {
    need(!slide.table?.headers.length || row.length <= slide.table.headers.length,
      `table.rows.${index}`, 'a table row has more cells than the header');
  }
});

export const deckMetaSchema = z.object({
  title: z.string().min(1).max(200).default('Untitled deck'),
  subtitle: z.string().max(240).optional(),
  author: z.string().max(120).optional(),
  date: z.string().max(40).optional(),
  footer: z.string().max(240).optional(),
  language: z.string().max(16).default('en'),
}).strict();

export const deckSchema = z.object({
  schemaVersion: z.literal(1).default(1),
  meta: deckMetaSchema.default({ title: 'Untitled deck', language: 'en' }),
  size: z.enum(DECK_SIZES).default('16:9'),
  theme: deckThemeSchema.default({ preset: 'eclipse' }),
  dataSources: z.record(z.string(), dataSourceSchema).default({}),
  slides: z.array(slideSchema).min(1).max(DECK_LIMITS.slides),
}).strict().superRefine((deck, ctx) => {
  const seen = new Set<string>();
  deck.slides.forEach((slide, index) => {
    if (!slide.id) return;
    if (seen.has(slide.id)) {
      ctx.addIssue({ code: 'custom', path: ['slides', index, 'id'], message: `duplicate slide id ${slide.id}` });
    }
    if (!/^[A-Za-z0-9_-]{1,40}$/.test(slide.id)) {
      ctx.addIssue({ code: 'custom', path: ['slides', index, 'id'], message: 'invalid slide id' });
    }
    seen.add(slide.id);
  });
});

export type DeckSpec = z.infer<typeof deckSchema>;
export type Slide = z.infer<typeof slideSchema>;
export type SlideLayout = (typeof SLIDE_LAYOUTS)[number];
export type DeckTheme = z.infer<typeof deckThemeSchema>;
export type DeckChart = z.infer<typeof deckChartSchema>;
export type DeckTable = z.infer<typeof deckTableSchema>;
export type DeckMetric = z.infer<typeof deckMetricSchema>;
export type DeckImage = z.infer<typeof deckImageSchema>;
export type DeckColumn = z.infer<typeof deckColumnSchema>;
export type DeckMeta = z.infer<typeof deckMetaSchema>;

export class DeckInvalidError extends Error {
  issues: string[];
  constructor(issues: string[]) {
    super(`invalid deck: ${issues.slice(0, 3).join('; ')}`);
    this.name = 'DeckInvalidError';
    this.issues = issues;
  }
}

function withSlideIds(deck: DeckSpec): DeckSpec {
  // Mirror Python's model validator: a missing id becomes `s{position}`.
  return {
    ...deck,
    slides: deck.slides.map((slide, index) => (
      slide.id ? slide : { ...slide, id: `s${index + 1}` }
    )),
  };
}

/** Validate a deck. Accepts an object or a JSON string; throws DeckInvalidError. */
export function parseDeck(value: unknown): DeckSpec {
  let raw = value;
  if (typeof value === 'string') {
    try {
      raw = JSON.parse(value);
    } catch (error) {
      throw new DeckInvalidError([`deck content is not valid JSON: ${String(error)}`]);
    }
  }
  const result = deckSchema.safeParse(raw);
  if (!result.success) {
    throw new DeckInvalidError(
      result.error.issues.map((issue) => `${issue.path.join('.') || 'deck'}: ${issue.message}`),
    );
  }
  return withSlideIds(result.data);
}

/** A deck that is guaranteed valid (no ids yet; parseDeck assigns them). */
export function emptyDeck(title = 'Untitled deck'): DeckSpec {
  return parseDeck({
    schemaVersion: 1,
    meta: { title },
    size: '16:9',
    theme: { preset: 'eclipse' },
    dataSources: {},
    slides: [{ layout: 'title', title }],
  });
}

export function nextSlideId(deck: DeckSpec): string {
  const used = new Set(deck.slides.map((slide) => slide.id));
  let index = deck.slides.length + 1;
  while (used.has(`s${index}`)) index += 1;
  return `s${index}`;
}

export function serializeDeck(deck: DeckSpec): string {
  return `${JSON.stringify(deck, null, 2)}\n`;
}

export function deckSlideTitle(slide: Slide): string {
  return slide.title || slide.statement || (slide.layout === 'title' ? slide.subtitle ?? '' : '') || '(untitled)';
}
