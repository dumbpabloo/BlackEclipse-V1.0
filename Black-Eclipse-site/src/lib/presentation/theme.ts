// ── Deck theme tokens (the browser mirror of python/harness/presentations/themes.py)
//
// Python is canonical for export. This fallback renders instantly and offline;
// `GET /presentations/themes` can replace it at runtime. `theme.test.ts` pins the
// two palettes together.

import type { DeckTheme } from './schema';

export const THEME_PRESETS = ['eclipse', 'paper', 'midnight', 'slate'] as const;
export type ThemePreset = (typeof THEME_PRESETS)[number];

export interface ThemeTokens {
  preset: string;
  background: string;
  surface: string;
  surfaceAlt: string;
  text: string;
  muted: string;
  accent: string;
  accentText: string;
  border: string;
  positive: string;
  negative: string;
  fontHeading: string;
  fontBody: string;
  chartPalette: string[];
}

const THEMES: Record<ThemePreset, Omit<ThemeTokens, 'preset'>> = {
  eclipse: {
    background: '#0B0F1A', surface: '#141B2D', surfaceAlt: '#1B2438', text: '#F2F5FF',
    muted: '#9AA6C4', accent: '#7C9CFF', accentText: '#0B0F1A', border: '#2A3550',
    positive: '#4ADE80', negative: '#FB7185', fontHeading: 'Segoe UI', fontBody: 'Segoe UI',
    chartPalette: ['#7C9CFF', '#4ADE80', '#FBBF24', '#FB7185', '#A78BFA', '#22D3EE'],
  },
  paper: {
    background: '#FBFAF7', surface: '#F1EEE7', surfaceAlt: '#E7E2D8', text: '#1A1A1A',
    muted: '#5A5A5A', accent: '#1F4FD8', accentText: '#FFFFFF', border: '#D5CFC4',
    positive: '#15803D', negative: '#B91C1C', fontHeading: 'Georgia', fontBody: 'Segoe UI',
    chartPalette: ['#1F4FD8', '#15803D', '#B45309', '#B91C1C', '#6D28D9', '#0E7490'],
  },
  midnight: {
    background: '#05080D', surface: '#0E141F', surfaceAlt: '#151E2C', text: '#EAF4F1',
    muted: '#8FA3A0', accent: '#00D4A0', accentText: '#04120E', border: '#1E2C33',
    positive: '#34D399', negative: '#F87171', fontHeading: 'Segoe UI', fontBody: 'Segoe UI',
    chartPalette: ['#00D4A0', '#38BDF8', '#FACC15', '#F87171', '#C084FC', '#2DD4BF'],
  },
  slate: {
    background: '#14181F', surface: '#1D232D', surfaceAlt: '#262E3B', text: '#F1F4F8',
    muted: '#A0ACBC', accent: '#F59E0B', accentText: '#14181F', border: '#333C4B',
    positive: '#84CC16', negative: '#F43F5E', fontHeading: 'Segoe UI', fontBody: 'Segoe UI',
    chartPalette: ['#F59E0B', '#60A5FA', '#84CC16', '#F43F5E', '#A78BFA', '#22D3EE'],
  },
};

export const TYPE_SCALE: Record<string, number> = {
  title: 34, section: 32, eyebrow: 12, subtitle: 18, body: 18, bullet: 18,
  metric_value: 32, metric_label: 12, metric_note: 11, statement: 30, quote: 28,
  attribution: 14, table: 13, caption: 10, source: 10, page: 10, chart_title: 14, label: 12,
};

export function themeTokens(theme?: DeckTheme | null): ThemeTokens {
  const preset = (theme?.preset ?? 'eclipse') as ThemePreset;
  const base = THEMES[preset] ?? THEMES.eclipse;
  return {
    ...base,
    accent: theme?.accent ?? base.accent,
    preset,
  };
}

export function isDarkTheme(tokens: ThemeTokens): boolean {
  const value = tokens.background.replace('#', '');
  const r = parseInt(value.slice(0, 2), 16);
  const g = parseInt(value.slice(2, 4), 16);
  const b = parseInt(value.slice(4, 6), 16);
  return 0.299 * r + 0.587 * g + 0.114 * b < 140;
}

/** CSS custom properties for a themed subtree. */
export function themeCssVars(tokens: ThemeTokens): Record<string, string> {
  return {
    '--deck-bg': tokens.background,
    '--deck-surface': tokens.surface,
    '--deck-surface-alt': tokens.surfaceAlt,
    '--deck-text': tokens.text,
    '--deck-muted': tokens.muted,
    '--deck-accent': tokens.accent,
    '--deck-accent-text': tokens.accentText,
    '--deck-border': tokens.border,
    '--deck-positive': tokens.positive,
    '--deck-negative': tokens.negative,
  };
}
