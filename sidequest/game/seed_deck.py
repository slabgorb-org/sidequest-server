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

from sidequest.game import zone_eligibility
from sidequest.genre.models.tropes import SeedTrope
from sidequest.telemetry.spans import Span
from sidequest.telemetry.spans.zone_eligibility import SPAN_ZONE_ELIGIBILITY_FILTERED


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
        *,
        active_factions: set[str] | None = None,
        zoned: bool = False,
        region: str = "",
    ) -> None:
        self.genre_id = genre_id
        self.world_id = world_id
        self.session_id = session_id
        self.drawn_ids: set[str] = set(drawn_ids) if drawn_ids else set()
        # Seam 4 zone-eligibility context (epic-157, ADR-059 amendment). Resolved
        # by the caller from the canonical region. A seed tagged for a faction
        # outside this active set is SKIPPED at draw() — never dealt, never marked
        # drawn — so it stays drawable once the party reaches its zone.
        # ``zoned=False`` (the 11 single-zone packs / a pre-bind turn) makes the
        # skip a permissive no-op (zero behavior change for existing decks).
        self._active_factions: set[str] = set(active_factions) if active_factions else set()
        self._zoned = zoned
        self._region = region

        # Deterministic shuffle keyed by session_id so reload deals the same
        # order. Shuffle the full list independent of drawn_ids; draw() skips
        # already-drawn seeds, so reconstruction yields the same remaining order.
        ordered = list(seeds)
        random.Random(_seed_int(session_id)).shuffle(ordered)
        self._ordered = ordered

    def draw(self) -> SeedTrope | None:
        """Deal the next undrawn, in-zone seed, or ``None`` when none remain.

        Seam 4 (epic-157, ADR-059 amendment): a candidate tagged for a faction
        outside the active zone is SKIPPED over the already-shuffled order (exactly
        like an already-drawn seed) and fires a ``zone_eligibility.filtered`` span.
        The skip preserves the deterministic shuffle — we filter the candidate
        set, NOT the ordering — and does NOT mark the seed drawn, so it stays
        drawable once the party's active zone includes its faction.
        """
        for seed in self._ordered:
            if seed.id in self.drawn_ids:
                continue
            if not zone_eligibility.is_eligible(
                seed.factions, self._active_factions, zoned=self._zoned
            ):
                # Tagged-but-wrong-zone → skip it and emit the lie-detector span.
                with Span.open(
                    SPAN_ZONE_ELIGIBILITY_FILTERED,
                    {
                        "subsystem": "seed",
                        "content_id": seed.id,
                        "content_factions": sorted(seed.factions),
                        "active_factions": sorted(self._active_factions),
                        "region": self._region,
                    },
                ):
                    pass
                continue
            self.drawn_ids.add(seed.id)
            return seed
        return None
