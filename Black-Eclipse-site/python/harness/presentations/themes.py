"""Deck theme tokens -- the one palette/type source for both renderers.

Python is canonical because the exporter needs the tokens without a browser.
`GET /presentations/themes` exposes them; `src/lib/presentation/theme.ts` keeps a
typed fallback mirror, pinned by `src/lib/presentation/theme.test.ts`.
"""

from __future__ import annotations

from typing import Any

from .model import ThemePreset


THEMES: dict[str, dict[str, Any]] = {
    ThemePreset.ECLIPSE.value: {
        "background": "#0B0F1A",
        "surface": "#141B2D",
        "surface_alt": "#1B2438",
        "text": "#F2F5FF",
        "muted": "#9AA6C4",
        "accent": "#7C9CFF",
        "accent_text": "#0B0F1A",
        "border": "#2A3550",
        "positive": "#4ADE80",
        "negative": "#FB7185",
        "font_heading": "Segoe UI",
        "font_body": "Segoe UI",
        "chart_palette": ["#7C9CFF", "#4ADE80", "#FBBF24", "#FB7185", "#A78BFA", "#22D3EE"],
    },
    ThemePreset.PAPER.value: {
        "background": "#FBFAF7",
        "surface": "#F1EEE7",
        "surface_alt": "#E7E2D8",
        "text": "#1A1A1A",
        "muted": "#5A5A5A",
        "accent": "#1F4FD8",
        "accent_text": "#FFFFFF",
        "border": "#D5CFC4",
        "positive": "#15803D",
        "negative": "#B91C1C",
        "font_heading": "Georgia",
        "font_body": "Segoe UI",
        "chart_palette": ["#1F4FD8", "#15803D", "#B45309", "#B91C1C", "#6D28D9", "#0E7490"],
    },
    ThemePreset.MIDNIGHT.value: {
        "background": "#05080D",
        "surface": "#0E141F",
        "surface_alt": "#151E2C",
        "text": "#EAF4F1",
        "muted": "#8FA3A0",
        "accent": "#00D4A0",
        "accent_text": "#04120E",
        "border": "#1E2C33",
        "positive": "#34D399",
        "negative": "#F87171",
        "font_heading": "Segoe UI",
        "font_body": "Segoe UI",
        "chart_palette": ["#00D4A0", "#38BDF8", "#FACC15", "#F87171", "#C084FC", "#2DD4BF"],
    },
    ThemePreset.SLATE.value: {
        "background": "#14181F",
        "surface": "#1D232D",
        "surface_alt": "#262E3B",
        "text": "#F1F4F8",
        "muted": "#A0ACBC",
        "accent": "#F59E0B",
        "accent_text": "#14181F",
        "border": "#333C4B",
        "positive": "#84CC16",
        "negative": "#F43F5E",
        "font_heading": "Segoe UI",
        "font_body": "Segoe UI",
        "chart_palette": ["#F59E0B", "#60A5FA", "#84CC16", "#F43F5E", "#A78BFA", "#22D3EE"],
    },
}

#: Type scale in points for the 13.333 x 7.5in canvas. The fit engine may step a
#: role DOWN; it never steps above these.
TYPE_SCALE: dict[str, int] = {
    "title": 34,
    "section": 32,
    "eyebrow": 12,
    "subtitle": 18,
    "body": 18,
    "bullet": 18,
    "metric_value": 32,
    "metric_label": 12,
    "metric_note": 11,
    "statement": 30,
    "quote": 28,
    "attribution": 14,
    "table": 13,
    "caption": 10,
    "source": 10,
    "page": 10,
    "chart_title": 14,
    "label": 12,
}


def theme_tokens(preset: str | ThemePreset, accent: str | None = None) -> dict[str, Any]:
    key = preset.value if isinstance(preset, ThemePreset) else str(preset or "eclipse")
    tokens = dict(THEMES.get(key) or THEMES[ThemePreset.ECLIPSE.value])
    if accent:
        tokens["accent"] = accent
    tokens["preset"] = key
    tokens["type"] = dict(TYPE_SCALE)
    return tokens


def all_themes() -> list[dict[str, Any]]:
    return [theme_tokens(name) for name in THEMES]


__all__ = ["THEMES", "TYPE_SCALE", "all_themes", "theme_tokens"]
