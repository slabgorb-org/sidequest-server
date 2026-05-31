"""Post-turn lore-accretion dispatch (story 75-1).

Sweeps every seated PC's ``known_facts`` into the lore store each turn so
the embed worker (dispatched immediately after, see
``websocket_session_handler._dispatch_embed_worker``) embeds them and the
next turn's RAG retrieval finds them. Sibling of
``lore_embed.dispatch_worker``.

Party-wide by design (ADR-037 per-player sheets): sweeping only
``characters[0]`` would starve every non-host seat's discovered facts —
the same single-seat bug that starved non-host XP in ``award_turn_xp``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from opentelemetry import trace

from sidequest.game.lore_accretion import accrete_facts_to_lore
from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

if TYPE_CHECKING:
    from sidequest.server.websocket_session_handler import (
        WebSocketSessionHandler,
        _SessionData,
    )

logger = logging.getLogger(__name__)


def accrete_for_turn(handler: WebSocketSessionHandler, sd: _SessionData) -> None:
    """Accrete every seated PC's known_facts into ``sd.lore_store``.

    Idempotent across turns. Emits an OTEL span and a watcher event so the
    GM panel can confirm the RAG is being fed during play (the AC3
    lie-detector — without it you cannot tell accretion from improvisation).
    """
    snapshot = sd.snapshot
    interaction = snapshot.turn_manager.interaction

    total_accreted = 0
    total_skipped_dupe = 0
    total_skipped_blank = 0
    for character in snapshot.characters:
        if not character.known_facts:
            continue
        result = accrete_facts_to_lore(
            sd.lore_store,
            character.known_facts,
            interaction=interaction,
            pc_name=character.core.name,
        )
        total_accreted += result.accreted
        total_skipped_dupe += result.skipped_duplicate
        total_skipped_blank += result.skipped_blank

    tracer = trace.get_tracer("sidequest.server.dispatch.lore_accretion")
    with tracer.start_as_current_span("rag.lore_accreted") as span:
        span.set_attribute("lore.accreted", total_accreted)
        span.set_attribute("lore.skipped_duplicate", total_skipped_dupe)
        span.set_attribute("lore.skipped_blank", total_skipped_blank)
        span.set_attribute("lore.turn_number", interaction)

    _watcher_publish(
        "state_transition",
        {
            "field": "lore_accretion",
            "op": "accreted",
            "accreted": total_accreted,
            "skipped_duplicate": total_skipped_dupe,
            "skipped_blank": total_skipped_blank,
            "turn_number": interaction,
        },
        component="lore",
    )
