// ── Deterministic text fitting (the browser mirror of presentations/fit.py)
//
// Preview and export must agree, so both use this arithmetic rather than DOM
// measurement. It is conservative by design; a slide that cannot fit is
// reported, never clipped.

export const FIT_LADDER = [34, 30, 28, 26, 24, 22, 20, 19, 18, 17, 16, 15, 14, 13, 12, 11, 10] as const;
export const GLYPH_WIDTH = 0.55;
export const GLYPH_WIDTH_BOLD = 0.6;
export const LINE_HEIGHT = 1.28;
const PX_PER_INCH = 96;
const PX_PER_PT = PX_PER_INCH / 72;

export function textWidthIn(text: string, pt: number, bold = false): number {
  const factor = bold ? GLYPH_WIDTH_BOLD : GLYPH_WIDTH;
  return (text?.length ?? 0) * pt * PX_PER_PT * factor / PX_PER_INCH;
}

export function wrapLines(text: string, widthIn: number, pt: number, bold = false): number {
  if (!text) return 1;
  const available = Math.max(widthIn, 0.1);
  let lines = 0;
  for (const paragraph of String(text).split('\n')) {
    const words = paragraph.split(/\s+/).filter(Boolean);
    if (words.length === 0) { lines += 1; continue; }
    let current = '';
    let count = 1;
    for (const word of words) {
      const candidate = current ? `${current} ${word}` : word;
      if (current && textWidthIn(candidate, pt, bold) > available) {
        count += 1;
        current = word;
      } else {
        current = candidate;
      }
    }
    lines += count;
  }
  return Math.max(lines, 1);
}

export function blockHeightIn(text: string, widthIn: number, pt: number, bold = false): number {
  return wrapLines(text, widthIn, pt, bold) * pt * LINE_HEIGHT / 72;
}

export function fitText(
  text: string,
  widthIn: number,
  heightIn: number,
  basePt: number,
  options: { minPt?: number; bold?: boolean } = {},
): { pt: number; fits: boolean } {
  const minPt = options.minPt ?? 10;
  const ladder = FIT_LADDER.filter((size) => size <= basePt);
  const candidates = ladder.length ? ladder : [Math.round(minPt)];
  for (const size of candidates) {
    if (size < minPt) break;
    if (blockHeightIn(text, widthIn, size, options.bold) <= heightIn) {
      return { pt: size, fits: true };
    }
  }
  return { pt: Math.max(Math.round(minPt), Math.min(...candidates)), fits: false };
}

export function fitBullets(
  items: string[],
  widthIn: number,
  heightIn: number,
  basePt: number,
  options: { minPt?: number; gapPt?: number } = {},
): { pt: number; fits: boolean } {
  const minPt = options.minPt ?? 12;
  const gapPt = options.gapPt ?? 8;
  const ladder = FIT_LADDER.filter((size) => size <= basePt);
  const candidates = ladder.length ? ladder : [Math.round(minPt)];
  for (const size of candidates) {
    if (size < minPt) break;
    let total = 0;
    for (const item of items.length ? items : ['']) {
      total += blockHeightIn(item, widthIn, size);
      total += gapPt / 72;
    }
    if (total <= heightIn) return { pt: size, fits: true };
  }
  return { pt: Math.max(Math.round(minPt), Math.min(...candidates)), fits: false };
}
