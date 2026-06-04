"""Per-turn entity card sync / reproject (Story 75-6, ADR-118 §D2).

Closes the universal-retrieval mutation loop. 75-4 built ``EntityCard`` +
projectors + the typed ``EntityStore``; 75-5 reads that store every turn via
``retrieve_turn_context``. But the store is inert — nothing populates it and
nothing refreshes it when an NPC's disposition shifts. ``sync_entity_cards`` is
both the first seeder and the per-turn reproject: it projects each live entity
and upserts it, re-arming only the cards whose projected content actually
changed (the determinism 75-4 guarantees makes "changed" a content comparison,
not a wall-clock or blanket re-embed).

The game-tier pure sweep here mirrors ``accrete_facts_to_lore`` (75-1): a
deterministic per-entity reproject drives an idempotent upsert, and the sweep
returns a result dataclass so the dispatch layer (:mod:`sidequest.server.dispatch.entity_sync`)
can emit GM-panel telemetry. No silent fallbacks: an entity the projector cannot
render is counted and recorded, never minted as a stub card.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic import ValidationError

from sidequest.game.entity_card import (
    EntityCard,
    project_faction_card,
    project_location_card,
    project_npc_card,
)
from sidequest.game.entity_store import EntityStore
from sidequest.game.npc_pool import is_projectable

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sidequest.game.session import GameSnapshot
    from sidequest.genre.models.lore import Faction

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LocationSyncView:
    """A normalized, projectable view of one location (Story 76-7).

    Locations are diffuse across the room graph, ``world_materialization``, and
    PG ``location_promotions`` (ADR-118 §D3). The per-source adaptation lives in
    the dispatch consumer (``dispatch.entity_sync``); ``sync_entity_cards`` takes
    the already-normalized view so the pure sweep stays source-agnostic.
    ``source`` is the provenance tag the projector records in card metadata.
    """

    location_id: str
    name: str
    description: str
    source: str


@dataclass
class EntitySyncResult:
    """Outcome of one entity-sync sweep (mirrors ``AccretionResult`` shape).

    ``reprojected`` counts cards that were inserted or content-changed (and so
    re-armed for embedding); ``unchanged`` counts cards whose projection matched
    the stored card (no churn); ``failed`` counts entities the projector
    rejected. ``skipped_unratified`` counts pool members withheld from the index
    by the ADR-138 §D2 ratification gate (``observation_pending`` phantoms) — a
    deliberate, observable withholding, distinct from ``failed`` (§D6). ``outcome``
    is derived so the watcher/span telemetry can never contradict the counts.

    The three per-type counters (``npc_count``/``location_count``/
    ``faction_count``) are honest reproject tallies per entity type, mirroring
    the ``entity_sync.{npc,location,faction}_count`` span attributes (ADR-118
    §D5). 75-6 syncs the NPC pool only, so ``location_count`` and
    ``faction_count`` accurately report **0 reprojected** every turn until the
    deferred faction/location sources are wired (logged deviation; ADR-118
    permits the NPC-first v1). They are present now so the span schema the GM
    panel reads is stable across that follow-up — not stub fields, but a true
    zero measurement.
    """

    reprojected: int = 0
    unchanged: int = 0
    failed: int = 0
    skipped_unratified: int = 0
    npc_count: int = 0
    location_count: int = 0
    faction_count: int = 0
    failed_refs: list[str] = field(default_factory=list)
    card_ids: list[str] = field(default_factory=list)

    @property
    def outcome(self) -> str:
        """``"partial"`` if any entity failed to project, else ``"skipped"`` when
        nothing was reprojected (a quiet, all-unchanged turn), else
        ``"success"``."""
        if self.failed:
            return "partial"
        if self.reprojected == 0:
            return "skipped"
        return "success"


def _apply_typed_card(
    store: EntityStore,
    card: EntityCard,
    result: EntitySyncResult,
    count_field: str,
) -> None:
    """Upsert one card and update the reproject tallies + the per-type counter
    named by ``count_field`` (``"npc_count"`` / ``"faction_count"`` /
    ``"location_count"``). A content change (or first insert) re-arms the card
    and counts a reproject; an unchanged card is left untouched (embedding
    preserved). One helper for every type — the only per-type difference is
    which honest counter advances."""
    if store.upsert(card):
        result.reprojected += 1
        setattr(result, count_field, getattr(result, count_field) + 1)
        result.card_ids.append(card.id)
    else:
        result.unchanged += 1


def sync_entity_cards(
    store: EntityStore,
    snapshot: GameSnapshot,
    *,
    factions: Iterable[Faction] = (),
    locations: Iterable[LocationSyncView] = (),
) -> EntitySyncResult:
    """Project the snapshot's cast — and (76-7) the bound world's factions and
    the turn's locations — into ``store``, upserting each card.

    Story 76-6: the NPC cast is the union of two sources — the stateful
    ``snapshot.npcs`` (promoted / narrator-invented mechanical entities) and the
    identity-only ``snapshot.npc_pool``. A promoted ``Npc`` and the pool member
    it was promoted from share a card id (``npc:<slug>``); the **stateful entity
    takes precedence** (it carries the live mechanical state) and the pool member
    is skipped, so a promoted NPC is never double-indexed or double-counted.

    Story 76-7: ``factions`` are the bound world's lore factions
    (``World.lore.factions`` — world-tier flavor, SOUL "Crunch in the Genre,
    Flavor in the World") and ``locations`` are pre-normalized
    :class:`LocationSyncView`s the dispatch consumer assembled from the diffuse
    location sources. Both are passed in (not read off the snapshot) because
    neither lives on it. Per-type card ids are namespaced (``faction:``/``loc:``
    vs ``npc:``) so they never collide in ``covered_ids``.

    Idempotent within a turn: an entity whose projected content matches the
    stored card is counted ``unchanged`` and left untouched (including its
    embedding). A content change (e.g. a disposition crossing an attitude band)
    replaces the stored card and re-arms it for re-embedding. An entity the
    projector rejects (e.g. a blank-named pool member, a description-less
    location) is recorded in ``failed_refs`` and counted — never minted as a stub
    card (No Silent Fallbacks). A stateful ``Npc`` cannot be unprojectable:
    ``CreatureCore`` validates its name non-blank, so the projector's ``_slug``
    guard never fires for it.
    """
    result = EntitySyncResult()
    covered_ids: set[str] = set()
    covered_origins: set[str] = set()

    # Stateful NPCs first — the richer entity takes precedence over its origin.
    for npc in snapshot.npcs:
        try:
            card = project_npc_card(npc)
        except (ValueError, ValidationError) as exc:
            result.failed += 1
            result.failed_refs.append(npc.core.name)
            logger.warning(
                "entity_sync.project_failed entity=%r error=%s",
                npc.core.name,
                exc,
            )
            continue
        covered_ids.add(card.id)
        if npc.pool_origin:
            covered_origins.add(npc.pool_origin)
        _apply_typed_card(store, card, result, "npc_count")

    # Pool members second — skip any superseded by a stateful Npc (same card id,
    # or the pool member this Npc was promoted from).
    for member in snapshot.npc_pool:
        # ADR-138 §D2 — gate the FILL, not the FLOOR. An unratified
        # (``observation_pending``) pool member is an auto-minted phantom the
        # Story 49-6 gate may purge next turn; the world has not committed to it,
        # so it must NOT enter the semantic index (where it would resurface as a
        # "recalled" NPC the world never ratified). Gate BEFORE projection: a
        # phantom's name validity is irrelevant until ratification, so a blank
        # name is a skip, not a ``failed`` (§D5: never indexed → never needs
        # eviction). The skip is counted, never silent (§D6) — the scene-present
        # floor still shows the member via ``build_npc_working_set``.
        if not is_projectable(member):
            result.skipped_unratified += 1
            continue
        try:
            card = project_npc_card(member)
        except (ValueError, ValidationError) as exc:
            # The projector rejected this entity (blank name → _slug raises;
            # blank content → EntityCard validation raises). Count it loud and
            # move on — do NOT emit a stub card for an unprojectable entity.
            result.failed += 1
            result.failed_refs.append(member.name)
            logger.warning(
                "entity_sync.project_failed entity=%r error=%s",
                member.name,
                exc,
            )
            continue
        if card.id in covered_ids or member.name in covered_origins:
            continue
        _apply_typed_card(store, card, result, "npc_count")

    # Factions (76-7) — the bound world's lore roster (world-tier flavor).
    for faction in factions:
        try:
            card = project_faction_card(faction)
        except (ValueError, ValidationError) as exc:
            result.failed += 1
            result.failed_refs.append(faction.name)
            logger.warning(
                "entity_sync.project_failed faction=%r error=%s",
                faction.name,
                exc,
            )
            continue
        if card.id in covered_ids:
            continue
        covered_ids.add(card.id)
        _apply_typed_card(store, card, result, "faction_count")

    # Locations (76-7) — pre-normalized views from the diffuse sources.
    for view in locations:
        try:
            card = project_location_card(
                location_id=view.location_id,
                name=view.name,
                description=view.description,
                source=view.source,
            )
        except (ValueError, ValidationError) as exc:
            result.failed += 1
            result.failed_refs.append(view.location_id)
            logger.warning(
                "entity_sync.project_failed location=%r error=%s",
                view.location_id,
                exc,
            )
            continue
        if card.id in covered_ids:
            continue
        covered_ids.add(card.id)
        _apply_typed_card(store, card, result, "location_count")

    return result
