"""Story 75-14 — defensive eviction of stranded cards (ADR-138 §D5/§D6).

75-12 (merged) gates the FILL: a *newly* pending pool member is never projected
into the ``EntityStore``, so a phantom purge needs no eviction (§D5's tidiest
property). This story covers the one case the FILL gate cannot: the ``EntityStore``
is **durable session state** (serialized into the save), while ``observation_pending``
is a **mutable** flag. A member ratified on turn N has a card in the store; if a
later turn re-marks that member ``observation_pending`` (the 49-6 gate flipping
back, or a future re-mint path), the card is *stranded* — it outlives the entity's
projection-eligibility and would keep surfacing as a "recalled" NPC the world has
un-committed. §D5 mandates a defensive eviction; §D6 mandates it be observable
(``entity_card.evicted{reason=unprojectable}``), never a silent drop.

These pin the pure-sweep behavior: the stranded card is removed, the eviction is
counted, and the common cases (no prior card, blank name, promoted-Npc shadow)
evict nothing. The span/watcher observability assertion lives in the dispatch-tier
test (``tests/server/dispatch/test_entity_sync_eviction_otel.py``).
"""

from __future__ import annotations

from sidequest.game.creature_core import CreatureCore
from sidequest.game.disposition import Disposition
from sidequest.game.entity_card import EntityType
from sidequest.game.entity_store import EntityStore
from sidequest.game.entity_sync import sync_entity_cards
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.game.session import Npc


def _ratified_member(name: str, *, disposition: int = 0, role: str = "smith") -> NpcPoolMember:
    return NpcPoolMember(
        name=name,
        role=role,
        pronouns="they/them",
        drawn_from="world_authored",
        disposition=Disposition(disposition),
    )


def _pending_member(name: str, *, role: str | None = None) -> NpcPoolMember:
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
    """Stand-in carrying the two entity sources ``sync_entity_cards`` reads."""

    def __init__(
        self,
        *,
        pool: list[NpcPoolMember] | None = None,
        npcs: list[Npc] | None = None,
    ) -> None:
        self.npc_pool = pool or []
        self.npcs = npcs or []


# ---------------------------------------------------------------------------
# §D5 — a card stranded on a now-unprojectable member is evicted
# ---------------------------------------------------------------------------


class TestStrandedCardIsEvicted:
    def test_card_for_member_re_marked_pending_is_evicted(self) -> None:
        """The core §D5 invariant. Turn 1 indexes a ratified member; the *same*
        member is then re-marked pending and re-synced. The stranded card must be
        evicted — never left retrievable on an un-committed entity."""
        store = EntityStore()
        member = _ratified_member("Borin")

        # Turn 1 — ratified → indexed.
        sync_entity_cards(store, _Snapshot(pool=[member]))
        assert "npc:borin" in store.cards

        # Later — the mutable gate flips back to pending (invariant violation:
        # the durable card now outlives the entity's projection-eligibility).
        member.observation_pending = True
        result = sync_entity_cards(store, _Snapshot(pool=[member]))

        assert "npc:borin" not in store.cards
        assert result.evicted == 1
        assert result.evicted_ids == ["npc:borin"]
        # It is also counted as a withheld skip — the two tallies are orthogonal.
        assert result.skipped_unratified == 1
        assert result.failed == 0

    def test_eviction_is_idempotent(self) -> None:
        """A second sweep after the card is already gone evicts nothing — eviction
        is not an error on absence (the purge-needs-no-eviction case, §D5)."""
        store = EntityStore()
        member = _ratified_member("Borin")
        sync_entity_cards(store, _Snapshot(pool=[member]))
        member.observation_pending = True
        sync_entity_cards(store, _Snapshot(pool=[member]))  # evicts once

        result = sync_entity_cards(store, _Snapshot(pool=[member]))  # nothing left

        assert "npc:borin" not in store.cards
        assert result.evicted == 0
        assert result.evicted_ids == []
        assert result.skipped_unratified == 1

    def test_only_the_stranded_card_is_evicted(self) -> None:
        """Eviction is surgical: a ratified scene-mate keeps its card while the
        stranded one is removed."""
        store = EntityStore()
        borin = _ratified_member("Borin")
        mira = _ratified_member("Mira")
        sync_entity_cards(store, _Snapshot(pool=[borin, mira]))

        borin.observation_pending = True
        result = sync_entity_cards(store, _Snapshot(pool=[borin, mira]))

        assert "npc:borin" not in store.cards
        assert "npc:mira" in store.cards
        assert result.evicted == 1
        assert result.evicted_ids == ["npc:borin"]

    def test_multiple_stranded_cards_all_evicted(self) -> None:
        """The n>1 path: two ratified members indexed, then both re-marked
        pending in the same sweep. BOTH stranded cards are evicted and tallied —
        proves the eviction loop does not stop after the first card."""
        store = EntityStore()
        borin = _ratified_member("Borin")
        mira = _ratified_member("Mira")
        sync_entity_cards(store, _Snapshot(pool=[borin, mira]))

        borin.observation_pending = True
        mira.observation_pending = True
        result = sync_entity_cards(store, _Snapshot(pool=[borin, mira]))

        assert "npc:borin" not in store.cards
        assert "npc:mira" not in store.cards
        assert result.evicted == 2
        assert set(result.evicted_ids) == {"npc:borin", "npc:mira"}
        assert result.skipped_unratified == 2


class TestSlugCollisionDoesNotEvictRatifiedSibling:
    """Regression for the intra-sweep slug-collision spurious-eviction bug
    (Reviewer HIGH finding). Two *distinct* pool members whose names case-fold to
    the same ``npc:<slug>`` can coexist (world_materialization dedupes pool
    members by exact string, not casefold). A pending member must never evict the
    card a ratified slug-twin legitimately owns this sweep — the eviction guard
    relies on ratified pool cards being recorded in ``covered_ids``, mirroring the
    stateful-Npc loop. Without that recording these tests evict the live card."""

    def test_pending_member_does_not_evict_ratified_slug_twin(self) -> None:
        """A ratified ``BORIN`` (already indexed on a prior sweep) and a pending
        ``Borin`` (its case-fold twin) appear together. The ratified card must
        survive — the pending twin is withheld, not licensed to delete a
        committed sibling's card."""
        store = EntityStore()
        ratified = _ratified_member("BORIN")
        # Prior sweep indexed the ratified member's card.
        sync_entity_cards(store, _Snapshot(pool=[ratified]))
        assert "npc:borin" in store.cards

        # A distinct pending member case-folds to the SAME id this sweep.
        pending_twin = _pending_member("Borin")
        result = sync_entity_cards(store, _Snapshot(pool=[ratified, pending_twin]))

        assert "npc:borin" in store.cards  # the committed sibling's card survives
        assert result.evicted == 0
        assert result.evicted_ids == []
        # The phantom twin is still counted as withheld from the index.
        assert result.skipped_unratified == 1

    def test_slug_twin_guard_holds_regardless_of_pool_order(self) -> None:
        """The guard must not depend on the pending twin appearing after the
        ratified one in ``npc_pool`` — a fresh-store sweep with the pending twin
        FIRST also retains the ratified card (the empty store means the early
        discard is a no-op, and the ratified member re-indexes)."""
        store = EntityStore()
        pending_twin = _pending_member("Borin")
        ratified = _ratified_member("BORIN")

        result = sync_entity_cards(store, _Snapshot(pool=[pending_twin, ratified]))

        assert "npc:borin" in store.cards
        assert result.evicted == 0


# ---------------------------------------------------------------------------
# §D5 — the cases that must NOT evict (purge stays a non-event)
# ---------------------------------------------------------------------------


class TestNoSpuriousEviction:
    def test_freshly_pending_member_with_no_prior_card_evicts_nothing(self) -> None:
        """The common case: a phantom that was never indexed (§D2 gate withheld
        it from the start) has no card to evict — purge stays a non-event."""
        store = EntityStore()

        result = sync_entity_cards(store, _Snapshot(pool=[_pending_member("Mira")]))

        assert result.skipped_unratified == 1
        assert result.evicted == 0
        assert result.evicted_ids == []
        assert len(store) == 0

    def test_blank_named_pending_member_never_evicts(self) -> None:
        """A blank-named phantom could never have produced a card (its ``_slug``
        would raise). The eviction lookup must swallow that and evict nothing —
        no crash, no spurious eviction."""
        store = EntityStore()
        bad = NpcPoolMember(name="   ", drawn_from="dialogue_extraction", observation_pending=True)

        result = sync_entity_cards(store, _Snapshot(pool=[bad]))

        assert result.skipped_unratified == 1
        assert result.evicted == 0
        assert result.evicted_ids == []
        assert len(store) == 0

    def test_promoted_npc_card_is_not_evicted_by_shadowing_pending_member(self) -> None:
        """A pending pool member and the stateful ``Npc`` it promoted to share
        ``npc:<slug>``. The stateful entity is always projectable (promotion is
        the world's commitment) and legitimately owns the card this turn — the
        shadowing pending member must NOT evict it."""
        store = EntityStore()
        snapshot = _Snapshot(
            pool=[_pending_member("Borin")],
            npcs=[_npc("Borin", disposition=50, pool_origin="Borin")],
        )

        result = sync_entity_cards(store, snapshot)

        # The stateful Npc's card survives — it is the world's committed entity.
        assert "npc:borin" in store.cards
        assert [c.id for c in store.query_by_type(EntityType.NPC)] == ["npc:borin"]
        assert result.evicted == 0
        assert result.npc_count == 1
