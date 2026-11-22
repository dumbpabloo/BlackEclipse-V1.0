import { describe, expect, it } from 'vitest';

import { languageFor, tokenizeLine } from './highlight';

describe('languageFor', () => {
  it('maps the common coding extensions', () => {
    expect(languageFor('src/a.tsx')).toBe('ts');
    expect(languageFor('x.py')).toBe('py');
    expect(languageFor('main.rs')).toBe('rust');
    expect(languageFor('unknown.zzz')).toBe('plain');
    expect(languageFor('Dockerfile')).toBe('plain');
  });
});

describe('tokenizeLine', () => {
  it('marks a keyword, a string and a number without changing the text', () => {
    const tokens = tokenizeLine('const name = "eclipse";', 'ts');
    expect(tokens.map((t) => t.kind)).toContain('keyword');
    expect(tokens.map((t) => t.kind)).toContain('string');
    expect(tokens.map((t) => t.text).join('')).toBe('const name = "eclipse";');
  });

  it('treats a hash comment as comment in Python', () => {
    const tokens = tokenizeLine('# explain "this"', 'py');
    expect(tokens).toEqual([{ text: '# explain "this"', kind: 'comment' }]);
  });

  it('does not mistake a CSS hex colour for a comment', () => {
    const tokens = tokenizeLine('color: #ff00aa;', 'css');
    expect(tokens.some((t) => t.kind === 'literal')).toBe(true);
    expect(tokens.some((t) => t.kind === 'comment')).toBe(false);
  });

  it('leaves plain text untouched', () => {
    expect(tokenizeLine('anything at all', 'plain')).toEqual([{ text: 'anything at all', kind: 'plain' }]);
  });

  it('keeps the source text exactly (round-trip property)', () => {
    const sample = 'if (x === 10) { return `value ${x}` } // done';
    expect(tokenizeLine(sample, 'ts').map((t) => t.text).join('')).toBe(sample);
  });
});
