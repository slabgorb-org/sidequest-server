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

import contextlib
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic import ValidationError

from sidequest.game.entity_card import (
    EntityCard,
    npc_card_id,
    project_faction_card,
    project_location_card,
    project_npc_card,
    project_quest_card,
    project_relationship_card,
    project_trope_card,
)
from sidequest.game.entity_store import EntityStore
from sidequest.game.lifecycle_scope import quest_is_dormant, trope_is_dormant
from sidequest.game.npc_pool import is_projectable
from sidequest.game.projection.relationships import band_for

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sidequest.game.session import GameSnapshot, Npc
    from sidequest.genre.models.lore import Faction
    from sidequest.genre.models.tropes import TropeDefinition

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
    deliberate, observable withholding, distinct from ``failed`` (§D6). ``evicted``
    counts the ADR-138 §D5 *defensive* evictions: cards stranded in the store on a
    member that is no longer projectable (a ratified member re-marked pending after
    its card was already indexed — the durable store outliving a mutable gate flag).
    Normally 0 (the gate withholds pending members *before* they are ever indexed,
    so purge needs no eviction); a non-zero count is an invariant-violation signal
    the GM panel must see (§D6 ``entity_card.evicted``). ``evicted_ids`` records
    which cards were removed so the dispatch tier emits one span per eviction.
    ``outcome`` is derived so the watcher/span telemetry can never contradict the
    counts.

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
    evicted: int = 0
    npc_count: int = 0
    location_count: int = 0
    faction_count: int = 0
    # Story 84-3 (WI-4, ADR-118 §A2): honest reproject tally for RELATIONSHIP
    # cards, mirroring the other per-type counters. A relationship card is only
    # projected for an NPC that has SOMETHING to say (disposition history or a
    # non-neutral band — the gate below), so this counts the cards that crossed the
    # gate AND content-changed/inserted, not every stateful NPC.
    relationship_count: int = 0
    # Story 84-5 (WI-2, ADR-118 §A2): honest reproject tallies for DORMANT quest /
    # trope cards. Only DORMANT items are projected (completed quest; dormant/
    # resolved trope) — active ones ride their existing floor and are NOT indexed,
    # so these count the dormant-routed cards, the active-vs-dormant routing decision
    # the GM panel verifies.
    quest_count: int = 0
    trope_count: int = 0
    # Story 84-5 (WI-2, Reviewer OTEL nit): the ACTIVE side of the routing split —
    # quests/tropes that rode their EXISTING floor and were deliberately NOT indexed
    # (the items the loops ``continue`` past). Emitted alongside the dormant
    # ``quest_count``/``trope_count`` so the GM panel sees the full active-vs-dormant
    # routing decision ("N active riding floor vs M dormant indexed"), not just the
    # dormant half.
    active_quest_count: int = 0
    active_trope_count: int = 0
    failed_refs: list[str] = field(default_factory=list)
    card_ids: list[str] = field(default_factory=list)
    evicted_ids: list[str] = field(default_factory=list)

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


def _has_relationship_to_project(npc: Npc) -> bool:
    """The §A2 projection gate (Diamonds and Coal, SM steer): a relationship card is
    worth indexing only when there is SOMETHING to say.

    True when the NPC carries disposition HISTORY (a non-empty ``disposition_log``)
    OR a NON-NEUTRAL standing (its 5-level band is not ``"Neutral"``). A neutral NPC
    with no beats is coal — projecting an empty neutral relationship card for every
    stateful NPC would flood the index with content-free cards (and waste an embed
    slot), so it is deliberately skipped. The skip is observable: the
    ``relationship_count`` tally only advances for gated, content-bearing cards."""
    if npc.disposition_log:
        return True
    return band_for(int(npc.disposition)) != "Neutral"


def sync_entity_cards(
    store: EntityStore,
    snapshot: GameSnapshot,
    *,
    factions: Iterable[Faction] = (),
    locations: Iterable[LocationSyncView] = (),
    tropes: Iterable[TropeDefinition] = (),
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
    # Every ``npc:<slug>`` id a *projectable* entity owns this sweep: promoted
    # stateful Npcs, the pool origins they were promoted from, and ratified pool
    # members. The defensive eviction below (ADR-138 §D5) discards a stranded card
    # only if its id is ABSENT here — so a live card is never evicted by a
    # slug-colliding pending twin, regardless of pool order. Slug-normalized (via
    # ``npc_card_id``), so the guard is case-insensitive: a pending ``borin``
    # cannot evict a ratified ``BORIN``'s card.
    covered_ids: set[str] = set()

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
        # Seed the origin's slug too: a promoted Npc may have been renamed at
        # promotion (``core.name`` diverges from ``pool_origin``), and a pending
        # pool member matching the ORIGIN name must still be guarded against
        # eviction. The slug form makes this case-insensitive — replacing the old
        # raw-string ``covered_origins`` comparison, which missed case variants.
        if npc.pool_origin:
            with contextlib.suppress(ValueError):
                covered_ids.add(npc_card_id(npc.pool_origin))
        _apply_typed_card(store, card, result, "npc_count")

        # Story 84-3 (WI-4, ADR-118 §A2): project the relationship card alongside
        # the NPC card — STORED at index time, not on-demand at retrieval. Gated on
        # there being something to say (history or a non-neutral band) so a neutral,
        # history-less NPC doesn't flood the index. A stateful Npc is always
        # projectable (CreatureCore validates the name), so this never fails here;
        # the rel card upserts into its own ``rel:<slug>`` id, distinct from the NPC
        # card, and advances the ``relationship_count`` tally on insert/change.
        if _has_relationship_to_project(npc):
            rel_card = project_relationship_card(npc)
            _apply_typed_card(store, rel_card, result, "relationship_count")

    # Pool members second — skip any superseded by a stateful Npc (same card id,
    # or the pool member this Npc was promoted from). Eviction of stranded cards
    # is DEFERRED to a second pass (below): a ratified slug-twin can appear LATER
    # in the pool than its pending sibling, so the eviction decision must wait
    # until ``covered_ids`` is fully populated. Deciding inline would make the
    # guard depend on pool order.
    eviction_candidates: list[str] = []
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
            # ADR-138 §D5 — defensive eviction, DEFERRED. The skip above keeps a
            # *newly* pending member out of the index, but the EntityStore is
            # durable session state: a member ratified on an earlier turn already
            # has a card here, and a later code path can re-mark it
            # ``observation_pending`` (the mutable 49-6 gate outliving the
            # persisted card). That strands a card on a now-unprojectable member.
            # Collect its id as an eviction CANDIDATE and decide after the full
            # pool loop has populated ``covered_ids``: a projectable slug-twin
            # appearing later in the pool (or a promoted Npc) legitimately owns
            # this id, and a pending member swept first must never delete a live
            # card. A blank-named phantom could never have produced a card.
            try:
                eviction_candidates.append(npc_card_id(member.name))
            except ValueError:
                # Blank-named phantom — never indexed, no invariant violation.
                continue
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
        if card.id in covered_ids:
            continue
        _apply_typed_card(store, card, result, "npc_count")
        covered_ids.add(card.id)

    # ADR-138 §D5 — deferred defensive eviction. ``covered_ids`` now holds every
    # ``npc:<slug>`` a projectable entity owns this sweep (promoted Npcs + their
    # origins + ratified pool members), independent of the order they appeared in
    # the pool. Evict a stranded candidate only if NO projectable entity owns its
    # id; otherwise a pending slug-twin swept before its ratified sibling would
    # discard a live card and fire a false ``entity_card.evicted`` span. No Silent
    # Fallbacks cuts both ways: the eviction signal must be TRUE, not just loud.
    # ``discard`` is idempotent, so duplicate candidate ids count at most once.
    for stranded_id in eviction_candidates:
        if stranded_id in covered_ids:
            continue  # a projectable entity owns this id — not stranded
        if store.discard(stranded_id):
            result.evicted += 1
            result.evicted_ids.append(stranded_id)

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

    # Quests (84-5, §A2) — DORMANT-ONLY. A completed quest is a dormant note,
    # indexed for recall; an ACTIVE quest rides the existing ``state_summary`` floor
    # and is NOT projected here (double-render guard, AC-8). The dormant predicate
    # is the routing gate.
    for quest_id, entry in getattr(snapshot, "quest_log", {}).items():
        if not quest_is_dormant(entry):
            # Active quest — rides the existing state_summary floor, NOT indexed.
            # Count it so the routing split is observable (Reviewer OTEL nit).
            result.active_quest_count += 1
            continue
        try:
            card = project_quest_card(quest_id, entry)
        except (ValueError, ValidationError) as exc:
            result.failed += 1
            result.failed_refs.append(f"quest:{quest_id}")
            logger.warning("entity_sync.project_failed quest=%r error=%s", quest_id, exc)
            continue
        _apply_typed_card(store, card, result, "quest_count")

    # Tropes (84-5, §A2) — DORMANT-ONLY. A dormant/resolved trope is a callback
    # note; a PROGRESSING trope rides the existing trope-foreground floor and is NOT
    # projected (AC-8). The human name/description live on the ``TropeDefinition``
    # (TropeState carries only id), so the definitions are joined in by id; a state
    # with no matching definition cannot be projected (no name) and is skipped loud.
    _trope_defs = {d.id: d for d in tropes if d.id}
    for state in getattr(snapshot, "active_tropes", []):
        if not trope_is_dormant(state):
            # Progressing trope — rides the existing trope-foreground floor, NOT
            # indexed. Count it so the routing split is observable (Reviewer OTEL nit).
            result.active_trope_count += 1
            continue
        definition = _trope_defs.get(state.id)
        if definition is None:
            result.failed += 1
            result.failed_refs.append(f"trope:{state.id}")
            logger.warning("entity_sync.project_failed trope=%r error=no_definition", state.id)
            continue
        try:
            card = project_trope_card(state, definition)
        except (ValueError, ValidationError) as exc:
            result.failed += 1
            result.failed_refs.append(f"trope:{state.id}")
            logger.warning("entity_sync.project_failed trope=%r error=%s", state.id, exc)
            continue
        _apply_typed_card(store, card, result, "trope_count")

    return result
