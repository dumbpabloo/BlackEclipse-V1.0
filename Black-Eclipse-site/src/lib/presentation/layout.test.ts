import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { layoutSlide, slideSize } from './layout';
import { parseDeck } from './schema';

const read = (name: string) => JSON.parse(readFileSync(join(__dirname, '__fixtures__', name), 'utf8'));

interface Golden {
  size: { widthIn: number; heightIn: number };
  slides: Array<{
    id: string; layout: string;
    regions: Record<string, { x: number; y: number; w: number; h: number }>;
    fonts: Record<string, number>;
    align: Record<string, string>;
    warnings: string[];
  }>;
}

describe('deck layout parity with Python', () => {
  const deck = parseDeck(read('full.deck.json'));
  const golden = read('golden-layout.json') as Golden;

  it('computes the same slide size', () => {
    expect(slideSize(deck.size)).toEqual([golden.size.widthIn, golden.size.heightIn]);
  });

  it('matches every region, font size, alignment and warning', () => {
    expect(deck.slides).toHaveLength(golden.slides.length);
    deck.slides.forEach((slide, index) => {
      const expected = golden.slides[index];
      const actual = layoutSlide(slide, deck.size);
      expect({ id: slide.id, layout: slide.layout }).toEqual({ id: expected.id, layout: expected.layout });
      expect(actual.regions).toEqual(expected.regions);
      expect(actual.fonts).toEqual(expected.fonts);
      expect(actual.align).toEqual(expected.align);
      expect(actual.warnings).toEqual(expected.warnings);
    });
  });

  it('renders a 4:3 deck from the same fractional design', () => {
    const model = layoutSlide(deck.slides[3], '4:3');
    expect(model.regions.body.x).toBeCloseTo(0.08 * 10, 4);
    expect(model.regions.body.w).toBeCloseTo(0.84 * 10, 4);
  });
});
