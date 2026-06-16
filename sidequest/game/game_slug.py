"""Game slug generation and parsing.

A game slug is the canonical identifier for a game:
    solo:        <YYYY-MM-DD>-<world-slug>
    multiplayer: <YYYY-MM-DD>-<world-slug>-mp

Same-day + same-world + same-mode collisions are the resume path. Solo and
multiplayer of the same world on the same day are *different games* and must
have different slugs — otherwise the second mode is silently downgraded to
the first (CLAUDE.md "No Silent Fallbacks").
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from sidequest.game.persistence import GameMode

_MP_SUFFIX = "-mp"

SLUG_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-([a-z0-9][a-z0-9-]*?)(-mp)?$")


class InvalidSlugError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedSlug:
    date: date
    world_slug: str
    mode: GameMode = GameMode.SOLO


def generate_slug(world_slug: str, today: date, mode: GameMode = GameMode.SOLO) -> str:
    if not world_slug:
        raise ValueError("world_slug must not be empty")
    base = f"{today.isoformat()}-{world_slug}"
    if mode == GameMode.MULTIPLAYER:
        return base + _MP_SUFFIX
    return base


def parse_slug(slug: str) -> ParsedSlug:
    m = SLUG_RE.match(slug)
    if not m:
        raise InvalidSlugError(f"not a valid game slug: {slug!r}")
    y, mo, d, world, mp = m.groups()
    if not world:
        raise InvalidSlugError(f"empty world in slug {slug!r}")
    try:
        parsed_date = date(int(y), int(mo), int(d))
    except ValueError as exc:
        raise InvalidSlugError(f"invalid date in slug {slug!r}: {exc}") from exc
    mode = GameMode.MULTIPLAYER if mp else GameMode.SOLO
    return ParsedSlug(date=parsed_date, world_slug=world, mode=mode)
