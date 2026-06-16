"""RED-phase tests for Story 75-6 — entity card sync / reproject (ADR-118 §D2).

Unit tier. Pins the pure sync function and the ``EntityStore`` upsert path
that 75-6 introduces to close the mutation loop:

- 75-4 (merged) gave us ``EntityCard`` + projectors + the typed ``EntityStore``.
- 75-5 (merged) gave us ``retrieve_turn_context`` that reads the store each turn.
- The store is **never populated or refreshed today** — ``sync_entity_cards`` is
  both the first seeder and the per-turn reproject.

Contract sources (spec-authority order):
- ``.session/75-6-session.md`` (Implementation Contract + Test Design Guardrails)
- ``sprint/context/context-story-75-6.md`` (test-strategy lens)
- ADR-118 §D2 (dirty-flag reproject), §D3 (deterministic projection)

Reuse discipline (SOUL *Don't Reinvent*): reproject calls the live
``project_npc_card`` projector and the live embedding-worker contract; the embed
read-back exercises the same daemon/cosine path lore already uses.

INTENTIONALLY RED until 75-6 lands — ``sidequest.game.entity_sync``,
``EntityStore.upsert``, and ``sidequest.game.entity_embedding`` do not exist yet.
"""

from __future__ import annotations

import pytest

from sidequest.game.disposition import Disposition
from sidequest.game.entity_card import EntityType, project_npc_card
from sidequest.game.entity_store import EntityStore
from sidequest.game.npc_pool import NpcPoolMember

# ---------------------------------------------------------------------------
# Fixtures — deterministic NPC pool members (the source the floor reads as
# off-stage and the fill retrieves by similarity).
# ---------------------------------------------------------------------------


def _member(name: str, *, disposition: int = 0, role: str = "smith") -> NpcPoolMember:
    return NpcPoolMember(
        name=name,
        role=role,
        pronouns="they/them",
        drawn_from="world_authored",
        disposition=Disposition(disposition),
    )


class _SnapshotStub:
    """Minimal stand-in carrying the entity sources ``sync_entity_cards`` reads.

    The real ``GameSnapshot`` carries ``npc_pool`` (NpcPoolMember) and ``npcs``
    (stateful Npc). 75-6's NPC sync source is the pool; this stub exposes the
    same attribute names so the unit tests do not need a full snapshot build.
    """

    def __init__(self, pool: list[NpcPoolMember]) -> None:
        self.npc_pool = pool
        self.npcs: list = []


# ---------------------------------------------------------------------------
# EntityStore.upsert — the reproject-safe write path (central 75-6 contract)
# ---------------------------------------------------------------------------


class TestEntityStoreUpsert:
    def test_upsert_inserts_a_new_card_and_reports_changed(self) -> None:
        """First sync of an entity: the card is added and upsert reports a
        change (so the caller counts it as reprojected)."""
        store = EntityStore()
        card = project_npc_card(_member("Borin"))

        changed = store.upsert(card)

        assert changed is True
        assert store.query_by_type(EntityType.NPC) == [card]

    def test_upsert_of_unchanged_content_is_a_noop(self) -> None:
        """Determinism payoff (ADR-118 §D2): re-projecting unchanged state and
        upserting it must NOT churn the card — no re-arm, embedding preserved."""
        store = EntityStore()
        first = project_npc_card(_member("Borin"))
        store.upsert(first)
        # Simulate the embed worker having run: embedding present, flag cleared.
        stored = store.cards[first.id]
        stored.embedding = [0.1, 0.2]
        stored.embedding_pending = False

        again = project_npc_card(_member("Borin"))
        changed = store.upsert(again)

        assert changed is False
        assert store.cards[first.id].embedding == [0.1, 0.2]
        assert store.cards[first.id].embedding_pending is False

    def test_upsert_does_not_raise_duplicate_on_existing_id(self) -> None:
        """``add`` raises DuplicateEntityId on a repeat id; ``upsert`` is the
        reproject-safe path that must NOT — else every reproject crashes the
        turn."""
        store = EntityStore()
        store.upsert(project_npc_card(_member("Borin")))
        # Must not raise.
        store.upsert(project_npc_card(_member("Borin")))
        assert len(store.query_by_type(EntityType.NPC)) == 1

    def test_upsert_of_changed_content_replaces_and_rearms(self) -> None:
        """A band-crossing disposition change reprojects new content; upsert
        replaces the stored card and re-arms ``embedding_pending`` so the worker
        re-embeds the changed cast (ADR-118 §D2)."""
        store = EntityStore()
        neutral = project_npc_card(_member("Borin", disposition=0))
        store.upsert(neutral)
        stored = store.cards[neutral.id]
        stored.embedding = [0.9, 0.0]
        stored.embedding_pending = False

        friendly = project_npc_card(_member("Borin", disposition=50))
        # Sanity: the projection actually changed (neutral -> friendly band).
        assert friendly.content != neutral.content
        assert friendly.content.endswith("friendly")

        changed = store.upsert(friendly)

        assert changed is True
        assert store.cards[neutral.id].content == friendly.content
        assert store.cards[neutral.id].embedding_pending is True


# ---------------------------------------------------------------------------
# sync_entity_cards — the per-turn pure sweep (mirror accrete_facts_to_lore)
# ---------------------------------------------------------------------------


class TestSyncEntityCards:
    def test_sync_populates_empty_store_from_pool(self) -> None:
        """The store starts empty; sync is the seeder. After one sweep every
        pool member has a namespaced NPC card."""
        from sidequest.game.entity_sync import sync_entity_cards

        store = EntityStore()
        snapshot = _SnapshotStub([_member("Borin"), _member("Mira")])

        result = sync_entity_cards(store, snapshot)

        npc_ids = {c.id for c in store.query_by_type(EntityType.NPC)}
        assert npc_ids == {"npc:borin", "npc:mira"}
        assert result.reprojected == 2
        assert result.npc_count == 2
        assert result.failed == 0
        assert result.outcome == "success"

    def test_resync_unchanged_roster_is_a_clean_skip(self) -> None:
        """Zero-byte-leak: a turn where nothing changed reprojects nothing,
        re-arms nothing, and reports ``outcome='skipped'``."""
        from sidequest.game.entity_sync import sync_entity_cards

        store = EntityStore()
        snapshot = _SnapshotStub([_member("Borin")])
        sync_entity_cards(store, snapshot)
        # Pretend the worker embedded the card.
        for card in store.query_by_type(EntityType.NPC):
            store.cards[card.id].embedding = [1.0, 0.0]
            store.cards[card.id].embedding_pending = False

        result = sync_entity_cards(store, snapshot)

        assert result.reprojected == 0
        assert result.unchanged == 1
        assert result.outcome == "skipped"
        # The embedded card was not disturbed.
        assert store.cards["npc:borin"].embedding_pending is False

    def test_sync_reprojects_only_the_mutated_entity(self) -> None:
        """A mutation to one NPC re-arms exactly that card; the unchanged
        sibling is left embedded (no blanket re-embed)."""
        from sidequest.game.entity_sync import sync_entity_cards

        store = EntityStore()
        borin = _member("Borin", disposition=0)
        mira = _member("Mira", disposition=0)
        snapshot = _SnapshotStub([borin, mira])
        sync_entity_cards(store, snapshot)
        for card in store.query_by_type(EntityType.NPC):
            store.cards[card.id].embedding = [1.0, 0.0]
            store.cards[card.id].embedding_pending = False

        # Borin's attitude crosses neutral -> friendly; Mira is untouched.
        borin.disposition = Disposition(50)
        result = sync_entity_cards(store, snapshot)

        assert result.reprojected == 1
        assert result.unchanged == 1
        assert store.cards["npc:borin"].embedding_pending is True
        assert store.cards["npc:mira"].embedding_pending is False

    def test_within_band_wiggle_does_not_rearm(self) -> None:
        """A raw-int disposition change that stays inside the same attitude band
        projects identical content -> no reproject (the projector embeds the
        band, not the int)."""
        from sidequest.game.entity_sync import sync_entity_cards

        store = EntityStore()
        borin = _member("Borin", disposition=0)
        snapshot = _SnapshotStub([borin])
        sync_entity_cards(store, snapshot)
        store.cards["npc:borin"].embedding = [1.0, 0.0]
        store.cards["npc:borin"].embedding_pending = False

        borin.disposition = Disposition(5)  # still neutral (band is ]-10, 10[)
        result = sync_entity_cards(store, snapshot)

        assert result.reprojected == 0
        assert result.unchanged == 1
        assert store.cards["npc:borin"].embedding_pending is False

    def test_unprojectable_entity_fails_loud_no_stub_card(self) -> None:
        """No Silent Fallbacks: a member the projector rejects (blank name) is
        counted as a failure, recorded in ``failed_refs``, and emits NO card.
        ``outcome`` degrades to 'partial' — never a silent success."""
        from sidequest.game.entity_sync import sync_entity_cards

        store = EntityStore()
        good = _member("Borin")
        bad = NpcPoolMember(name="   ", drawn_from="world_authored")  # slug() raises
        snapshot = _SnapshotStub([good, bad])

        result = sync_entity_cards(store, snapshot)

        assert result.reprojected == 1
        assert result.failed == 1
        assert result.failed_refs == ["   "]
        assert result.outcome == "partial"
        # The good card landed; no stub card was minted for the bad one.
        assert [c.id for c in store.query_by_type(EntityType.NPC)] == ["npc:borin"]

    def test_empty_snapshot_is_skipped_not_an_empty_batch(self) -> None:
        from sidequest.game.entity_sync import sync_entity_cards

        store = EntityStore()
        result = sync_entity_cards(store, _SnapshotStub([]))

        assert result.reprojected == 0
        assert result.failed == 0
        assert result.outcome == "skipped"
        assert len(store) == 0

    def test_sync_is_idempotent_within_a_single_turn(self) -> None:
        """Running the sweep twice back-to-back (same state) does no second
        round of work — the second call is all-unchanged."""
        from sidequest.game.entity_sync import sync_entity_cards

        store = EntityStore()
        snapshot = _SnapshotStub([_member("Borin")])
        sync_entity_cards(store, snapshot)
        second = sync_entity_cards(store, snapshot)

        assert second.reprojected == 0
        assert second.unchanged == 1
        assert len(store.query_by_type(EntityType.NPC)) == 1


# ---------------------------------------------------------------------------
# Embed read-back — the synced cards must be drained by the live worker path
# (the gap: today the embed worker only drains the lore store). Mirror of the
# lore AC4 test, which calls the embed function directly with a fake client.
# ---------------------------------------------------------------------------


class _FakeClient:
    """Returns a constant embedding so query_by_similarity scores cosine=1.0."""

    def __init__(self, embedding: list[float] | None = None) -> None:
        self._embedding = embedding or [1.0, 0.0]

    def is_available(self) -> bool:
        return True

    async def embed(self, text: str) -> dict:
        return {"embedding": list(self._embedding), "model": "fake", "latency_ms": 1}


@pytest.mark.asyncio
async def test_synced_card_is_embeddable_and_then_retrievable() -> None:
    """Sync → embed (the entity drain) → query_by_similarity hits the card.
    Proves reprojected cards become similarity-searchable, not stuck pending."""
    from sidequest.game.entity_embedding import embed_pending_entity_cards
    from sidequest.game.entity_sync import sync_entity_cards

    store = EntityStore()
    sync_entity_cards(store, _SnapshotStub([_member("Borin")]))
    assert store.pending_embedding_ids() == ["npc:borin"]

    result = await embed_pending_entity_cards(store, client=_FakeClient([1.0, 0.0]))

    assert result.embedded == 1
    assert store.cards["npc:borin"].embedding_pending is False
    hits = store.query_by_similarity([1.0, 0.0], top_k=5)
    assert "npc:borin" in [card.id for _score, card in hits]
