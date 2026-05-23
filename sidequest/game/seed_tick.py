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

from typing import Any

from sidequest.game.seed_deck import SeedDeck
from sidequest.game.session import GameSnapshot, SeedState
from sidequest.genre.models.tropes import SeedTrope


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
    duplicate ghosts.

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
        return

    deck = SeedDeck(
        genre_id=snapshot.genre_slug,
        world_id=snapshot.world_slug,
        session_id=session_id,
        seeds=seeds,
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
    snapshot.active_seeds = drawn
