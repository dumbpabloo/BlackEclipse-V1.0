// ── Slide layout (the browser mirror of presentations/layout.py)
//
// Regions are fractions of the slide so 16:9 and 4:3 share one design. The
// exporter computes the same numbers from Python; `layout.test.ts` compares this
// module against the committed golden model.

import { fitBullets, fitText } from './fit';
import type { Slide, SlideLayout } from './schema';
import { TYPE_SCALE } from './theme';

export interface Region { x: number; y: number; w: number; h: number }

type RegionSpec = [number, number, number, number];

export const REGIONS: Record<string, Record<string, RegionSpec>> = {
  title: {
    eyebrow: [0.08, 0.10, 0.84, 0.06], title: [0.08, 0.26, 0.84, 0.30],
    subtitle: [0.08, 0.60, 0.84, 0.12], footer: [0.08, 0.92, 0.84, 0.05],
  },
  section: {
    eyebrow: [0.08, 0.18, 0.84, 0.06], title: [0.08, 0.28, 0.84, 0.26],
    subtitle: [0.08, 0.58, 0.84, 0.10], footer: [0.08, 0.92, 0.84, 0.05],
  },
  agenda: {
    title: [0.06, 0.07, 0.88, 0.14], body: [0.08, 0.26, 0.84, 0.60], source: [0.06, 0.92, 0.88, 0.05],
  },
  bullets: {
    title: [0.06, 0.07, 0.88, 0.14], body: [0.08, 0.26, 0.84, 0.60], source: [0.06, 0.92, 0.88, 0.05],
  },
  two_column: {
    title: [0.06, 0.07, 0.88, 0.12], left: [0.06, 0.23, 0.42, 0.64], right: [0.52, 0.23, 0.42, 0.64],
    source: [0.06, 0.92, 0.88, 0.05],
  },
  statement: {
    statement: [0.10, 0.20, 0.80, 0.48], attribution: [0.10, 0.72, 0.80, 0.08], footer: [0.08, 0.92, 0.84, 0.05],
  },
  quote: {
    statement: [0.10, 0.18, 0.80, 0.50], attribution: [0.10, 0.72, 0.80, 0.08], footer: [0.08, 0.92, 0.84, 0.05],
  },
  metric_row: {
    title: [0.06, 0.07, 0.88, 0.13], metrics: [0.06, 0.24, 0.88, 0.54], source: [0.06, 0.92, 0.88, 0.05],
  },
  table: {
    title: [0.06, 0.07, 0.88, 0.12], table: [0.06, 0.23, 0.88, 0.63], source: [0.06, 0.92, 0.88, 0.05],
  },
  chart: {
    title: [0.06, 0.07, 0.88, 0.11], chart: [0.06, 0.22, 0.88, 0.64], source: [0.06, 0.92, 0.88, 0.05],
  },
  image_full: {
    title: [0.05, 0.05, 0.90, 0.10], image: [0.05, 0.17, 0.90, 0.72], source: [0.05, 0.92, 0.90, 0.05],
  },
  image_right: {
    title: [0.06, 0.07, 0.88, 0.12], left: [0.06, 0.23, 0.42, 0.63], image: [0.52, 0.21, 0.42, 0.66],
    source: [0.06, 0.92, 0.88, 0.05],
  },
  closing: {
    title: [0.08, 0.32, 0.84, 0.26], subtitle: [0.08, 0.60, 0.84, 0.10], footer: [0.08, 0.92, 0.84, 0.05],
  },
};

export const SLIDE_SIZES: Record<string, [number, number]> = {
  '16:9': [13.333, 7.5],
  '4:3': [10.0, 7.5],
};

export function slideSize(size: string): [number, number] {
  return SLIDE_SIZES[size] ?? SLIDE_SIZES['16:9'];
}

function rect(box: RegionSpec, widthIn: number, heightIn: number): Region {
  const [x, y, w, h] = box;
  const round = (value: number) => Math.round(value * 10000) / 10000;
  return { x: round(x * widthIn), y: round(y * heightIn), w: round(w * widthIn), h: round(h * heightIn) };
}

function tableFit(rows: number, heightIn: number): { pt: number; fits: boolean } {
  for (const size of [13, 12, 11, 10]) {
    if (rows * size * 1.28 / 72 + 0.15 <= heightIn) return { pt: size, fits: true };
  }
  return { pt: 10, fits: false };
}

export interface LayoutModel {
  regions: Record<string, Region>;
  fonts: Record<string, number>;
  align: Record<string, string>;
  warnings: string[];
}

export function layoutSlide(slide: Slide, size = '16:9'): LayoutModel {
  const [widthIn, heightIn] = slideSize(size);
  const spec = REGIONS[slide.layout] ?? REGIONS.bullets;
  const regions: Record<string, Region> = {};
  for (const [name, box] of Object.entries(spec)) regions[name] = rect(box, widthIn, heightIn);
  const fonts: Record<string, number> = {};
  const align: Record<string, string> = {};
  const warnings: string[] = [];
  const add = (code: string) => { if (!warnings.includes(code)) warnings.push(code); };

  if (regions.title) {
    const fit = fitText(slide.title ?? '', regions.title.w, regions.title.h, TYPE_SCALE.title,
      { minPt: 20, bold: true });
    fonts.title = fit.pt;
    if (slide.title && !fit.fits) add('overflow:title');
    align.title = 'left';
  }
  if (regions.subtitle && slide.subtitle) {
    fonts.subtitle = fitText(slide.subtitle, regions.subtitle.w, regions.subtitle.h,
      TYPE_SCALE.subtitle, { minPt: 12 }).pt;
    align.subtitle = slide.layout === 'title' || slide.layout === 'closing' ? 'center' : 'left';
  }
  if (regions.eyebrow) { fonts.eyebrow = TYPE_SCALE.eyebrow; align.eyebrow = 'left'; }
  if (regions.body) {
    let items = slide.bullets;
    if (slide.layout === 'bullets' && slide.body) items = items.length ? items : [slide.body];
    const fit = fitBullets(items.length ? items : [''], regions.body.w - 0.35, regions.body.h,
      TYPE_SCALE.bullet);
    fonts.body = fit.pt;
    if (!fit.fits) add('overflow:body');
    align.body = 'left';
  }
  if (regions.left) {
    const column = slide.left;
    let items = column?.bullets ?? [];
    if (slide.layout === 'image_right' && slide.body) items = [slide.body];
    const fit = fitBullets(items.length ? items : [''], regions.left.w - 0.35,
      regions.left.h - 0.4, TYPE_SCALE.bullet);
    fonts.left = fit.pt;
    if (!fit.fits) add('overflow:left');
    align.left = 'left';
  }
  if (regions.right) {
    const items = slide.right?.bullets ?? [];
    const fit = fitBullets(items.length ? items : [''], regions.right.w - 0.35,
      regions.right.h - 0.4, TYPE_SCALE.bullet);
    fonts.right = fit.pt;
    if (!fit.fits) add('overflow:right');
    align.right = 'left';
  }
  if (regions.statement) {
    const text = slide.statement || slide.title || '';
    const base = slide.layout === 'quote' ? TYPE_SCALE.quote : TYPE_SCALE.statement;
    const fit = fitText(text, regions.statement.w, regions.statement.h, base,
      { minPt: 16, bold: slide.layout === 'statement' });
    fonts.statement = fit.pt;
    if (!fit.fits) add('overflow:statement');
    align.statement = 'left';
  }
  if (regions.attribution) { fonts.attribution = TYPE_SCALE.attribution; align.attribution = 'right'; }
  if (regions.metrics) {
    const count = Math.max(1, slide.metrics.length);
    const cardW = (regions.metrics.w - 0.25 * (count - 1)) / count;
    fonts.metric_value = fitText(
      slide.metrics.map((metric) => metric.value).join(' ') || '', cardW - 0.3, 0.9,
      TYPE_SCALE.metric_value, { minPt: 18, bold: true },
    ).pt;
    fonts.metric_label = TYPE_SCALE.metric_label;
    fonts.metric_note = TYPE_SCALE.metric_note;
  }
  if (regions.table && slide.table) {
    const fit = tableFit(slide.table.rows.length + 1, regions.table.h);
    fonts.table = fit.pt;
    if (!fit.fits) add('overflow:table');
  }
  if (regions.chart) fonts.chart_title = TYPE_SCALE.chart_title;
  if (regions.source) fonts.source = TYPE_SCALE.source;
  if (regions.footer) fonts.footer = TYPE_SCALE.page;

  return { regions, fonts, align, warnings };
}

export function layoutOf(layout: string): keyof typeof REGIONS {
  return (layout in REGIONS ? layout : 'bullets') as keyof typeof REGIONS;
}

export type { SlideLayout };
