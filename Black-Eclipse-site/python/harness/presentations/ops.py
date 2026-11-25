"""Typed deck edit operations -- validation and deterministic application.

A deck is structured JSON, so text-range operations do not apply. These ops are
the closed vocabulary the editor and the AI copilot use. Every op is re-applied
to the CURRENT deck under the canvas revision guard; nothing here trusts a
client-sent deck body.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from .model import (
    MAX_SLIDES,
    DeckChart,
    DeckError,
    DeckSpec,
    DeckTable,
    DeckMetric,
    Slide,
    SlideLayout,
    ThemePreset,
    dump_deck,
    parse_deck,
)

_CAMEL = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

DeckOpName = Literal[
    "add_slide", "delete_slide", "move_slide", "replace_slide",
    "set_meta", "set_theme", "set_notes", "set_title", "set_bullets",
    "set_body", "set_source", "set_chart", "set_table", "set_metrics",
]

MetaField = Literal["title", "subtitle", "author", "date", "footer", "language"]


class DeckOp(BaseModel):
    model_config = _CAMEL

    op: DeckOpName
    slide_id: Optional[str] = Field(default=None, max_length=40)
    slide: Optional[Slide] = None
    at: Optional[int] = Field(default=None, ge=0)
    to: Optional[int] = Field(default=None, ge=0)
    field: Optional[MetaField] = None
    value: Optional[str] = Field(default=None, max_length=4_000)
    text: Optional[str] = Field(default=None, max_length=4_000)
    bullets: Optional[list[str]] = None
    chart: Optional[DeckChart] = None
    table: Optional[DeckTable] = None
    metrics: Optional[list[DeckMetric]] = None
    preset: Optional[ThemePreset] = None
    accent: Optional[str] = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    explanation: str = Field(default="", max_length=1_000)


def _fail(code: str, message: str) -> None:
    raise DeckError(code, message)


def _deck(value: str) -> DeckSpec:
    try:
        return parse_deck(value)
    except DeckError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DeckError("DECK_INVALID", f"the current deck could not be read: {exc}") from exc


def _find(deck: DeckSpec, slide_id: Optional[str]) -> Slide:
    if not slide_id:
        _fail("DECK_INVALID", "this operation requires slideId")
    for slide in deck.slides:
        if slide.id == slide_id:
            return slide
    _fail("DECK_SLIDE_NOT_FOUND", f"slide {slide_id!r} does not exist in this deck")


def _next_id(deck: DeckSpec) -> str:
    used = {slide.id for slide in deck.slides}
    index = len(deck.slides) + 1
    while f"s{index}" in used:
        index += 1
    return f"s{index}"


def _apply_one(deck: DeckSpec, op: DeckOp) -> None:
    name = op.op

    if name == "add_slide":
        if len(deck.slides) >= MAX_SLIDES:
            _fail("DECK_LIMIT", f"a deck carries at most {MAX_SLIDES} slides")
        if op.slide is None:
            _fail("DECK_INVALID", "add_slide requires a slide")
        slide = op.slide.model_copy(deep=True)
        if not slide.id:
            slide.id = _next_id(deck)
        if any(existing.id == slide.id for existing in deck.slides):
            _fail("DECK_INVALID", f"slide id {slide.id!r} already exists")
        index = len(deck.slides) if op.at is None else min(op.at, len(deck.slides))
        deck.slides.insert(index, slide)
        return

    if name == "delete_slide":
        slide = _find(deck, op.slide_id)
        if len(deck.slides) == 1:
            _fail("DECK_LIMIT", "a deck keeps at least one slide")
        deck.slides.remove(slide)
        return

    if name == "move_slide":
        slide = _find(deck, op.slide_id)
        if op.to is None:
            _fail("DECK_INVALID", "move_slide requires 'to'")
        deck.slides.remove(slide)
        deck.slides.insert(min(op.to, len(deck.slides)), slide)
        return

    if name == "replace_slide":
        slide = _find(deck, op.slide_id)
        if op.slide is None:
            _fail("DECK_INVALID", "replace_slide requires a slide")
        replacement = op.slide.model_copy(deep=True)
        replacement.id = slide.id
        deck.slides[deck.slides.index(slide)] = replacement
        return

    if name == "set_meta":
        if op.field is None:
            _fail("DECK_INVALID", "set_meta requires a field")
        if op.field == "title" and not (op.value or "").strip():
            _fail("DECK_INVALID", "a deck title cannot be blank")
        setattr(deck.meta, op.field, op.value or None)
        if op.field == "title":
            deck.meta.title = op.value or "Untitled deck"
        if op.field == "language":
            deck.meta.language = op.value or "en"
        return

    if name == "set_theme":
        if op.preset is not None:
            deck.theme.preset = op.preset
        if op.accent is not None:
            deck.theme.accent = op.accent
        return

    slide = _find(deck, op.slide_id)

    if name == "set_notes":
        slide.notes = op.text
        return
    if name == "set_title":
        if not (op.text or "").strip():
            _fail("DECK_INVALID", "a slide title cannot be blank")
        slide.title = op.text
        return
    if name == "set_bullets":
        if op.bullets is None:
            _fail("DECK_INVALID", "set_bullets requires bullets")
        slide.bullets = [str(item) for item in op.bullets]
        return
    if name == "set_body":
        slide.body = op.text
        return
    if name == "set_source":
        slide.source = op.text
        return
    if name == "set_chart":
        if op.chart is None:
            _fail("DECK_INVALID", "set_chart requires a chart")
        slide.chart = op.chart
        return
    if name == "set_table":
        if op.table is None:
            _fail("DECK_INVALID", "set_table requires a table")
        slide.table = op.table
        return
    if name == "set_metrics":
        if op.metrics is None:
            _fail("DECK_INVALID", "set_metrics requires metrics")
        slide.metrics = op.metrics
        return

    _fail("DECK_OPERATION_UNSUPPORTED", f"unsupported deck operation {name!r}")


def apply_deck_ops(content: str, operations: list[DeckOp]) -> str:
    """Apply ops in order to the current deck text, returning canonical JSON."""
    deck = _deck(content)
    for index, operation in enumerate(operations or []):
        try:
            _apply_one(deck, operation)
        except DeckError as exc:
            raise DeckError(exc.code, f"operation {index + 1}: {exc.message}") from exc
    try:
        # Re-validate the whole deck: limits and cross-slide rules apply after
        # every op, not only to the fields an op touched.
        deck = parse_deck(deck.model_dump(by_alias=True))
        return dump_deck(deck)
    except DeckError as exc:
        raise DeckError(exc.code, f"the edit produced an invalid deck: {exc.message}") from exc


def describe(operations: list[DeckOp]) -> str:
    return ", ".join(operation.op for operation in operations or []) or "no change"


def slide_layout_names() -> list[str]:
    return [layout.value for layout in SlideLayout]


__all__ = ["DeckOp", "DeckOpName", "apply_deck_ops", "describe", "slide_layout_names"]
