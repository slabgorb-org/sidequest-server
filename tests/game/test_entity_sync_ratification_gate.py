"""RED-phase tests for Story 75-12 — wire the ratification gate into the
ADR-118 universal-retrieval NPC projection (ADR-138 §D2/§D5/§D6).

75-11 (merged) shipped the projection-eligibility predicate
``is_projectable(entity)``: a ``NpcPoolMember`` is projectable iff it is
*ratified* (``observation_pending is False``); a promoted ``Npc`` is always
projectable. 75-11 explicitly deferred *wiring* the predicate into the
projection surfaces. This story wires it into the ADR-118 surface — the
``EntityStore`` retrieval index that ``sync_entity_cards`` populates.

The load-bearing invariant (ADR-138 §D2 — "gate the FILL, not the FLOOR"):
an unratified, auto-minted pool member is a phantom the Story 49-6 gate may
purge next turn; the world has not committed to it, so it must NOT be embedded
into the semantic index. If it were, it would become retrievable as a "recalled"
NPC the world never ratified — exactly the kind of confabulation the OTEL
lie-detector exists to catch (§D1). Because pending members never enter the
index, a purge needs no eviction (§D5).

Observability (§D6): the skip is *counted*, never silent. ``EntitySyncResult``
carries a ``skipped_unratified`` tally so the dispatch layer can surface it on
the GM panel (the watcher/span assertion lives in the dispatch-tier test).

Contract sources (spec-authority order):
- ``.session/75-12-session.md`` (Technical Context: gate the FILL not the FLOOR)
- ``sprint/context/context-story-75-12.md``
- ADR-138 §D2/§D5/§D6, ADR-118 §D3, and 75-11's ``is_projectable`` predicate

INTENTIONALLY RED until 75-12 lands: ``sync_entity_cards`` today projects EVERY
pool member regardless of ``observation_pending``, and ``EntitySyncResult`` has
no ``skipped_unratified`` field.
"""

from __future__ import annotations

from sidequest.game.creature_core import CreatureCore
from sidequest.game.disposition import Disposition
from sidequest.game.entity_card import EntityType
from sidequest.game.entity_store import EntityStore
from sidequest.game.entity_sync import sync_entity_cards
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.game.session import Npc

# ---------------------------------------------------------------------------
# Fixtures — ratified vs pending pool members + stateful Npcs. Mirrors the
# construction patterns in test_entity_sync.py / test_entity_sync_stateful_npcs.py
# so the gate tests share one id space (``npc:<slug>``) with the live path.
# ---------------------------------------------------------------------------


def _ratified_member(name: str, *, disposition: int = 0, role: str = "smith") -> NpcPoolMember:
    """A world-committed pool member (``observation_pending=False`` — the
    default). Projectable; the gate must let it through unchanged."""
    return NpcPoolMember(
        name=name,
        role=role,
        pronouns="they/them",
        drawn_from="world_authored",
        disposition=Disposition(disposition),
    )


def _pending_member(name: str, *, role: str | None = None) -> NpcPoolMember:
    """An auto-minted, still-unratified phantom — the Story 49-6 observation
    gate has not promoted it yet. The §D2 gate must withhold it from the index."""
    return NpcPoolMember(
        name=name,
        role=role,
        pronouns="they/them",
        drawn_from="dialogue_extraction",
        observation_pending=True,
    )


def _npc(name: str, *, disposition: int = 0, pool_origin: str | None = None) -> Npc:
    return Npc(
        core=CreatureCore(name=name, description="A wandering smith.", personality="Gruff."),
        disposition=Disposition(disposition),
        pronouns="they/them",
        pool_origin=pool_origin,
    )


class _Snapshot:
    """Stand-in carrying the two entity sources ``sync_entity_cards`` reads
    (mirrors ``GameSnapshot``: ``npc_pool`` + ``npcs``)."""

    def __init__(
        self,
        *,
        pool: list[NpcPoolMember] | None = None,
        npcs: list[Npc] | None = None,
    ) -> None:
        self.npc_pool = pool or []
        self.npcs = npcs or []


# ---------------------------------------------------------------------------
# §D2 — the gate WITHHOLDS unratified pool members from the index
# ---------------------------------------------------------------------------


class TestPendingMembersAreNotIndexed:
    def test_pending_pool_member_produces_no_card(self) -> None:
        """The core §D2 invariant: a pending member is NOT projected into the
        store. Today it IS — ``sync_entity_cards`` projects every pool member —
        so this fails until the gate is wired."""
        store = EntityStore()

        result = sync_entity_cards(store, _Snapshot(pool=[_pending_member("Mira")]))

        assert store.query_by_type(EntityType.NPC) == []
        assert "npc:mira" not in store.cards
        assert result.npc_count == 0
        assert result.reprojected == 0

    def test_pending_member_is_counted_skipped_not_failed(self) -> None:
        """§D6: the skip is observable, never silent — ``skipped_unratified``
        tallies it. It is a deliberate withholding, NOT a projection failure, so
        ``failed`` stays 0 and ``failed_refs`` stays empty (a pending phantom is
        not a 'No Silent Fallbacks' error — it is an expected, ratified-later
        member)."""
        store = EntityStore()

        result = sync_entity_cards(store, _Snapshot(pool=[_pending_member("Mira")]))

        assert result.skipped_unratified == 1
        assert result.failed == 0
        assert result.failed_refs == []

    def test_blank_named_pending_member_is_skipped_before_projection(self) -> None:
        """Gate-before-project ordering: a pending member is withheld *before*
        the projector is ever called, so even a blank-named phantom (whose
        ``_slug`` would otherwise raise) is counted ``skipped_unratified`` — NOT
        ``failed``. The world has not committed to the phantom; its name
        validity is irrelevant until ratification. This pins the ordering via
        observable counts (no source grepping)."""
        store = EntityStore()
        bad = NpcPoolMember(name="   ", drawn_from="dialogue_extraction", observation_pending=True)

        result = sync_entity_cards(store, _Snapshot(pool=[bad]))

        assert result.skipped_unratified == 1
        assert result.failed == 0
        assert result.failed_refs == []
        assert len(store) == 0

    def test_all_pending_roster_indexes_nothing(self) -> None:
        """A scene whose entire off-stage pool is unratified seeds an empty
        index — every member withheld, every skip counted, nothing failed."""
        store = EntityStore()
        snapshot = _Snapshot(pool=[_pending_member("Mira"), _pending_member("Jacopo")])

        result = sync_entity_cards(store, snapshot)

        assert len(store) == 0
        assert result.skipped_unratified == 2
        assert result.reprojected == 0
        assert result.failed == 0


# ---------------------------------------------------------------------------
# §D1 — ratified members and stateful Npcs are unaffected (regression)
# ---------------------------------------------------------------------------


class TestRatifiedEntitiesStillProjected:
    def test_ratified_member_is_projected_normally(self) -> None:
        """The gate withholds ONLY pending members. A ratified (world-committed)
        member projects exactly as before — and is not counted as skipped."""
        store = EntityStore()

        result = sync_entity_cards(store, _Snapshot(pool=[_ratified_member("Borin")]))

        assert [c.id for c in store.query_by_type(EntityType.NPC)] == ["npc:borin"]
        assert result.npc_count == 1
        assert result.reprojected == 1
        assert result.skipped_unratified == 0

    def test_stateful_npc_is_always_projected_never_gated(self) -> None:
        """§D1: a promoted ``Npc`` is always projectable — promotion is itself
        the world's commitment, so ``is_projectable`` short-circuits True for it.
        Even a stateful NPC with no pool origin is indexed and never counted as
        unratified-skipped."""
        store = EntityStore()

        result = sync_entity_cards(store, _Snapshot(npcs=[_npc("Zed", pool_origin=None)]))

        assert "npc:zed" in {c.id for c in store.query_by_type(EntityType.NPC)}
        assert result.npc_count == 1
        assert result.skipped_unratified == 0

    def test_mixed_roster_projects_ratified_and_skips_pending(self) -> None:
        """The honest split a real scene produces: a ratified member is indexed,
        a pending one is withheld and counted. Both tallies are truthful — the
        GM panel can see exactly what reached the index and what was held back."""
        store = EntityStore()
        snapshot = _Snapshot(pool=[_ratified_member("Borin"), _pending_member("Mira")])

        result = sync_entity_cards(store, snapshot)

        assert [c.id for c in store.query_by_type(EntityType.NPC)] == ["npc:borin"]
        assert "npc:mira" not in store.cards
        assert result.reprojected == 1
        assert result.npc_count == 1
        assert result.skipped_unratified == 1
        assert result.failed == 0


# ---------------------------------------------------------------------------
# dedup-by-id (story title) — the gate composes with the stateful-wins dedup
# ---------------------------------------------------------------------------


class TestGateComposesWithDedup:
    def test_pending_member_shadowed_by_promoted_npc_yields_one_card(self) -> None:
        """A pending pool member and the stateful ``Npc`` it would promote to
        share ``npc:<slug>``. The stateful entity (the world's commitment) wins
        and is indexed; the result is exactly ONE card and an honest
        ``npc_count`` of 1 — never a phantom double-index. (This test pins the
        observable card outcome; it deliberately does not constrain whether the
        shadowed pool member is attributed to the dedup path or the gate, as
        both withhold it and both are defensible.)"""
        store = EntityStore()
        snapshot = _Snapshot(
            pool=[_pending_member("Borin")],
            npcs=[_npc("Borin", disposition=50, pool_origin="Borin")],
        )

        result = sync_entity_cards(store, snapshot)

        npc_cards = store.query_by_type(EntityType.NPC)
        assert [c.id for c in npc_cards] == ["npc:borin"]
        # The stateful projection won — the card reflects the Npc's friendly band.
        assert npc_cards[0].content.endswith("friendly")
        assert result.npc_count == 1
