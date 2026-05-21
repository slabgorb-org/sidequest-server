"""Seed trope deck engine (Epic 22, Story 22-1).

A per-(genre, world, session) deck that deals short-arc seed tropes without
replacement. The shuffle is reproducible: seeded by ``session_id`` so a deck
re-instantiated on session load deals the same order. ``drawn_ids`` carries the
set of already-dealt seeds (reconstructed from the persisted snapshot's active
seeds + ghosts) so reload never redeals a seed.

The deck takes an explicit ``seeds`` list rather than loading YAML itself —
callers load seeds from the genre pack and inject them, which keeps the engine
pure and decoupled from content (see story 22-1 design deviations).
"""

from __future__ import annotations

import hashlib
import random

from sidequest.genre.models.tropes import SeedTrope


def _seed_int(session_id: str) -> int:
    """Derive a stable integer PRNG seed from the session id.

    Hashing to an int keeps the shuffle reproducible across processes and
    Python versions (string seeds are version-sensitive) and is independent of
    PYTHONHASHSEED, unlike the builtin ``hash()``.
    """
    digest = hashlib.sha256(session_id.encode("utf-8")).digest()
    return int.from_bytes(digest, "big")


class SeedDeck:
    """Draw-without-replacement deck of seed tropes, reproducible per session."""

    def __init__(
        self,
        genre_id: str,
        world_id: str,
        session_id: str,
        seeds: list[SeedTrope],
        drawn_ids: set[str] | None = None,
    ) -> None:
        self.genre_id = genre_id
        self.world_id = world_id
        self.session_id = session_id
        self.drawn_ids: set[str] = set(drawn_ids) if drawn_ids else set()

        # Deterministic shuffle keyed by session_id so reload deals the same
        # order. Shuffle the full list independent of drawn_ids; draw() skips
        # already-drawn seeds, so reconstruction yields the same remaining order.
        ordered = list(seeds)
        random.Random(_seed_int(session_id)).shuffle(ordered)
        self._ordered = ordered

    def draw(self) -> SeedTrope | None:
        """Deal the next undrawn seed, or ``None`` when the deck is exhausted."""
        for seed in self._ordered:
            if seed.id not in self.drawn_ids:
                self.drawn_ids.add(seed.id)
                return seed
        return None
