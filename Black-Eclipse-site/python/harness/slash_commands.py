"""Slash-command tokenising and fuzzy scoring, shared by any command surface.

The typing surface differs between a terminal and a palette, but the *matching* rules
should not: a user who types `/mo` must get `/model` first on either. This module is the
contract — token extraction, the scoring ladder, alias resolution and stable ordering.

Transcribed from Vibe-Trading's `cli/commands/slash_router.py` (MIT). The ladder is
deliberately simple and is the part worth pinning:

    prefix      → 100 + len(needle)
    substring   →  50 + len(needle)
    subsequence →  10 + matched chars
    no match    →   0
    bare "/"    →   1  (everything, in registry order)

Ties break by registry order, and a returned list is always a fresh copy so a caller may
sort or filter it without touching the registry.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

__all__ = [
    "Command",
    "Matchable",
    "find_exact",
    "fuzzy_score",
    "match_commands",
    "parse_token",
]

#: Prefix scores, kept as named constants so the ladder is data, not literals.
PREFIX_SCORE = 100
SUBSTRING_SCORE = 50
SUBSEQUENCE_SCORE = 10
NO_MATCH = 0
BARE = 1


class Matchable:
    """The two fields matching needs: a name and an optional alias map."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name


@dataclass(frozen=True)
class Command:
    """One command entry: a keyword without its leading slash, plus a description."""

    name: str
    description: str = ""
    handler: str = ""


def parse_token(text: str) -> str:
    """The command token: ``/me arg`` → ``me``, ``/`` → ``""``, no slash → ``""``."""
    raw = str(text or "").lstrip()
    if not raw.startswith("/"):
        return ""
    parts = raw[1:].split(None, 1)
    return parts[0] if parts else ""


def fuzzy_score(needle: str, name: str) -> int:
    """Score how well ``needle`` matches ``name``. Higher is better; 0 is no match."""
    if not needle:
        return BARE
    needle_l = needle.lower()
    name_l = str(name).lower()
    if name_l.startswith(needle_l):
        return PREFIX_SCORE + len(needle_l)
    if needle_l in name_l:
        return SUBSTRING_SCORE + len(needle_l)
    index = 0
    for char in name_l:
        if index < len(needle_l) and char == needle_l[index]:
            index += 1
    if index == len(needle_l):
        return SUBSEQUENCE_SCORE + index
    return NO_MATCH


def match_commands(
    text: str,
    commands: Sequence[Command],
    aliases: Optional[dict[str, str]] = None,
) -> list[Command]:
    """Commands matching ``text``, best first; ties by registry order.

    Returns ``[]`` unless the trimmed input starts with ``/`` — a plain question is not
    a command. Aliases are resolved before scoring so ``/q`` surfaces ``quit``.
    """
    if not str(text or "").lstrip().startswith("/"):
        return []
    token = parse_token(text)
    lookup = dict(aliases or {})
    token = lookup.get(token, token)
    scored = []
    for index, command in enumerate(commands):
        score = fuzzy_score(token, command.name)
        if score > NO_MATCH:
            scored.append((score, -index, command))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [copy.copy(command) for _score, _index, command in scored]


def find_exact(
    name: str, commands: Sequence[Command], aliases: Optional[dict[str, str]] = None
) -> Optional[Command]:
    """The command with this exact name (or alias), else ``None``."""
    key = str(name or "").lstrip("/").strip()
    key = dict(aliases or {}).get(key, key)
    for command in commands:
        if command.name == key:
            return command
    return None
