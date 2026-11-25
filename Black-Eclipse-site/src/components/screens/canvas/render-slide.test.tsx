import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { renderToStaticMarkup } from 'react-dom/server';
import { parseDeck } from '@/lib/presentation/schema';
import { SlideStage } from './render-slide';

const deck = parseDeck(
  JSON.parse(readFileSync(join(__dirname, '../../../lib/presentation/__fixtures__/full.deck.json'), 'utf8')),
);

function render(index: number): string {
  return renderToStaticMarkup(<SlideStage deck={deck} slide={deck.slides[index]} page={index + 1} />);
}

describe('SlideStage', () => {
  it('renders every layout in the parity deck', () => {
    deck.slides.forEach((slide, index) => {
      expect(() => render(index)).not.toThrow();
      const markup = render(index);
      expect(markup).toContain('--deck-bg');
      expect(markup).toContain('aspect-ratio');
    });
  });

  it('shows the title and subtitle on the title slide', () => {
    const markup = render(0);
    expect(markup).toContain('Parity Deck');
    expect(markup).toContain('Every layout');
  });

  it('shows metric values and a table header', () => {
    expect(render(7)).toContain('$12.4M');
    expect(render(8)).toContain('EV/EBITDA');
  });

  it('labels an unavailable chart instead of drawing one', () => {
    const empty = parseDeck({
      slides: [{ layout: 'chart', title: 'Trend', chart: { kind: 'line' } }],
    });
    const markup = renderToStaticMarkup(<SlideStage deck={empty} slide={empty.slides[0]} />);
    expect(markup).toContain('Unavailable');
    expect(markup).toContain('no chart data');
  });

  it('labels an image artifact that has no id', () => {
    const empty = parseDeck({
      slides: [{ layout: 'image_full', title: 'Photo', image: { alt: 'A photo', fit: 'cover' } }],
    });
    const markup = renderToStaticMarkup(<SlideStage deck={empty} slide={empty.slides[0]} />);
    expect(markup).toContain('Image unavailable');
    expect(markup).toContain('A photo');
  });
});
