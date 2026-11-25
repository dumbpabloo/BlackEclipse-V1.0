"""Presentation (deck) contracts -- the ONE deck schema.

A deck is data, never code and never a file. The model authors a `DeckSpec`;
the app renders it; the exporter turns the same spec into a real `.pptx`. That
is why the schema is closed: a layout is chosen by name, text is bounded, and
there are no absolute coordinates to place content off the slide.

Wire shape is camelCase (`to_camel`), snake_case is accepted on input. Unknown
fields are refused so a typo cannot silently drop a slide field.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

_CAMEL = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

DECK_SCHEMA_VERSION = 1
DECK_MIME = "application/vnd.eclipse.deck+json"

MAX_SLIDES = 150
MAX_BULLETS = 7
MAX_BULLET_CHARS = 180
MAX_BODY_CHARS = 700
MAX_METRICS = 4
MAX_TABLE_ROWS = 12
MAX_TABLE_COLS = 8
MAX_NOTES_CHARS = 4_000
MAX_DECK_BYTES = 4 * 1024 * 1024
MAX_CHART_POINTS = 400


class DeckError(Exception):
    """A deck failure with a stable, machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class SlideLayout(str, Enum):
    TITLE = "title"
    SECTION = "section"
    AGENDA = "agenda"
    BULLETS = "bullets"
    TWO_COLUMN = "two_column"
    STATEMENT = "statement"
    QUOTE = "quote"
    METRIC_ROW = "metric_row"
    TABLE = "table"
    CHART = "chart"
    IMAGE_FULL = "image_full"
    IMAGE_RIGHT = "image_right"
    CLOSING = "closing"


class DeckSize(str, Enum):
    WIDE = "16:9"
    STANDARD = "4:3"


class ThemePreset(str, Enum):
    ECLIPSE = "eclipse"
    PAPER = "paper"
    MIDNIGHT = "midnight"
    SLATE = "slate"


class ImageFit(str, Enum):
    COVER = "cover"
    CONTAIN = "contain"


ChartKind = Literal[
    "line", "area", "bar", "stackedBar", "pie", "donut", "scatter",
    "candlestick", "waterfall", "histogram", "heatmap",
]


class DeckTheme(BaseModel):
    model_config = _CAMEL

    preset: ThemePreset = ThemePreset.ECLIPSE
    accent: Optional[str] = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    logo_artifact_id: Optional[str] = Field(default=None, max_length=120)


class DataSource(BaseModel):
    """A named market series resolved at render time. Never a URL."""

    model_config = _CAMEL

    kind: Literal["price"] = "price"
    symbol: str = Field(..., min_length=1, max_length=24)
    range: str = Field(default="1y", max_length=24)
    interval: str = Field(default="1d", max_length=12)


class DeckChartSeries(BaseModel):
    model_config = _CAMEL

    name: str = Field(..., min_length=1, max_length=120)
    data: list[Optional[float]] = Field(default_factory=list)
    color: Optional[str] = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")

    @model_validator(mode="after")
    def _bounded(self) -> "DeckChartSeries":
        if len(self.data) > MAX_CHART_POINTS:
            raise ValueError(f"a deck series carries at most {MAX_CHART_POINTS} points")
        return self


class DeckChart(BaseModel):
    """A chart on a slide: explicit data, or a named market source.

    Both may be present: explicit series win, and `dataSource` is kept as
    provenance. A chart with neither is refused at render time (UNAVAILABLE),
    never drawn from invented numbers.
    """

    model_config = _CAMEL

    kind: ChartKind = "line"
    title: Optional[str] = Field(default=None, max_length=200)
    categories: list[str] = Field(default_factory=list, max_length=MAX_CHART_POINTS)
    series: list[DeckChartSeries] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list, max_length=MAX_CHART_POINTS)
    values: list[float] = Field(default_factory=list, max_length=MAX_CHART_POINTS)
    data_source: Optional[str] = Field(default=None, max_length=60)
    value_format: Optional[Literal["percent", "currency", "compact", "number"]] = None
    unit: Optional[str] = Field(default=None, max_length=24)


class DeckMetric(BaseModel):
    model_config = _CAMEL

    label: str = Field(..., min_length=1, max_length=80)
    value: str = Field(..., min_length=1, max_length=40)
    delta: Optional[str] = Field(default=None, max_length=40)
    note: Optional[str] = Field(default=None, max_length=160)
    source: Optional[str] = Field(default=None, max_length=200)


class DeckTable(BaseModel):
    model_config = _CAMEL

    headers: list[str] = Field(default_factory=list, max_length=MAX_TABLE_COLS)
    rows: list[list[str]] = Field(default_factory=list, max_length=MAX_TABLE_ROWS)
    caption: Optional[str] = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _shape(self) -> "DeckTable":
        if self.headers and len(self.headers) > MAX_TABLE_COLS:
            raise ValueError(f"a table carries at most {MAX_TABLE_COLS} columns")
        for row in self.rows:
            if self.headers and len(row) > len(self.headers):
                raise ValueError("a table row has more cells than the header")
            if len(row) > MAX_TABLE_COLS:
                raise ValueError(f"a table row carries at most {MAX_TABLE_COLS} cells")
        return self


class DeckColumn(BaseModel):
    model_config = _CAMEL

    heading: Optional[str] = Field(default=None, max_length=120)
    bullets: list[str] = Field(default_factory=list, max_length=MAX_BULLETS)
    body: Optional[str] = Field(default=None, max_length=MAX_BODY_CHARS)


class DeckImage(BaseModel):
    """An image already in the Artifact Center. No paths, no remote URLs."""

    model_config = _CAMEL

    artifact_id: Optional[str] = Field(default=None, max_length=120)
    alt: str = Field(default="", max_length=300)
    fit: ImageFit = ImageFit.COVER


class Slide(BaseModel):
    model_config = _CAMEL

    id: str = Field(default="", max_length=40)
    layout: SlideLayout = SlideLayout.BULLETS
    title: Optional[str] = Field(default=None, max_length=200)
    subtitle: Optional[str] = Field(default=None, max_length=240)
    eyebrow: Optional[str] = Field(default=None, max_length=80)
    bullets: list[str] = Field(default_factory=list)
    body: Optional[str] = Field(default=None, max_length=MAX_BODY_CHARS)
    statement: Optional[str] = Field(default=None, max_length=300)
    attribution: Optional[str] = Field(default=None, max_length=160)
    left: Optional[DeckColumn] = None
    right: Optional[DeckColumn] = None
    metrics: list[DeckMetric] = Field(default_factory=list)
    table: Optional[DeckTable] = None
    chart: Optional[DeckChart] = None
    image: Optional[DeckImage] = None
    notes: Optional[str] = Field(default=None, max_length=MAX_NOTES_CHARS)
    source: Optional[str] = Field(default=None, max_length=240)

    @model_validator(mode="after")
    def _bounded(self) -> "Slide":
        if len(self.bullets) > MAX_BULLETS:
            raise ValueError(f"a slide carries at most {MAX_BULLETS} bullets")
        for bullet in self.bullets:
            if len(str(bullet)) > MAX_BULLET_CHARS:
                raise ValueError(f"a bullet carries at most {MAX_BULLET_CHARS} characters")
        if len(self.metrics) > MAX_METRICS:
            raise ValueError(f"a slide carries at most {MAX_METRICS} metrics")
        for column in (self.left, self.right):
            if column is not None and len(column.bullets) > MAX_BULLETS:
                raise ValueError(f"a column carries at most {MAX_BULLETS} bullets")
        if self.layout is SlideLayout.TWO_COLUMN and not (self.left or self.right or self.body):
            raise ValueError("a two_column slide needs left/right columns or body text")
        if self.layout is SlideLayout.STATEMENT and not (self.statement or self.title):
            raise ValueError("a statement slide needs statement text")
        if self.layout is SlideLayout.CHART and self.chart is None:
            raise ValueError("a chart slide needs a chart block")
        if self.layout is SlideLayout.TABLE and self.table is None:
            raise ValueError("a table slide needs a table block")
        if self.layout is SlideLayout.METRIC_ROW and not self.metrics:
            raise ValueError("a metric_row slide needs metrics")
        return self


class DeckMeta(BaseModel):
    model_config = _CAMEL

    title: str = Field(default="Untitled deck", min_length=1, max_length=200)
    subtitle: Optional[str] = Field(default=None, max_length=240)
    author: Optional[str] = Field(default=None, max_length=120)
    date: Optional[str] = Field(default=None, max_length=40)
    footer: Optional[str] = Field(default=None, max_length=240)
    language: str = Field(default="en", max_length=16)


class DeckSpec(BaseModel):
    model_config = _CAMEL

    schema_version: Literal[1] = DECK_SCHEMA_VERSION
    meta: DeckMeta = Field(default_factory=DeckMeta)
    size: DeckSize = DeckSize.WIDE
    theme: DeckTheme = Field(default_factory=DeckTheme)
    data_sources: dict[str, DataSource] = Field(default_factory=dict)
    slides: list[Slide] = Field(default_factory=list)

    @model_validator(mode="after")
    def _deck_rules(self) -> "DeckSpec":
        if not self.slides:
            raise ValueError("a deck needs at least one slide")
        if len(self.slides) > MAX_SLIDES:
            raise ValueError(f"a deck carries at most {MAX_SLIDES} slides")
        seen: set[str] = set()
        for index, slide in enumerate(self.slides):
            if not slide.id:
                slide.id = f"s{index + 1}"
            if slide.id in seen:
                raise ValueError(f"duplicate slide id {slide.id!r}")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", slide.id):
                raise ValueError(
                    f"slide id {slide.id!r} must be 1-40 letters, digits, '-' or '_'"
                )
            seen.add(slide.id)
        return self


def parse_deck(value: Any) -> DeckSpec:
    """Validate a deck from a dict or a JSON string. Raises `DeckError`."""
    if isinstance(value, DeckSpec):
        return value
    if isinstance(value, str):
        import json

        try:
            value = json.loads(value)
        except ValueError as exc:
            raise DeckError("DECK_INVALID", f"deck content is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise DeckError("DECK_INVALID", "a deck must be a JSON object")
    try:
        return DeckSpec.model_validate(value)
    except Exception as exc:  # pydantic ValidationError
        raise DeckError("DECK_INVALID", f"invalid deck: {exc}") from exc


def dump_deck(deck: DeckSpec) -> str:
    """Canonical bytes as UTF-8 text: pretty, stable key order, no NaN."""
    return deck.model_dump_json(by_alias=True, indent=2, exclude_none=False)


def deck_size_bytes(deck: DeckSpec) -> int:
    return len(dump_deck(deck).encode("utf-8"))


__all__ = [
    "DECK_MIME",
    "DECK_SCHEMA_VERSION",
    "MAX_BODY_CHARS",
    "MAX_BULLETS",
    "MAX_BULLET_CHARS",
    "MAX_CHART_POINTS",
    "MAX_DECK_BYTES",
    "MAX_METRICS",
    "MAX_NOTES_CHARS",
    "MAX_SLIDES",
    "MAX_TABLE_COLS",
    "MAX_TABLE_ROWS",
    "DataSource",
    "DeckChart",
    "DeckChartSeries",
    "DeckColumn",
    "DeckError",
    "DeckImage",
    "DeckMeta",
    "DeckMetric",
    "DeckSize",
    "DeckSpec",
    "DeckTable",
    "DeckTheme",
    "ImageFit",
    "Slide",
    "SlideLayout",
    "ThemePreset",
    "deck_size_bytes",
    "dump_deck",
    "parse_deck",
]
