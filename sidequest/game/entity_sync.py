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

from sidequest.game.entity_card import project_npc_card
from sidequest.game.entity_store import EntityStore

if TYPE_CHECKING:
    from sidequest.game.session import GameSnapshot

logger = logging.getLogger(__name__)


@dataclass
class EntitySyncResult:
    """Outcome of one entity-sync sweep (mirrors ``AccretionResult`` shape).

    ``reprojected`` counts cards that were inserted or content-changed (and so
    re-armed for embedding); ``unchanged`` counts cards whose projection matched
    the stored card (no churn); ``failed`` counts entities the projector
    rejected. ``outcome`` is derived so the watcher/span telemetry can never
    contradict the counts.

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


def sync_entity_cards(store: EntityStore, snapshot: GameSnapshot) -> EntitySyncResult:
    """Project the snapshot's NPC pool into ``store``, upserting each card.

    Idempotent within a turn: an entity whose projected content matches the
    stored card is counted ``unchanged`` and left untouched (including its
    embedding). A content change (e.g. a disposition crossing an attitude band)
    replaces the stored card and re-arms it for re-embedding. An entity the
    projector rejects (e.g. a blank-named member) is recorded in ``failed_refs``
    and counted — never minted as a stub card (No Silent Fallbacks).
    """
    result = EntitySyncResult()
    for member in snapshot.npc_pool:
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
        if store.upsert(card):
            result.reprojected += 1
            result.npc_count += 1
            result.card_ids.append(card.id)
        else:
            result.unchanged += 1
    return result
