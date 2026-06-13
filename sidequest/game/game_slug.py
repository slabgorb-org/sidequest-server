"""Game slug generation and parsing.

A game slug is the canonical identifier for a game:
    solo:        <YYYY-MM-DD>-<world-slug>-<token>
    multiplayer: <YYYY-MM-DD>-<world-slug>-mp-<token>

``<token>`` is a short random hex string minted per game, so every created
game is unique. The date prefix and ``-mp`` mode marker are preserved: the
date is the human "when did this start" signal, and the mode marker keeps solo
and multiplayer of the same world distinct (otherwise the second mode would be
silently downgraded to the first — CLAUDE.md "No Silent Fallbacks").

History (sq-playtest 2026-06-13): the slug used to be the deterministic
``<date>-<world>[-mp]`` with no token, so a second run of the same world on the
same day RESUMED the first — silently inheriting its durable seat roster. On a
reused per-day MP slug that inherited a leftover, never-reconnecting seat and
deadlocked the turn barrier on a phantom sealer (the Kael deadlock). Real
co-play joins by sharing the exact ``/play/<slug>`` link, not by re-deriving a
deterministic slug, so uniqueness removes the inheritance class at the root.

Legacy deterministic slugs (durable saves predate the token) still parse — the
trailing ``-<token>`` is optional in :func:`parse_slug`.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import date

from sidequest.game.persistence import GameMode

_MP_SUFFIX = "-mp"

# Bytes of randomness in a minted token. 4 bytes → 8 lowercase hex chars
# (4.3e9 space per world+day+mode), ample for a personal-scale project while
# staying short enough to read in a shared link.
_TOKEN_BYTES = 4

# date - world - [mp] - [token]. World is non-greedy so the optional ``-mp``
# marker and ``-<token>`` peel off the tail first; both are optional so legacy
# deterministic slugs (no token) still parse. ``token`` is lowercase hex.
SLUG_RE = re.compile(
    r"^(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})-"
    r"(?P<world>[a-z0-9][a-z0-9_-]*?)"
    r"(?P<mp>-mp)?"
    r"(?:-(?P<token>[0-9a-f]{6,}))?$"
)


class InvalidSlugError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedSlug:
    date: date
    world_slug: str
    mode: GameMode = GameMode.SOLO


def mint_token() -> str:
    """A fresh, unique-per-game lowercase-hex token."""
    return secrets.token_hex(_TOKEN_BYTES)


def generate_slug(
    world_slug: str,
    today: date,
    mode: GameMode = GameMode.SOLO,
    *,
    token: str | None = None,
) -> str:
    """Mint a unique game slug ``<date>-<world>[-mp]-<token>``.

    ``token`` is injectable for deterministic tests; when omitted a fresh one
    is minted, so two calls with identical (world, day, mode) never collide.
    """
    if not world_slug:
        raise ValueError("world_slug must not be empty")
    base = f"{today.isoformat()}-{world_slug}"
    if mode == GameMode.MULTIPLAYER:
        base += _MP_SUFFIX
    return f"{base}-{token or mint_token()}"


def parse_slug(slug: str) -> ParsedSlug:
    """Extract the kept signal (date, world, mode) from a slug.

    Accepts both new tokened slugs and legacy deterministic slugs (the token
    is optional). The token itself is not surfaced — it carries no signal.
    """
    m = SLUG_RE.match(slug)
    if not m:
        raise InvalidSlugError(f"not a valid game slug: {slug!r}")
    world = m.group("world")
    if not world:
        raise InvalidSlugError(f"empty world in slug {slug!r}")
    try:
        parsed_date = date(int(m.group("y")), int(m.group("mo")), int(m.group("d")))
    except ValueError as exc:
        raise InvalidSlugError(f"invalid date in slug {slug!r}: {exc}") from exc
    mode = GameMode.MULTIPLAYER if m.group("mp") else GameMode.SOLO
    return ParsedSlug(date=parsed_date, world_slug=world, mode=mode)
