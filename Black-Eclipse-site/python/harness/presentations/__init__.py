"""Presentations (decks): an editable deck artifact and a faithful .pptx export."""

from .model import (
    DECK_MIME,
    DECK_SCHEMA_VERSION,
    DeckError,
    DeckSpec,
    Slide,
    SlideLayout,
    dump_deck,
    parse_deck,
)
from .ops import DeckOp, apply_deck_ops, describe as describe_deck_ops

__all__ = [
    "DECK_MIME",
    "DECK_SCHEMA_VERSION",
    "DeckError",
    "DeckOp",
    "DeckSpec",
    "Slide",
    "SlideLayout",
    "apply_deck_ops",
    "describe_deck_ops",
    "dump_deck",
    "parse_deck",
]
