'use client';

// ── The ONE slide renderer ───────────────────────────────────────────────────
// Renders a validated deck slide at any width. Geometry comes from `layout.ts`
// (the same numbers the exporter uses), fonts are sized in container units so a
// 160px thumbnail and a full stage are the SAME slide, and unavailable data is
// labelled instead of drawn.

import type { CSSProperties } from 'react';
import { downloadUrl } from '@/lib/harness/clients/artifacts-browser';
import { layoutSlide, slideSize, type Region } from '@/lib/presentation/layout';
import type { DeckChart, DeckSpec, DeckTable, Slide } from '@/lib/presentation/schema';
import { themeCssVars, themeTokens, type ThemeTokens } from '@/lib/presentation/theme';
import { cn } from '@/lib/utils';

function pct(region: Region, widthIn: number, heightIn: number): CSSProperties {
  return {
    left: `${(region.x / widthIn) * 100}%`,
    top: `${(region.y / heightIn) * 100}%`,
    width: `${(region.w / widthIn) * 100}%`,
    height: `${(region.h / heightIn) * 100}%`,
  };
}

function textStyle(pt: number, widthIn: number, tokens: ThemeTokens, opts: {
  color?: string; bold?: boolean; align?: string; font?: string; lineHeight?: number;
} = {}): CSSProperties {
  const cqw = (pt * 100) / (72 * widthIn);
  return {
    fontSize: `${cqw}cqw`,
    lineHeight: opts.lineHeight ?? 1.28,
    color: opts.color ?? tokens.text,
    fontWeight: opts.bold ? 700 : 400,
    textAlign: (opts.align as CSSProperties['textAlign']) ?? 'left',
    fontFamily: `${opts.font ?? tokens.fontBody}, system-ui, sans-serif`,
  };
}

function MiniChart({ chart, tokens }: { chart: DeckChart & { available?: boolean; unavailableReason?: string }; tokens: ThemeTokens }) {
  const series = chart.series ?? [];
  const categories = chart.categories ?? [];
  const values = series[0]?.data ?? chart.values ?? [];
  const available = chart.available !== false && values.some((value) => typeof value === 'number');
  if (!available) {
    return (
      <div className="flex h-full w-full flex-col items-center justify-center gap-1 rounded border border-dashed"
        style={{ borderColor: tokens.border, background: tokens.surface, color: tokens.muted }}>
        <span className="font-semibold uppercase tracking-wider" style={{ fontSize: '1.6cqw' }}>Unavailable</span>
        <span style={{ fontSize: '1.1cqw' }}>{chart.unavailableReason ?? 'no chart data'}</span>
      </div>
    );
  }
  if (chart.kind === 'pie' || chart.kind === 'donut') {
    const total = values.reduce<number>((sum, value) => sum + (typeof value === 'number' ? Math.abs(value) : 0), 0) || 1;
    let offset = 0;
    const segments = values.map((value, index) => {
      const share = (Math.abs(typeof value === 'number' ? value : 0) / total) * 100;
      const segment = { share, offset, color: tokens.chartPalette[index % tokens.chartPalette.length] };
      offset += share;
      return segment;
    });
    const gradient = segments
      .map((segment) => `${segment.color} ${segment.offset}% ${segment.offset + segment.share}%`)
      .join(', ');
    return (
      <div className="flex h-full w-full items-center gap-[2cqw]">
        <div className="relative aspect-square h-full rounded-full"
          style={{ background: `conic-gradient(${gradient})` }}>
          {chart.kind === 'donut' ? (
            <div className="absolute inset-[22%] rounded-full" style={{ background: tokens.background }} />
          ) : null}
        </div>
        <ul className="flex flex-col gap-[0.5cqw]" style={{ fontSize: '1.1cqw', color: tokens.muted }}>
          {chart.labels.map((label, index) => (
            <li key={`${label}-${index}`} className="flex items-center gap-[0.5cqw]">
              <span className="inline-block h-[0.9cqw] w-[0.9cqw] rounded-sm"
                style={{ background: tokens.chartPalette[index % tokens.chartPalette.length] }} />
              {label}
            </li>
          ))}
        </ul>
      </div>
    );
  }

  const width = 100;
  const height = 60;
  const pad = 4;
  const numbers = values.map((value) => (typeof value === 'number' ? value : 0));
  const min = Math.min(...numbers, 0);
  const max = Math.max(...numbers, 1);
  const span = max - min || 1;
  const x = (index: number) => pad + (index / Math.max(numbers.length - 1, 1)) * (width - pad * 2);
  const y = (value: number) => height - pad - ((value - min) / span) * (height - pad * 2);
  const points = numbers.map((value, index) => `${x(index)},${y(value)}`).join(' ');
  const color = series[0]?.color ?? tokens.accent;

  return (
    <div className="flex h-full w-full flex-col">
      <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" className="min-h-0 flex-1">
        {chart.kind === 'area' ? (
          <polygon points={`${pad},${height - pad} ${points} ${width - pad},${height - pad}`}
            fill={color} opacity={0.22} />
        ) : null}
        {chart.kind === 'bar' || chart.kind === 'stackedBar' ? (
          numbers.map((value, index) => {
            const barWidth = (width - pad * 2) / Math.max(numbers.length, 1) * 0.55;
            const zero = y(0);
            return (
              <rect key={index} x={x(index) - barWidth / 2} y={Math.min(y(value), zero)}
                width={barWidth} height={Math.abs(zero - y(value))} fill={color} rx={0.4} />
            );
          })
        ) : (
          <>
            <polyline points={points} fill="none" stroke={color} strokeWidth={0.7}
              vectorEffect="non-scaling-stroke" strokeLinejoin="round" />
            {numbers.map((value, index) => (
              <circle key={index} cx={x(index)} cy={y(value)} r={0.9} fill={color} />
            ))}
          </>
        )}
        <line x1={pad} y1={height - pad} x2={width - pad} y2={height - pad}
          stroke={tokens.border} strokeWidth={0.3} vectorEffect="non-scaling-stroke" />
      </svg>
      <div className="flex justify-between" style={{ fontSize: '0.9cqw', color: tokens.muted }}>
        <span>{categories[0] ?? ''}</span>
        <span>{categories[categories.length - 1] ?? ''}</span>
      </div>
    </div>
  );
}

function SlideTable({ table, tokens, fontSize }: { table: DeckTable; tokens: ThemeTokens; fontSize: number }) {
  return (
    <table className="h-full w-full border-collapse" style={{ fontSize: `${fontSize}cqw`, color: tokens.text }}>
      <thead>
        <tr>
          {table.headers.map((header, index) => (
            <th key={`${header}-${index}`} className="px-[0.8cqw] py-[0.5cqw] text-left"
              style={{ background: tokens.surfaceAlt, color: tokens.text, borderBottom: `1px solid ${tokens.border}` }}>
              {header}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {table.rows.map((row, rowIndex) => (
          <tr key={rowIndex} style={{ background: rowIndex % 2 ? tokens.surface : 'transparent' }}>
            {row.map((cell, cellIndex) => (
              <td key={cellIndex} className="px-[0.8cqw] py-[0.4cqw]" style={{ borderBottom: `1px solid ${tokens.border}` }}>
                {cell}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export interface SlideStageProps {
  deck: DeckSpec;
  slide: Slide;
  page?: number;
  className?: string;
  /** Read-only stages (thumbnails/chat) never capture pointer events. */
  interactive?: boolean;
}

export function SlideStage({ deck, slide, page, className, interactive = false }: SlideStageProps) {
  const tokens = themeTokens(deck.theme);
  const [widthIn, heightIn] = slideSize(deck.size);
  const model = layoutSlide(slide, deck.size);
  const { regions, fonts, align } = model;
  const factor = (pt: number) => textStyle(pt, widthIn, tokens, { font: tokens.fontBody });
  const chart = slide.chart as (DeckChart & { available?: boolean; unavailableReason?: string }) | undefined;

  return (
    <div
      className={cn('relative overflow-hidden', className)}
      style={{
        ...themeCssVars(tokens),
        containerType: 'inline-size',
        aspectRatio: `${widthIn} / ${heightIn}`,
        background: tokens.background,
        color: tokens.text,
        pointerEvents: interactive ? undefined : 'none',
      }}
      aria-label={slide.title ?? slide.layout}
    >
      {regions.eyebrow && (slide.eyebrow || deck.meta.author) ? (
        <div className="absolute flex items-end" style={pct(regions.eyebrow, widthIn, heightIn)}>
          <span style={{ ...factor(fonts.eyebrow ?? 12), color: tokens.accent, fontWeight: 700, letterSpacing: '0.08em', textTransform: 'uppercase' }}>
            {slide.eyebrow ?? deck.meta.author}
          </span>
        </div>
      ) : null}

      {regions.title && slide.title ? (
        <div className="absolute flex overflow-hidden"
          style={{ ...pct(regions.title, widthIn, heightIn), alignItems: slide.layout === 'title' || slide.layout === 'closing' ? 'center' : 'flex-start' }}>
          <span style={{ ...factor(fonts.title ?? 30), fontWeight: 700, fontFamily: `${tokens.fontHeading}, system-ui, sans-serif`, textAlign: (align.title as CSSProperties['textAlign']) }}>
            {slide.title}
          </span>
        </div>
      ) : null}

      {regions.subtitle && slide.subtitle ? (
        <div className="absolute flex items-start" style={pct(regions.subtitle, widthIn, heightIn)}>
          <span style={{ ...factor(fonts.subtitle ?? 18), color: tokens.muted, textAlign: (align.subtitle as CSSProperties['textAlign']) }}>
            {slide.subtitle}
          </span>
        </div>
      ) : null}

      {regions.body ? (
        <div className="absolute" style={pct(regions.body, widthIn, heightIn)}>
          <ul className="flex flex-col gap-[0.5cqw]" style={{ paddingLeft: '1.2cqw' }}>
            {(slide.bullets.length ? slide.bullets : slide.body ? [slide.body] : []).map((item, index) => (
              <li key={index} style={{ ...factor(fonts.body ?? 18), listStyleType: slide.layout === 'agenda' ? 'decimal' : 'disc' }}>
                {item}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {regions.left ? (
        <div className="absolute flex flex-col gap-[0.6cqw]" style={pct(regions.left, widthIn, heightIn)}>
          {slide.left?.heading ? (
            <span style={{ ...factor(13), color: tokens.accent, fontWeight: 700 }}>{slide.left.heading}</span>
          ) : null}
          <ul className="flex flex-col gap-[0.4cqw]" style={{ paddingLeft: '1.2cqw' }}>
            {(slide.left?.bullets?.length ? slide.left.bullets : slide.body && slide.layout === 'image_right' ? [slide.body] : []).map((item, index) => (
              <li key={index} style={{ ...factor(fonts.left ?? 18), listStyleType: 'disc' }}>{item}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {regions.right ? (
        <div className="absolute flex flex-col gap-[0.6cqw]" style={pct(regions.right, widthIn, heightIn)}>
          {slide.right?.heading ? (
            <span style={{ ...factor(13), color: tokens.accent, fontWeight: 700 }}>{slide.right.heading}</span>
          ) : null}
          <ul className="flex flex-col gap-[0.4cqw]" style={{ paddingLeft: '1.2cqw' }}>
            {(slide.right?.bullets ?? []).map((item, index) => (
              <li key={index} style={{ ...factor(fonts.right ?? 18), listStyleType: 'disc' }}>{item}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {regions.statement ? (
        <div className="absolute flex overflow-hidden"
          style={{ ...pct(regions.statement, widthIn, heightIn), alignItems: slide.layout === 'quote' ? 'flex-start' : 'center' }}>
          <span style={{ ...factor(fonts.statement ?? 28), fontWeight: slide.layout === 'statement' ? 700 : 400, fontFamily: `${tokens.fontHeading}, system-ui, sans-serif` }}>
            {slide.statement || slide.title}
          </span>
        </div>
      ) : null}

      {regions.attribution && slide.attribution ? (
        <div className="absolute flex items-end justify-end" style={pct(regions.attribution, widthIn, heightIn)}>
          <span style={{ ...factor(14), color: tokens.muted }}>— {slide.attribution}</span>
        </div>
      ) : null}

      {regions.metrics ? (
        <div className="absolute flex gap-[1.6cqw]" style={pct(regions.metrics, widthIn, heightIn)}>
          {slide.metrics.map((metric, index) => (
            <div key={index} className="flex min-w-0 flex-1 flex-col justify-center rounded-[0.8cqw] px-[1.4cqw]"
              style={{ background: tokens.surface, border: `1px solid ${tokens.border}` }}>
              <span style={{ ...factor(fonts.metric_value ?? 30), fontWeight: 700, fontFamily: `${tokens.fontHeading}, system-ui, sans-serif` }}>
                {metric.value}
              </span>
              <span style={{ ...factor(fonts.metric_label ?? 12), color: tokens.muted, marginTop: '0.8cqw' }}>{metric.label}</span>
              {metric.delta ? (
                <span style={{ ...factor(fonts.metric_note ?? 11), color: tokens.accent, marginTop: '0.4cqw' }}>{metric.delta}</span>
              ) : null}
              {metric.note ? (
                <span style={{ ...factor(fonts.metric_note ?? 11), color: tokens.muted, marginTop: '0.3cqw' }}>{metric.note}</span>
              ) : null}
            </div>
          ))}
        </div>
      ) : null}

      {regions.table && slide.table ? (
        <div className="absolute overflow-hidden" style={pct(regions.table, widthIn, heightIn)}>
          <SlideTable table={slide.table} tokens={tokens} fontSize={fonts.table ?? 12} />
        </div>
      ) : null}

      {regions.chart && chart ? (
        <div className="absolute" style={pct(regions.chart, widthIn, heightIn)}>
          <MiniChart chart={chart} tokens={tokens} />
        </div>
      ) : null}

      {regions.image && slide.image ? (
        <div className="absolute overflow-hidden rounded-[0.6cqw]" style={{ ...pct(regions.image, widthIn, heightIn), background: tokens.surface, border: `1px solid ${tokens.border}` }}>
          {slide.image.artifactId ? (
            <img src={downloadUrl(slide.image.artifactId)} alt={slide.image.alt}
              className="h-full w-full" style={{ objectFit: slide.image.fit === 'contain' ? 'contain' : 'cover' }} />
          ) : (
            <div className="flex h-full flex-col items-center justify-center gap-1" style={{ color: tokens.muted, fontSize: '1.2cqw' }}>
              <span className="font-semibold uppercase tracking-wider" style={{ fontSize: '1.3cqw' }}>Image unavailable</span>
              <span>{slide.image.alt || 'no image artifact'}</span>
            </div>
          )}
        </div>
      ) : null}

      {regions.source && slide.source ? (
        <div className="absolute flex items-end" style={pct(regions.source, widthIn, heightIn)}>
          <span className="truncate" style={{ ...factor(fonts.source ?? 10), color: tokens.muted }}>{slide.source}</span>
        </div>
      ) : null}

      {regions.footer ? (
        <div className="absolute flex items-end justify-between" style={pct(regions.footer, widthIn, heightIn)}>
          <span className="truncate pr-[2cqw]" style={{ ...factor(fonts.footer ?? 10), color: tokens.muted }}>
            {deck.meta.footer ?? ''}
          </span>
          <span style={{ ...factor(fonts.footer ?? 10), color: tokens.muted }}>{page ?? ''}</span>
        </div>
      ) : regions.source ? (
        <div className="absolute flex items-end justify-end" style={pct(regions.source, widthIn, heightIn)}>
          <span style={{ ...factor(fonts.source ?? 10), color: tokens.muted }}>{page ?? ''}</span>
        </div>
      ) : null}

      {model.warnings.some((warning) => warning.startsWith('overflow:')) ? (
        <div className="absolute right-[1cqw] top-[1cqw] rounded px-[0.8cqw] py-[0.3cqw]"
          style={{ background: '#F59E0B', color: '#14181F', fontSize: '1cqw', fontWeight: 700 }}>
          Text overflow
        </div>
      ) : null}
    </div>
  );
}

export function SlideThumbnail({ deck, slide, page }: { deck: DeckSpec; slide: Slide; page: number }) {
  return <SlideStage deck={deck} slide={slide} page={page} className="w-full rounded-md" />;
}
