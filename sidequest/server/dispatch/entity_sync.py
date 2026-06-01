"""Per-turn entity-sync dispatch (Story 75-6, ADR-118 §D2).

Sibling of ``lore_accretion.accrete_for_turn``. Runs in
``_execute_narration_turn`` right after lore accretion and before the embed
worker dispatch, so reprojected cards (``embedding_pending=True``) are in the
queue when the worker fires. Reprojects the snapshot's NPC pool into
``sd.entity_store`` so the universal-retrieval index the narrator reads (75-5)
reflects the *current* cast, not a stale or empty snapshot.

Like its lore sibling, sync runs BEFORE narration is delivered, so a sync
failure is ISOLATED: logged, surfaced as an ``op="failed"`` watcher event, and
swallowed. Keeping the index fresh must never cost the player their turn's
narration (ADR-006 graceful degradation).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from opentelemetry import trace

from sidequest.game.entity_card import (
    SPAN_CARD_REPROJECT_COUNT,
    SPAN_STALE_CARD_COUNT,
)
from sidequest.game.entity_sync import sync_entity_cards
from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

if TYPE_CHECKING:
    from sidequest.server.websocket_session_handler import (
        WebSocketSessionHandler,
        _SessionData,
    )

logger = logging.getLogger(__name__)


def sync_for_turn(handler: WebSocketSessionHandler, sd: _SessionData) -> None:
    """Reproject the snapshot's entities into ``sd.entity_store``.

    Emits an OTEL ``accretion.entity_sync`` span and a watcher event so the GM
    panel can confirm the retrieval index is being kept fresh (the lie-detector
    — without it you cannot tell a live index from a stale one). Failure is
    isolated and swallowed; the turn always survives.
    """
    snapshot = sd.snapshot
    interaction = snapshot.turn_manager.interaction

    try:
        result = sync_entity_cards(sd.entity_store, snapshot)
    except Exception as exc:  # noqa: BLE001 — sync must never crash a turn
        logger.exception("entity_sync.sweep_failed")
        _watcher_publish(
            "state_transition",
            {
                "field": "entity_sync",
                "op": "failed",
                "error": type(exc).__name__,
                "turn_number": interaction,
            },
            component="retrieval",
            severity="error",
        )
        return

    tracer = trace.get_tracer("sidequest.server.dispatch.entity_sync")
    with tracer.start_as_current_span("accretion.entity_sync") as span:
        span.set_attribute(SPAN_CARD_REPROJECT_COUNT, result.reprojected)
        span.set_attribute(SPAN_STALE_CARD_COUNT, result.unchanged)
        span.set_attribute("entity_sync.npc_count", result.npc_count)
        span.set_attribute("entity_sync.location_count", result.location_count)
        span.set_attribute("entity_sync.faction_count", result.faction_count)
        span.set_attribute("entity_sync.failed", result.failed)
        span.set_attribute("entity_sync.outcome", result.outcome)
        span.set_attribute("entity_sync.turn_number", interaction)

    _watcher_publish(
        "state_transition",
        {
            "field": "entity_sync",
            "op": "synced",
            "reprojected": result.reprojected,
            "unchanged": result.unchanged,
            "failed": result.failed,
            "npc_count": result.npc_count,
            "outcome": result.outcome,
            "turn_number": interaction,
        },
        component="retrieval",
    )
