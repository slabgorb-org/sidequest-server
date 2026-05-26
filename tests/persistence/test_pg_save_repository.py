"""PgSaveRepository composite store tests (ADR-115 A7).

Exercises the full unified surface — events, snapshots, narrative, scrapbook,
promotions — through ONE object, covering:

  1. Protocol conformance (isinstance check).
  2. transaction() atomicity: event + projection commit together.
  3. Rollback: append inside a raised transaction leaves latest_event_seq unchanged.
  4. save/load snapshot round-trip.
  5. append_narrative / recent_narrative round-trip.
  6. Scrapbook append + image-url backfill.
  7. Location promotion upsert + list.
  8. Cross-session isolation — two repos on one pool see only their own rows.
"""

from __future__ import annotations

import uuid

import pytest

from sidequest.game import db_pool
from sidequest.game.pg.promotions import PgLocationPromotionRow
from sidequest.game.pg.save_repository import PgSaveRepository
from sidequest.game.projection_filter import FilterDecision
from sidequest.game.repository import SaveRepository
from sidequest.game.session import GameSnapshot, NarrativeEntry


def _slug() -> str:
    return f"sq_a7_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def repo(monkeypatch, migrated_db: str) -> PgSaveRepository:
    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    pool = db_pool.get_pool()
    r = PgSaveRepository.for_slug(
        pool,
        slug=_slug(),
        mode="solo",
        genre_slug="test_genre",
        world_slug="test_world",
    )
    yield r
    db_pool.close_pool()


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_pg_save_repository_satisfies_protocol(repo: PgSaveRepository) -> None:
    """PgSaveRepository must pass isinstance(repo, SaveRepository)."""
    assert isinstance(repo, SaveRepository)


# ---------------------------------------------------------------------------
# transaction() — atomic commit
# ---------------------------------------------------------------------------


def test_transaction_commits_event_and_projection_atomically(repo: PgSaveRepository) -> None:
    """Event + projection written in one transaction both persist on clean exit."""
    with repo.transaction() as tx:
        ev = tx.append_event(kind="NARRATION", payload_json='{"msg":"hi"}')
        tx.write_projection(
            event_seq=ev.seq,
            player_id="p1",
            decision=FilterDecision(include=True, payload_json='{"msg":"hi"}'),
        )

    assert repo.latest_event_seq() == ev.seq
    cached = repo.read_projection_since(player_id="p1", since_seq=0)
    assert len(cached) == 1
    assert cached[0].event_seq == ev.seq
    assert cached[0].include is True
    # write_projection persists payload only when include is True — verify round-trip.
    assert cached[0].payload_json == '{"msg":"hi"}'


# ---------------------------------------------------------------------------
# transaction() — rollback on exception
# ---------------------------------------------------------------------------


def test_transaction_rolls_back_on_exception(repo: PgSaveRepository) -> None:
    """If the transaction block raises, the appended event must NOT persist."""
    before_seq = repo.latest_event_seq()

    with pytest.raises(RuntimeError, match="boom"), repo.transaction() as tx:
        tx.append_event(kind="DOOMED", payload_json="{}")
        raise RuntimeError("boom")

    assert repo.latest_event_seq() == before_seq


# ---------------------------------------------------------------------------
# save / load snapshot
# ---------------------------------------------------------------------------


def test_save_load_snapshot_roundtrip(repo: PgSaveRepository) -> None:
    snap = GameSnapshot(
        genre_slug="test_genre",
        world_slug="test_world",
        atmosphere="eerie silence",
    )
    repo.save(snap)
    loaded = repo.load()
    assert loaded is not None
    assert loaded.snapshot.atmosphere == "eerie silence"
    assert loaded.snapshot.genre_slug == "test_genre"


def test_load_returns_none_when_no_snapshot(repo: PgSaveRepository) -> None:
    assert repo.load() is None


# ---------------------------------------------------------------------------
# append_narrative / recent_narrative
# ---------------------------------------------------------------------------


def test_append_and_recent_narrative(repo: PgSaveRepository) -> None:
    entry = NarrativeEntry(timestamp=0, round=1, author="narrator", content="A dark cave.", tags=[])
    repo.append_narrative(entry)
    rows = repo.recent_narrative(5)
    assert len(rows) == 1
    assert rows[0].content == "A dark cave."
    assert rows[0].round == 1


# ---------------------------------------------------------------------------
# Scrapbook
# ---------------------------------------------------------------------------


def test_scrapbook_append_and_image_url_backfill(repo: PgSaveRepository) -> None:
    repo.append_scrapbook_entry(
        turn_id=7,
        scene_title="The Vault",
        scene_type="interior",
        location="vault",
        image_url=None,
        narrative_excerpt="They opened the vault.",
        world_facts=["gold inside"],
        npcs_present=[],
        render_status="pending",
    )
    assert 7 in repo.scrapbook_turn_ids(max_turn=10)
    updated = repo.update_scrapbook_image_url(turn_id=7, image_url="https://cdn.example/img.png")
    assert updated is True
    url_map = repo.scrapbook_image_url_map()
    assert url_map.get(7) == "https://cdn.example/img.png"


# ---------------------------------------------------------------------------
# Location promotions
# ---------------------------------------------------------------------------


def test_upsert_and_list_location_promotion(repo: PgSaveRepository) -> None:
    promo = PgLocationPromotionRow(
        region_id="region_1",
        entity_id="npc_zara",
        provenance="yes_and_minted",
        label="Zara the Merchant",
        promoted_at_turn=3,
        promoted_canon="Zara appeared at the bazaar.",
        new_tier="yes_and",
        new_binding_kind=None,
        new_binding_ref=None,
    )
    repo.upsert_location_promotion(promo)
    rows = repo.list_location_promotions(region_id="region_1")
    assert len(rows) == 1
    assert rows[0].entity_id == "npc_zara"
    assert rows[0].label == "Zara the Merchant"


# ---------------------------------------------------------------------------
# Cross-session isolation — the load-bearing multi-tenant invariant
# ---------------------------------------------------------------------------


def test_cross_session_isolation(repo: PgSaveRepository) -> None:
    """Two repos on different slugs share a pool+table but see ONLY their own rows.

    ``repo`` (fixture) is session A. Build session B on the same pool with a
    distinct uuid slug. Write through A across EVERY sub-store; assert B sees
    none of it, and A still sees its own.
    """
    repo_a = repo
    repo_b = PgSaveRepository.for_slug(
        repo_a._pool,
        slug=_slug(),
        mode="solo",
        genre_slug="test_genre",
        world_slug="test_world",
    )

    # --- Write through A across every sub-store --------------------------------
    with repo_a.transaction() as tx:
        ev = tx.append_event(kind="NARRATION", payload_json='{"a":1}')
        tx.write_projection(
            event_seq=ev.seq,
            player_id="pA",
            decision=FilterDecision(include=True, payload_json='{"a":1}'),
        )
    repo_a.append_narrative(
        NarrativeEntry(timestamp=0, round=1, author="narrator", content="A's tale", tags=[])
    )
    repo_a.save(
        GameSnapshot(genre_slug="test_genre", world_slug="test_world", atmosphere="A's world")
    )
    repo_a.append_scrapbook_entry(
        turn_id=1,
        scene_title="A scene",
        scene_type="interior",
        location="here",
        image_url="https://cdn.example/a.png",
        narrative_excerpt="A excerpt",
        world_facts=[],
        npcs_present=[],
        render_status="done",
    )
    repo_a.upsert_location_promotion(
        PgLocationPromotionRow(
            region_id="rA",
            entity_id="eA",
            provenance="yes_and_minted",
            label="A label",
            promoted_at_turn=1,
            promoted_canon="A canon",
            new_tier="yes_and",
            new_binding_kind=None,
            new_binding_ref=None,
        )
    )

    # --- B sees NONE of A's writes --------------------------------------------
    assert repo_b.latest_event_seq() == 0
    assert repo_b.read_events_since(since_seq=0) == []
    assert repo_b.read_projection_since(player_id="pA", since_seq=0) == []
    assert repo_b.recent_narrative(10) == []
    assert repo_b.max_narrative_round() == 0
    assert repo_b.load() is None
    assert repo_b.scrapbook_turn_ids(max_turn=100) == set()
    assert repo_b.scrapbook_image_url_map() == {}
    assert repo_b.list_location_promotions(region_id="rA") == []

    # --- A still sees its own writes ------------------------------------------
    assert repo_a.latest_event_seq() == ev.seq
    assert [e.kind for e in repo_a.read_events_since(since_seq=0)] == ["NARRATION"]
    assert len(repo_a.recent_narrative(10)) == 1
    loaded_a = repo_a.load()
    assert loaded_a is not None and loaded_a.snapshot.atmosphere == "A's world"
    assert repo_a.scrapbook_image_url_map().get(1) == "https://cdn.example/a.png"
    assert len(repo_a.list_location_promotions(region_id="rA")) == 1
