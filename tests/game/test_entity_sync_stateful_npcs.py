"""RED-phase tests for Story 76-6 — sync stateful ``snapshot.npcs`` into the
universal-retrieval entity index (epic 76, ADR-118 §D2/§D3 source coverage).

The index is **NPC-pool-only today**: ``project_npc_card`` accepts only
``NpcPoolMember`` and ``sync_entity_cards`` iterates only ``snapshot.npc_pool``.
Scene-stateful NPCs (``snapshot.npcs`` — the promoted/narrator-invented ``Npc``
entities that carry the mechanical state) are therefore never projected and
never retrievable. This story closes that gap.

Contract sources (spec-authority order):
- ``.session/76-6-session.md`` (SM scope guardrails)
- ``sprint/context/context-story-76-6.md`` (ACs 1-4)
- ADR-118 §D3 (deterministic projection, dual-rep sync discipline)

Reuse discipline (SOUL *Don't Reinvent*): the stateful path must go through the
SAME ``project_npc_card`` projector, the SAME ``EntityStore``, and the SAME
``EntitySyncResult`` counters as the live pool path — no parallel machinery.

INTENTIONALLY RED until 76-6 lands:
- ``project_npc_card`` does not yet accept an ``Npc`` (reads ``member.name`` /
  ``member.role`` — an ``Npc`` carries its name at ``core.name`` and has no
  ``.role``), so projecting one raises ``AttributeError``.
- ``sync_entity_cards`` ignores ``snapshot.npcs`` entirely, so stateful NPCs
  never reach the store and ``npc_count`` never reflects them.
"""

from __future__ import annotations

from sidequest.game.creature_core import CreatureCore
from sidequest.game.disposition import Disposition
from sidequest.game.entity_card import EntityType, project_npc_card
from sidequest.game.entity_store import EntityStore
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.game.session import Npc

# ---------------------------------------------------------------------------
# Fixtures — pool members (identity-only) and stateful Npcs (mechanical).
# ---------------------------------------------------------------------------


def _member(name: str, *, disposition: int = 0, role: str = "smith") -> NpcPoolMember:
    return NpcPoolMember(
        name=name,
        role=role,
        pronouns="they/them",
        drawn_from="world_authored",
        disposition=Disposition(disposition),
    )


def _npc(name: str, *, disposition: int = 0, pool_origin: str | None = None) -> Npc:
    """A minimal scene-stateful ``Npc`` — the entity that lives in
    ``snapshot.npcs`` after a pool member is promoted (or a narrator invents
    one). Name is carried at ``core.name``, NOT at the top level."""
    return Npc(
        core=CreatureCore(name=name, description="A wandering smith.", personality="Gruff."),
        disposition=Disposition(disposition),
        pronouns="they/them",
        pool_origin=pool_origin,
    )


class _Snapshot:
    """Stand-in carrying the two entity sources ``sync_entity_cards`` reads.

    Mirrors the real ``GameSnapshot`` attribute names: ``npc_pool`` (identity
    scaffolding) and ``npcs`` (stateful mechanical entities). 75-6 read only the
    former; 76-6 must read both.
    """

    def __init__(
        self,
        *,
        pool: list[NpcPoolMember] | None = None,
        npcs: list[Npc] | None = None,
    ) -> None:
        self.npc_pool = pool or []
        self.npcs = npcs or []


# ---------------------------------------------------------------------------
# AC1 — project_npc_card accepts a stateful Npc and returns a valid card
# ---------------------------------------------------------------------------


class TestProjectStatefulNpc:
    def test_projects_a_stateful_npc_into_a_namespaced_card(self) -> None:
        """AC1: a stateful ``Npc`` projects to a well-formed NPC card whose id
        and ref derive from ``core.name`` — the same namespace pool members use,
        so the two sources share one id space (the basis for dedup)."""
        card = project_npc_card(_npc("Borin"))

        assert card.entity_type == EntityType.NPC
        assert card.id == "npc:borin"
        assert card.entity_ref == "Borin"
        assert "Borin" in card.content

    def test_stateful_projection_is_deterministic(self) -> None:
        """ADR-118 §D3: identical state must project identical content — the
        75-6 reproject treats 'changed' as a content comparison, so a
        non-deterministic stateful projection would re-embed every turn."""
        first = project_npc_card(_npc("Borin", disposition=0))
        second = project_npc_card(_npc("Borin", disposition=0))

        assert first.content == second.content
        assert first.id == second.id

    def test_stateful_content_keys_on_attitude_band_not_raw_int(self) -> None:
        """Content keys on the disposition *band* (so retrieval keys on
        relationship and reproject fires on band crossings), mirroring the pool
        projector. Neutral and friendly Borin must project different content."""
        neutral = project_npc_card(_npc("Borin", disposition=0))
        friendly = project_npc_card(_npc("Borin", disposition=50))

        assert neutral.content != friendly.content
        assert friendly.content.endswith("friendly")


# ---------------------------------------------------------------------------
# AC2 — sync_entity_cards iterates snapshot.npcs
# ---------------------------------------------------------------------------


class TestSyncIndexesStatefulNpcs:
    def test_sync_indexes_a_stateful_npc_from_empty_pool(self) -> None:
        """AC2: a snapshot whose only cast is a stateful ``Npc`` (empty pool)
        still lands an NPC card and counts it. Today this returns nothing —
        ``snapshot.npcs`` is never read."""
        from sidequest.game.entity_sync import sync_entity_cards

        store = EntityStore()
        snapshot = _Snapshot(npcs=[_npc("Borin")])

        result = sync_entity_cards(store, snapshot)

        assert [c.id for c in store.query_by_type(EntityType.NPC)] == ["npc:borin"]
        assert result.npc_count == 1
        assert result.reprojected == 1
        assert result.failed == 0
        assert result.outcome == "success"

    def test_narrator_invented_npc_is_indexed(self) -> None:
        """A narrator-invented stateful NPC (``pool_origin=None``, never in the
        pool) is indexed like any other — relevance, not provenance, governs
        recall (ADR-014 Living World)."""
        from sidequest.game.entity_sync import sync_entity_cards

        store = EntityStore()
        snapshot = _Snapshot(npcs=[_npc("Zed", pool_origin=None)])

        result = sync_entity_cards(store, snapshot)

        assert "npc:zed" in {c.id for c in store.query_by_type(EntityType.NPC)}
        assert result.npc_count == 1

    def test_pool_and_distinct_stateful_npcs_both_indexed(self) -> None:
        """AC3: a scene with a pool member AND an unrelated stateful NPC indexes
        BOTH — the two sources are unioned, not one-or-the-other."""
        from sidequest.game.entity_sync import sync_entity_cards

        store = EntityStore()
        snapshot = _Snapshot(pool=[_member("Alice")], npcs=[_npc("Bob")])

        result = sync_entity_cards(store, snapshot)

        assert {c.id for c in store.query_by_type(EntityType.NPC)} == {
            "npc:alice",
            "npc:bob",
        }
        assert result.npc_count == 2


# ---------------------------------------------------------------------------
# AC3 — dedup: a promoted Npc must not double-index against its pool origin
# ---------------------------------------------------------------------------


class TestPromotedNpcDedup:
    def test_promoted_npc_dedups_against_its_pool_origin(self) -> None:
        """SM guardrail: a promoted ``Npc`` and the ``NpcPoolMember`` it was
        promoted from share an id — they must produce ONE card, not two, and
        ``npc_count`` must report one unique NPC (a naive union double-counts).
        The richer stateful entity wins."""
        from sidequest.game.entity_sync import sync_entity_cards

        store = EntityStore()
        # Pool member is neutral; the promoted stateful Npc has moved to friendly.
        snapshot = _Snapshot(
            pool=[_member("Borin", disposition=0)],
            npcs=[_npc("Borin", disposition=50, pool_origin="Borin")],
        )

        result = sync_entity_cards(store, snapshot)

        npc_cards = store.query_by_type(EntityType.NPC)
        assert [c.id for c in npc_cards] == ["npc:borin"]
        # The stateful projection won — the card reflects the Npc's friendly band.
        assert npc_cards[0].content.endswith("friendly")
        # Honest count: one unique NPC, not two.
        assert result.npc_count == 1
        # Story 84-3 (WI-4): the non-neutral NPC also yields a relationship card, so
        # the sweep reprojects two cards (npc:borin + rel:borin). The dedup invariant
        # this test guards is npc_count == 1 (one unique NPC, not double-counted).
        assert result.relationship_count == 1
        assert result.reprojected == 2

    def test_stateful_band_change_reprojects_through_sync(self) -> None:
        """The stateful path participates in the dirty-flag reproject: a band
        crossing on a stateful NPC re-arms exactly its card on the next sweep."""
        from sidequest.game.entity_sync import sync_entity_cards

        store = EntityStore()
        borin = _npc("Borin", disposition=0)
        snapshot = _Snapshot(npcs=[borin])
        sync_entity_cards(store, snapshot)
        store.cards["npc:borin"].embedding = [1.0, 0.0]
        store.cards["npc:borin"].embedding_pending = False

        # Cross neutral -> friendly and re-sync.
        borin.disposition = Disposition(50)
        result = sync_entity_cards(store, snapshot)

        # Story 84-3 (WI-4): the band crossing re-arms BOTH the npc card (content
        # changed) AND the now-projected relationship card (Borin crossed into a
        # non-neutral band, so a rel:borin card is born this sweep). The invariant
        # this test guards — the npc card's embedding_pending flips True — is
        # asserted directly below.
        assert result.reprojected == 2
        assert store.cards["npc:borin"].embedding_pending is True


# ---------------------------------------------------------------------------
# No Silent Fallbacks — for stateful NPCs the guarantee is upstream
# ---------------------------------------------------------------------------


class TestStatefulNoSilentFallbacks:
    """The SM guardrail asks for "No Silent Fallbacks on an unprojectable Npc".

    Investigation during test design (logged as a TEA deviation): a stateful
    ``Npc`` is **structurally always projectable**. ``CreatureCore`` validates
    ``name`` non-blank at construction, and the NPC projector derives its id and
    content from ``core.name`` — so the ``_slug`` blank-name failure path that
    exists for ``NpcPoolMember`` is UNREACHABLE for an ``Npc``. The
    No-Silent-Fallbacks intent is therefore enforced one layer up, at the model
    boundary, rather than as a sync-level failure count.
    """

    def test_creature_core_name_validator_is_the_upstream_guard(self) -> None:
        """The guarantee that makes a stateful NPC always projectable: a blank
        ``CreatureCore`` name is rejected at construction (fail loud), so no
        blank-named ``Npc`` can ever reach the projector to be silently stubbed."""
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            CreatureCore(name="   ", description="X.", personality="Y.")

    def test_minimal_valid_stateful_npc_always_projects(self) -> None:
        """Any constructable ``Npc`` projects without error and lands a card —
        the failure path is upstream (the validator above), never a silent stub
        here."""
        from sidequest.game.entity_sync import sync_entity_cards

        store = EntityStore()
        result = sync_entity_cards(store, _Snapshot(npcs=[_npc("Borin")]))

        assert result.failed == 0
        assert [c.id for c in store.query_by_type(EntityType.NPC)] == ["npc:borin"]
