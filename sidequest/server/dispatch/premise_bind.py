"""Hydrate the runtime PoliticalState onto a snapshot at session start (Plan 2).

Mirrors ``scenario_bind.bind_scenario``: a no-op when the active world ships no
premises/blocs (a valid authoring choice, NOT a silent fallback to any default).
"""

from __future__ import annotations

import logging
from typing import Any

from opentelemetry import trace

from sidequest.game.political_state import PoliticalState
from sidequest.game.session import GameSnapshot

logger = logging.getLogger(__name__)


def bind_political_state(
    pack: Any,
    snapshot: GameSnapshot,
    *,
    genre_slug: str,
    world_slug: str,
) -> bool:
    """Hydrate ``snapshot.political_state`` from the active world's premises/blocs.

    Returns True when state was bound, False when the world has none (or is
    unknown) — in which case ``snapshot.political_state`` is left as ``None``.

    Mirrors the no-silent-fallback doctrine of ``bind_scenario``: a world with
    no premises/blocs is a valid authored state, not a misconfiguration.
    """
    world = pack.worlds.get(world_slug) if getattr(pack, "worlds", None) else None
    if world is None:
        span = trace.get_current_span()
        span.add_event(
            "political_state.bind_skipped",
            {
                "event": "political_state_bind_skipped",
                "genre": genre_slug,
                "world": world_slug,
                "reason": "unknown_world",
            },
        )
        logger.info(
            "political_state.bind_skipped genre=%s world=%s reason=unknown_world",
            genre_slug,
            world_slug,
        )
        return False

    state = PoliticalState.from_world(world)
    if state is None:
        span = trace.get_current_span()
        span.add_event(
            "political_state.bind_skipped",
            {
                "event": "political_state_bind_skipped",
                "genre": genre_slug,
                "world": world_slug,
                "reason": "no_world_politics",
            },
        )
        logger.info(
            "political_state.bind_skipped genre=%s world=%s reason=no_world_politics",
            genre_slug,
            world_slug,
        )
        return False

    snapshot.political_state = state

    span = trace.get_current_span()
    span.add_event(
        "political_state.initialized",
        {
            "event": "political_state_initialized",
            "genre": genre_slug,
            "world": world_slug,
            "premise_count": len(state.premises),
            "bloc_count": len(state.blocs),
        },
    )
    logger.info(
        "political_state.initialized genre=%s world=%s premises=%d blocs=%d",
        genre_slug,
        world_slug,
        len(state.premises),
        len(state.blocs),
    )
    return True


__all__ = ["bind_political_state"]
