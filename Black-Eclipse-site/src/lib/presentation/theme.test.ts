import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { THEME_PRESETS, themeTokens } from './theme';

interface PythonTheme {
  preset: string; background: string; surface: string; surface_alt: string;
  text: string; muted: string; accent: string; accent_text: string; border: string;
  positive: string; negative: string; font_heading: string; font_body: string;
  chart_palette: string[]; type: Record<string, number>;
}

const golden = JSON.parse(
  readFileSync(join(__dirname, '__fixtures__', 'golden-layout.json'), 'utf8'),
) as { themes: Record<string, PythonTheme> };

describe('deck theme parity with Python', () => {
  it('ships the same preset list', () => {
    expect([...THEME_PRESETS].sort()).toEqual(Object.keys(golden.themes).sort());
  });

  it.each([...THEME_PRESETS])('matches the %s palette', (preset) => {
    const py = golden.themes[preset];
    const ts = themeTokens({ preset });
    expect(ts.background).toBe(py.background);
    expect(ts.surface).toBe(py.surface);
    expect(ts.surfaceAlt).toBe(py.surface_alt);
    expect(ts.text).toBe(py.text);
    expect(ts.muted).toBe(py.muted);
    expect(ts.accent).toBe(py.accent);
    expect(ts.accentText).toBe(py.accent_text);
    expect(ts.border).toBe(py.border);
    expect(ts.positive).toBe(py.positive);
    expect(ts.negative).toBe(py.negative);
    expect(ts.fontHeading).toBe(py.font_heading);
    expect(ts.fontBody).toBe(py.font_body);
    expect(ts.chartPalette).toEqual(py.chart_palette);
  });

  it('lets a deck override the accent without changing the preset', () => {
    const tokens = themeTokens({ preset: 'midnight', accent: '#FF0000' });
    expect(tokens.accent).toBe('#FF0000');
    expect(tokens.background).toBe(golden.themes.midnight.background);
  });
});
