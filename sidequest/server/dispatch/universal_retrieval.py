"""Per-turn universal-retrieval GM-panel dispatch (Story 75-7, ADR-118 §D5).

Sibling of ``entity_sync.sync_for_turn`` and ``lore_embed.retrieve_for_turn``.

The ``retrieval.universal`` OTEL *span* already ships (75-5,
``game/retrieval_orchestration.retrieve_turn_context``) and fires every turn —
but it only reaches OTLP/Jaeger. The GM panel (the lie-detector Keith watches
during play) subscribes to the **WatcherHub** event stream, not the raw span
pipeline, and ``retrieve_turn_context`` emits no ``publish_event``. So today the
universal-retrieval decision is invisible on the GM panel.

This module adds the *watcher-emission half*: it wraps the pure (game-tier)
``retrieve_turn_context`` and publishes a ``state_transition`` watcher event
carrying the same ADR-118 §D5 attribute set the span holds, under
``component="retrieval"`` (the subsystem label 75-6 established for entity-sync),
so the decision lands on the dashboard the same way its siblings do.

Like its siblings, the emission is best-effort and isolated: a ``publish_event``
failure is logged and swallowed — observing the turn must never crash the turn
(ADR-006 graceful degradation). The pure orchestrator stays watcher-free for
import hygiene; the watcher emission belongs here in the server dispatch tier.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sidequest.agents.npc_context import player_referenced_npcs_from_action
from sidequest.game.retrieval_orchestration import (
    RetrievedEntities,
    retrieve_turn_context,
)
from sidequest.telemetry.retrieval_reason import card_reason_payload
from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

if TYPE_CHECKING:
    from sidequest.server.session_handler import WebSocketSessionHandler, _SessionData

logger = logging.getLogger(__name__)

# The one degraded outcome that is a genuine failure (daemon down / embed
# error) and must stand out on the GM panel; the others (budget_exhausted,
# no_candidates, success) are legitimate decisions, not errors.
_FAILURE_OUTCOME = "query_failed"


async def retrieve_for_turn(
    handler: WebSocketSessionHandler,
    sd: _SessionData,
    action: str,
) -> RetrievedEntities:
    """Run universal retrieval and surface the decision on the GM panel.

    Calls the 75-5 ``retrieve_turn_context`` (which emits the
    ``retrieval.universal`` span and NEVER raises), then publishes a watcher
    event carrying the ADR-118 §D5 attribute set so the same numbers the span
    holds reach the dashboard. Returns the ``RetrievedEntities`` unchanged so the
    caller can still build the narrator prompt from the floor + fill.
    """
    result = await retrieve_turn_context(
        sd.entity_store,
        sd.snapshot,
        action,
        current_turn=sd.snapshot.turn_manager.interaction,
        # Story 75-10: derive the per-turn reference signal from the player's
        # action so off-stage NPCs the player named render BRIEF (name+role) in
        # the floor instead of COMPACT (name only). The action text already lives
        # here — no separate extractor needed.
        player_referenced_npcs=player_referenced_npcs_from_action(sd.snapshot, action),
    )

    # Observing the turn must never crash the turn: a publish failure is logged
    # and swallowed, and the result is returned regardless (ADR-006).
    try:
        severity = "warning" if result.outcome == _FAILURE_OUTCOME else "info"
        _watcher_publish(
            "state_transition",
            {
                "field": "universal_retrieval",
                "op": result.outcome,
                "budget_total": result.budget_total,
                "floor_count": result.floor_count,
                "floor_token_cost": result.floor_token_cost,
                "fill_candidate_count": result.fill_candidate_count,
                "fill_selected_count": result.fill_selected_count,
                "fill_token_cost": result.fill_token_cost,
                "npc_count": len(result.retrieved_npcs or []),
                "location_count": len(result.retrieved_locations or []),
                "faction_count": len(result.retrieved_factions or []),
                "rejected_below_similarity": result.rejected_below_similarity,
                "dimension_mismatch_count": result.dimension_mismatch_count,
                "turn_number": sd.snapshot.turn_manager.interaction,
                # Story 84-4 (ADR-118 §A5): the load-bearing WI-6 fix. 84-1 put
                # embed_skipped on the SPAN only — the GM panel reads the WatcherHub
                # EVENT stream, not Jaeger, so the drama-gate decision never reached
                # the dashboard. Surface it (both polarities) here.
                "embed_skipped": result.embed_skipped,
                # The per-card score decomposition rides the event NATIVELY (a list
                # of dicts) — the hub passes the fields dict through verbatim. This
                # is the SAME payload shape the span JSON-encodes; do NOT double-
                # encode it into a string here. Empty list on a gate-skip (present,
                # never absent — the panel always reads a parseable field).
                "card_reasons": [card_reason_payload(s) for s in result.card_scores],
            },
            component="retrieval",
            severity=severity,
        )
    except Exception as exc:  # noqa: BLE001 — GM-panel emission cannot crash a turn
        logger.warning(
            "universal_retrieval.publish_failed outcome=%s error=%s",
            result.outcome,
            exc,
        )

    return result
