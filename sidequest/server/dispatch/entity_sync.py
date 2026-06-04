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
from sidequest.game.entity_sync import LocationSyncView, sync_entity_cards
from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

if TYPE_CHECKING:
    from sidequest.genre.models.lore import Faction
    from sidequest.server.websocket_session_handler import (
        WebSocketSessionHandler,
        _SessionData,
    )

logger = logging.getLogger(__name__)


def _collect_world_factions(sd: _SessionData) -> list[Faction]:
    """The bound world's lore factions (Story 76-7) — world-tier flavor (SOUL
    "Crunch in the Genre, Flavor in the World"; ADR-120). Sourced from
    ``sd.genre_pack.worlds[sd.world_slug].lore.factions`` and nowhere else: with
    no world bound (``world_slug == ""``) there is no world lore in scope, so
    zero factions index — never a genre/global fallback (No Silent Fallbacks).
    Defensive ``getattr`` reads keep an entity-sync sweep from crashing a turn on
    a partially-built session."""
    world_slug = getattr(sd, "world_slug", "") or ""
    genre_pack = getattr(sd, "genre_pack", None)
    if not world_slug or genre_pack is None:
        return []
    world = genre_pack.worlds.get(world_slug)
    if world is None:
        return []
    lore = getattr(world, "lore", None)
    return list(getattr(lore, "factions", None) or [])


def _collect_location_views(sd: _SessionData) -> list[LocationSyncView]:
    """Normalized location views for this turn (Story 76-7).

    Locations are diffuse across three sources (ADR-118 §D3). **v1 covers PG
    ``location_promotions``** — the persisted, description-bearing Yes-And
    locations, reachable via ``sd.repository.list_location_promotions`` per
    discovered region. Room-graph rooms (``RoomState`` carries no prose) and
    ``world_materialization`` outputs need the ``location_view`` authored-prose
    resolution path and are deferred to a follow-up (see Dev deviation /
    Delivery Finding). A promotion with no ``promoted_canon`` prose is skipped,
    never minted as a stub card (No Silent Fallbacks). Collection never crashes a
    turn: missing repository / region read errors degrade to an empty list."""
    repository = getattr(sd, "repository", None)
    if repository is None:
        return []
    regions = getattr(sd.snapshot, "discovered_regions", None) or []
    views: list[LocationSyncView] = []
    for region_id in regions:
        try:
            rows = repository.list_location_promotions(region_id=region_id)
        except Exception:  # noqa: BLE001 — a bad region read must not cost the turn
            logger.exception("entity_sync.location_read_failed region=%s", region_id)
            continue
        for row in rows or []:
            if not (row.promoted_canon or "").strip():
                continue  # no projectable prose — skip, do not stub
            views.append(
                LocationSyncView(
                    location_id=row.entity_id,
                    name=row.label,
                    description=row.promoted_canon,
                    source="promotion",
                )
            )
    return views


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
        factions = _collect_world_factions(sd)
        locations = _collect_location_views(sd)
        result = sync_entity_cards(
            sd.entity_store, snapshot, factions=factions, locations=locations
        )
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
        # ADR-138 §D6 — the GM-panel lie-detector sees what the ratification gate
        # withheld from the index, so a quiet narrator (never re-citing a phantom)
        # is distinguishable from a silently-swallowed real NPC.
        span.set_attribute("entity_sync.npc_unratified_skipped", result.skipped_unratified)
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
            "skipped_unratified": result.skipped_unratified,
            "npc_count": result.npc_count,
            "faction_count": result.faction_count,
            "location_count": result.location_count,
            "outcome": result.outcome,
            "turn_number": interaction,
        },
        component="retrieval",
    )
