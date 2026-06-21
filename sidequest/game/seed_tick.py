"""Seed trope engine — per-turn expiry tick and session-start draw.

Sibling of :mod:`sidequest.game.trope_tick`. The trope engine handles
long-lived macro arcs; this module handles short-arc *seeds* (Epic 22):
deliberately vague narrative events dealt at session start and surfaced
in the narrator's VALLEY zone (ADR-009) until their ``lifespan_turns``
elapse — at which point they migrate into ``snapshot.seed_ghosts`` as
record-only callbacks for cross-session reference.

Story 22-3 wires the engine between the schema (22-1) and the narrator
context (the VALLEY-zone renderer in
:mod:`sidequest.agents.seed_context_builder`). Resolution mechanics
(seeds *taken* by the players) are out of scope for this epic — ghosts
are immutable.
"""

from __future__ import annotations

import logging
from typing import Any

from sidequest.game import zone_eligibility
from sidequest.game.seed_deck import SeedDeck
from sidequest.game.session import GameSnapshot, SeedState
from sidequest.genre.models.tropes import SeedTrope
from sidequest.telemetry.spans import (
    SPAN_SEED_DECK_EMPTY,
    SPAN_SEED_DRAWN,
    SPAN_SEED_EXPIRED,
    Span,
)

logger = logging.getLogger(__name__)

# Once-per-session dedup for the empty-deck signal: ``ensure_initial_draw``
# runs every turn, and an EMPTY deck never latches the active_seeds/seed_ghosts
# idempotency guard — without this the warning would fire per turn.
_deck_empty_signaled: set[str] = set()


def _zone_context(snapshot: GameSnapshot, pack: Any) -> tuple[set[str], bool, str]:
    """Resolve the Seam 4 zone-eligibility context for a deck draw (epic-157).

    Returns ``(active_factions, zoned, region)``. Only a zoned world pays the
    region query (Cost Scales with Drama); the 11 single-zone packs and any
    pre-bind / no-cartography pack return the permissive ``(∅, False, "")`` so the
    deck's draw filter is a no-op (zero behavior change for existing decks).
    """
    zoned = zone_eligibility.world_is_zoned(zone_eligibility.cartography_for(snapshot, pack))
    if not zoned:
        return set(), False, ""
    return zone_eligibility.active_factions(snapshot, pack), True, snapshot.region_for() or ""


def tick_seeds(
    snapshot: GameSnapshot,
    pack: Any,
    *,
    now_turn: int,
) -> None:
    """Advance the seed engine by one turn.

    Walks ``snapshot.active_seeds`` and migrates any entry whose
    :meth:`SeedState.is_expired` is True out of actives and into
    ``snapshot.seed_ghosts`` via :meth:`SeedState.to_ghost`. The
    migration is idempotent on the same ``now_turn`` — a second tick
    with the same turn finds no expired actives and produces no
    duplicate ghosts. Each migration fires one :data:`SPAN_SEED_EXPIRED`
    span carrying ``seed_id`` and ``expired_at_turn`` so the GM panel
    can distinguish "engine engaged, found no expiries" from "engine
    never engaged" (CLAUDE.md OTEL Observability Principle).

    ``pack`` is duck-typed (mirrors :func:`tick_tropes`'s
    ``pack.tropes`` discipline): only ``pack.seed_tropes`` is read by
    callers that need to resolve a seed's authored prose, and the tick
    itself does not need the pack — but accepting it keeps the call
    signature parallel to ``tick_tropes`` for the wire site.
    """

    del pack  # unused; signature parallels tick_tropes for wire-site parity

    surviving: list[SeedState] = []
    for seed in snapshot.active_seeds:
        if seed.is_expired(now_turn):
            snapshot.seed_ghosts.append(seed.to_ghost(now_turn))
            with Span.open(
                SPAN_SEED_EXPIRED,
                {
                    "seed_id": seed.id,
                    "expired_at_turn": now_turn,
                },
            ):
                pass
        else:
            surviving.append(seed)
    snapshot.active_seeds = surviving


_DEFAULT_INITIAL_HAND = 3


def ensure_initial_draw(
    snapshot: GameSnapshot,
    pack: Any,
    *,
    session_id: str,
    now_turn: int = 0,
    hand_size: int = _DEFAULT_INITIAL_HAND,
) -> None:
    """Deal the opening hand of seeds for a fresh session — idempotent.

    A session is "fresh" when both ``active_seeds`` and ``seed_ghosts``
    are empty. Once any seed has been drawn (and therefore lives on one
    of the two lists), this call is a no-op — reload paths must not
    redraw, and a session whose every active has already ghosted must
    not re-bootstrap.

    The deck is keyed by ``session_id`` (per 22-1's reproducibility
    contract); the same call on the same session produces the same
    opening hand. Packs without authored seeds (``pack.seed_tropes``
    empty or absent) are a no-op too.
    """

    if snapshot.active_seeds or snapshot.seed_ghosts:
        return

    seeds: list[SeedTrope] = list(getattr(pack, "seed_tropes", []) or [])
    if not seeds:
        # sq-playtest 2026-06-07 (77-7 forensics, split item c): this used to
        # be a SILENT no-op — the lull-escalation engine then ran all session
        # with an empty deck (fired=False reason=none_available every turn),
        # invisible until forensics. No Silent Fallbacks: emit once at
        # bootstrap so the GM panel sees the configuration smell up front.
        # "Once" needs an explicit latch: an empty deck never populates
        # active_seeds/seed_ghosts, so the fresh-session guard above never
        # trips and this branch re-runs EVERY turn.
        if session_id in _deck_empty_signaled:
            return
        _deck_empty_signaled.add(session_id)
        with Span.open(
            SPAN_SEED_DECK_EMPTY,
            {
                "session_slug": session_id,
                "genre_slug": snapshot.genre_slug or "",
                "world_slug": snapshot.world_slug or "",
            },
        ):
            pass
        logger.warning(
            "seed.deck_empty genre=%s world=%s session=%s — the bound seed "
            "source authors no seed_tropes; the lull-escalation engine will "
            "decline (reason=none_available) on every lull this session",
            snapshot.genre_slug,
            snapshot.world_slug,
            session_id,
        )
        return

    zone_active, zoned, region = _zone_context(snapshot, pack)
    deck = SeedDeck(
        genre_id=snapshot.genre_slug,
        world_id=snapshot.world_slug,
        session_id=session_id,
        seeds=seeds,
        active_factions=zone_active,
        zoned=zoned,
        region=region,
    )
    drawn: list[SeedState] = []
    for _ in range(hand_size):
        seed = deck.draw()
        if seed is None:
            break
        drawn.append(
            SeedState(
                id=seed.id,
                name=seed.name,
                activated_at_turn=now_turn,
                flavor_tags=list(seed.flavor_tags),
                lifespan_turns=seed.lifespan_turns,
                delivery_hints=list(seed.delivery_hints),
            )
        )
        # One span per drawn seed so the GM panel can attribute the
        # opening hand back to individual seed_ids (sibling discipline
        # of SPAN_TROPE_ACTIVATE — one-per-activation, not one-per-tick).
        with Span.open(
            SPAN_SEED_DRAWN,
            {
                "seed_id": seed.id,
                "session_id": session_id,
                "activated_at_turn": now_turn,
            },
        ):
            pass
    snapshot.active_seeds = drawn


def draw_engaged_seed(
    snapshot: GameSnapshot,
    pack: Any,
    *,
    session_id: str,
    engagement_signal: str,
    now_turn: int,
) -> None:
    """Draw one seed from the deck in response to player engagement.

    Reuses the ``SeedDeck`` (22-1) with ``drawn_ids`` reconstructed from
    the snapshot's active seeds and ghosts — no new persistence. The
    caller is responsible for gating on engagement thresholds (e.g.
    ``active_seeds < 2``); this function draws unconditionally if the
    deck has remaining seeds.

    Emits ``SPAN_SEED_DRAWN`` with ``trigger="engagement"`` so the GM
    panel can distinguish mid-session draws from bootstrap draws.
    """
    seeds: list[SeedTrope] = list(getattr(pack, "seed_tropes", []) or [])
    if not seeds:
        return

    drawn_ids = {s.id for s in snapshot.active_seeds} | {g.id for g in snapshot.seed_ghosts}

    zone_active, zoned, region = _zone_context(snapshot, pack)
    deck = SeedDeck(
        genre_id=snapshot.genre_slug,
        world_id=snapshot.world_slug,
        session_id=session_id,
        seeds=seeds,
        drawn_ids=drawn_ids,
        active_factions=zone_active,
        zoned=zoned,
        region=region,
    )

    seed = deck.draw()
    if seed is None:
        return

    snapshot.active_seeds.append(
        SeedState(
            id=seed.id,
            name=seed.name,
            activated_at_turn=now_turn,
            flavor_tags=list(seed.flavor_tags),
            lifespan_turns=seed.lifespan_turns,
            delivery_hints=list(seed.delivery_hints),
        )
    )

    with Span.open(
        SPAN_SEED_DRAWN,
        {
            "seed_id": seed.id,
            "trigger": "engagement",
            "session_id": session_id,
            "activated_at_turn": now_turn,
        },
    ):
        pass
