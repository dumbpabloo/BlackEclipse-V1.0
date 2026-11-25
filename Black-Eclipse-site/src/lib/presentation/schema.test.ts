import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import {
  DECK_LIMITS, DeckInvalidError, emptyDeck, nextSlideId, parseDeck, serializeDeck,
} from './schema';

const fixture = (name: string) =>
  readFileSync(join(__dirname, '__fixtures__', name), 'utf8');

describe('deck schema', () => {
  it('accepts the shared parity deck and assigns every slide id', () => {
    const deck = parseDeck(fixture('full.deck.json'));
    expect(deck.slides).toHaveLength(13);
    expect(deck.slides[0].id).toBe('s1');
    expect(deck.slides[12].id).toBe('s13');
    expect(deck.meta.title).toBe('Parity Deck');
  });

  it('round-trips through serializeDeck', () => {
    const deck = parseDeck(fixture('full.deck.json'));
    expect(parseDeck(serializeDeck(deck))).toEqual(deck);
  });

  it('refuses empty, duplicate and unknown fields', () => {
    expect(() => parseDeck({ slides: [] })).toThrow(DeckInvalidError);
    expect(() => parseDeck({ slides: [
      { id: 'x', layout: 'bullets', bullets: ['a'] },
      { id: 'x', layout: 'bullets', bullets: ['b'] },
    ] })).toThrow(/duplicate/);
    expect(() => parseDeck({ slides: [{ layout: 'bullets', titel: 'typo' }] })).toThrow(/unrecognized|invalid/i);
    expect(() => parseDeck({ slides: [{ layout: 'nope' }] })).toThrow(DeckInvalidError);
  });

  it('refuses layout content that is missing', () => {
    expect(() => parseDeck({ slides: [{ layout: 'chart', title: 'x' }] })).toThrow(/chart block/);
    expect(() => parseDeck({ slides: [{ layout: 'table', title: 'x' }] })).toThrow(/table block/);
    expect(() => parseDeck({ slides: [{ layout: 'metric_row', title: 'x' }] })).toThrow(/metrics/);
  });

  it('bounds bullets', () => {
    const tooMany = Array.from({ length: DECK_LIMITS.bullets + 1 }, () => 'x');
    expect(() => parseDeck({ slides: [{ layout: 'bullets', bullets: tooMany }] })).toThrow(DeckInvalidError);
    expect(() => parseDeck({
      slides: [{ layout: 'bullets', bullets: ['x'.repeat(DECK_LIMITS.bulletChars + 1)] }],
    })).toThrow(DeckInvalidError);
  });

  it('creates a valid starter deck and a free id', () => {
    const deck = emptyDeck('New deck');
    expect(deck.slides).toHaveLength(1);
    expect(deck.slides[0].layout).toBe('title');
    expect(nextSlideId(deck)).toBe('s2');
  });

  it('reports a readable error for bad JSON', () => {
    expect(() => parseDeck('{nope')).toThrow(/not valid JSON/);
  });
});
