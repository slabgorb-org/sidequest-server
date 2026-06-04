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
from sidequest.game.location_view import get_location_prose
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


def _resolve_region_view(
    sd: _SessionData, region_id: str, *, source: str
) -> LocationSyncView | None:
    """Resolve one region's authored cartography prose into a normalized
    :class:`LocationSyncView`, or ``None`` when the region carries no projectable
    prose (Story 76-11).

    Prose comes from the bound world's authored ``cartography.regions[region_id]``
    merged with any live encounter overlay via ``location_view.get_location_prose``
    — never synthesized. A region absent from cartography, or whose effective
    prose is blank, is skipped (returns ``None``): No Silent Fallbacks / No
    Stubbing — the projector's blank-description guard must never see a stub. The
    ``source`` tag (``"room_graph"`` / ``"world_materialization"``) records the
    provenance for GM-panel forensics. Defensive ``getattr`` reads keep a sweep
    from crashing a turn on a partially-built session or a world without
    cartography (e.g. duck-typed test sessions)."""
    world_slug = getattr(sd, "world_slug", "") or ""
    genre_pack = getattr(sd, "genre_pack", None)
    if not world_slug or genre_pack is None:
        return None
    world = genre_pack.worlds.get(world_slug)
    if world is None:
        return None
    cartography = getattr(world, "cartography", None)
    regions = getattr(cartography, "regions", None) or {}
    region = regions.get(region_id)
    if region is None:
        return None
    authored = getattr(region, "description", "") or ""
    prose = get_location_prose(
        region_id=region_id, authored_description=authored, snapshot=sd.snapshot
    )
    if not prose.strip():
        return None  # no projectable prose — skip, do not stub
    name = getattr(region, "name", None) or region_id
    return LocationSyncView(location_id=region_id, name=name, description=prose, source=source)


def _collect_location_views(sd: _SessionData) -> tuple[list[LocationSyncView], int]:
    """Normalized location views for this turn (Story 76-7 + 76-11).

    Locations are diffuse across three sources (ADR-118 §D3), assembled here in
    the dispatch consumer and returned with a per-collection ``failed`` count:

    - **promotion** — PG ``location_promotions`` for the discovered regions, read
      in ONE batched round-trip (76-11 perf fix; was one query per region, an
      O(N)-per-turn cost the 76-7 Reviewer flagged).
    - **room_graph** — each discovered region resolved to its authored cartography
      prose via ``location_view`` (``RoomState`` itself carries no prose).
    - **world_materialization** — each region a materialized history chapter
      placed the party in (``snapshot.world_history`` is written only by
      ``materialize_world``), likewise resolved to authored prose.

    Precedence on a shared ``loc:<id>`` card id is first-source-wins (promotion →
    room_graph → world_materialization); a region is never double-indexed. A
    source that yields no projectable prose is skipped, never stubbed (No Silent
    Fallbacks). Collection never crashes a turn: a failed promotion read degrades
    to zero promotion views, surfaces a watcher event naming the dropped regions
    (76-11 observability fix — the GM-panel lie-detector must see the
    under-report), and is counted toward the returned ``failed`` total."""
    views: list[LocationSyncView] = []
    failed = 0
    seen_ids: set[str] = set()

    def _add(view: LocationSyncView | None) -> None:
        if view is None:
            return
        card_id = f"loc:{view.location_id}"
        if card_id in seen_ids:
            return  # first source wins — never double-index a region
        seen_ids.add(card_id)
        views.append(view)

    snapshot = sd.snapshot
    regions = list(getattr(snapshot, "discovered_regions", None) or [])

    # Source 1 (promotion) — ONE batched read for all discovered regions.
    repository = getattr(sd, "repository", None)
    if repository is not None and regions:
        try:
            rows = repository.list_location_promotions(region_ids=regions)
        except Exception as exc:  # noqa: BLE001 — a bad read must not cost the turn
            logger.exception("entity_sync.location_read_failed regions=%s", regions)
            failed += len(regions)
            _watcher_publish(
                "state_transition",
                {
                    "field": "entity_sync",
                    "op": "location_read_failed",
                    "regions": regions,
                    "error": type(exc).__name__,
                },
                component="retrieval",
                severity="warning",
            )
            rows = []
        for row in rows or []:
            if not (row.promoted_canon or "").strip():
                continue  # no projectable prose — skip, do not stub
            _add(
                LocationSyncView(
                    location_id=row.entity_id,
                    name=row.label,
                    description=row.promoted_canon,
                    source="promotion",
                )
            )

    # Source 2 (room_graph) — discovered regions resolved to authored prose.
    for region_id in regions:
        _add(_resolve_region_view(sd, region_id, source="room_graph"))

    # Source 3 (world_materialization) — regions a materialized chapter placed
    # the party in.
    for chapter in getattr(snapshot, "world_history", None) or []:
        location = getattr(chapter, "location", None)
        if location:
            _add(_resolve_region_view(sd, location, source="world_materialization"))

    return views, failed


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
        locations, location_failed = _collect_location_views(sd)
        result = sync_entity_cards(
            sd.entity_store, snapshot, factions=factions, locations=locations
        )
        # 76-11: a dropped promotion read is counted so the GM-panel sees the
        # location under-report (it already published its own watcher event).
        result.failed += location_failed
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
