"""Postgres location_promotions adapter tests (ADR-115 A6)."""

from __future__ import annotations

import uuid

import pytest

from sidequest.game import db_pool
from sidequest.game.pg import sessions
from sidequest.game.pg.promotions import PgLocationPromotionRow, PgPromotionStore

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _row(
    region_id: str = "region_alpha",
    entity_id: str = "gruk_the_goblin",
    *,
    promoted_at_turn: int = 3,
    provenance: str = "yes_and_minted",
    label: str = "Gruk the Goblin",
    promoted_canon: str = "yes_and_promoted",
    new_tier: str = "yes_and",
    new_binding_kind: str | None = None,
    new_binding_ref: str | None = None,
) -> PgLocationPromotionRow:
    return PgLocationPromotionRow(
        region_id=region_id,
        entity_id=entity_id,
        provenance=provenance,
        label=label,
        promoted_at_turn=promoted_at_turn,
        promoted_canon=promoted_canon,
        new_tier=new_tier,
        new_binding_kind=new_binding_kind,
        new_binding_ref=new_binding_ref,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(monkeypatch, migrated_db: str):
    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    pool = db_pool.get_pool()
    slug = f"promo_{uuid.uuid4().hex[:8]}"
    sid = sessions.ensure_session(pool, slug=slug, mode="solo", genre_slug="g", world_slug="w")
    yield PgPromotionStore(pool, session_id=sid)
    db_pool.close_pool()


# ---------------------------------------------------------------------------
# upsert + list_location_promotions round-trip
# ---------------------------------------------------------------------------


def test_upsert_then_list_round_trips(store) -> None:
    r = _row(region_id="cave_a", entity_id="goblin_1", promoted_at_turn=2)
    store.upsert_location_promotion(r)
    rows = store.list_location_promotions(region_id="cave_a")
    assert len(rows) == 1
    got = rows[0]
    assert got.region_id == "cave_a"
    assert got.entity_id == "goblin_1"
    assert got.promoted_at_turn == 2
    assert got.provenance == "yes_and_minted"
    assert got.label == "Gruk the Goblin"
    assert got.new_tier == "yes_and"
    assert got.new_binding_kind is None
    assert got.new_binding_ref is None


def test_upsert_with_binding_fields(store) -> None:
    r = _row(
        region_id="cave_b",
        entity_id="treasure_chest",
        new_binding_kind="item",
        new_binding_ref="chest_of_gold",
    )
    store.upsert_location_promotion(r)
    rows = store.list_location_promotions(region_id="cave_b")
    assert len(rows) == 1
    got = rows[0]
    assert got.new_binding_kind == "item"
    assert got.new_binding_ref == "chest_of_gold"


def test_second_upsert_updates_not_duplicates(store) -> None:
    """ON CONFLICT must UPDATE, not INSERT a second row."""
    r1 = _row(region_id="cave_c", entity_id="ent_x", promoted_at_turn=1, label="First Label")
    r2 = _row(region_id="cave_c", entity_id="ent_x", promoted_at_turn=7, label="Updated Label")
    store.upsert_location_promotion(r1)
    store.upsert_location_promotion(r2)
    rows = store.list_location_promotions(region_id="cave_c")
    assert len(rows) == 1
    assert rows[0].promoted_at_turn == 7
    assert rows[0].label == "Updated Label"


def test_list_empty_when_no_promotions(store) -> None:
    rows = store.list_location_promotions(region_id="nonexistent_region")
    assert rows == []


def test_list_scoped_to_region(store) -> None:
    """list_location_promotions returns only the requested region's rows."""
    store.upsert_location_promotion(_row(region_id="region_a", entity_id="e1"))
    store.upsert_location_promotion(_row(region_id="region_b", entity_id="e2"))
    rows_a = store.list_location_promotions(region_id="region_a")
    rows_b = store.list_location_promotions(region_id="region_b")
    assert len(rows_a) == 1 and rows_a[0].entity_id == "e1"
    assert len(rows_b) == 1 and rows_b[0].entity_id == "e2"


def test_list_ordering_by_turn_then_entity(store) -> None:
    """Rows ordered by promoted_at_turn ASC, entity_id ASC."""
    store.upsert_location_promotion(
        _row(region_id="ord_r", entity_id="z_entity", promoted_at_turn=2)
    )
    store.upsert_location_promotion(
        _row(region_id="ord_r", entity_id="a_entity", promoted_at_turn=2)
    )
    store.upsert_location_promotion(
        _row(region_id="ord_r", entity_id="m_entity", promoted_at_turn=1)
    )
    rows = store.list_location_promotions(region_id="ord_r")
    entity_ids = [r.entity_id for r in rows]
    assert entity_ids == ["m_entity", "a_entity", "z_entity"]


# ---------------------------------------------------------------------------
# Story 76-11 — batched region_ids read (one query for many regions)
# ---------------------------------------------------------------------------


def test_batched_region_ids_returns_rows_for_all_requested_regions(store) -> None:
    """A single ``region_ids=[...]`` read returns the union of all requested
    regions' rows (Story 76-11 perf fix — one round-trip instead of N)."""
    store.upsert_location_promotion(_row(region_id="reg_a", entity_id="e1"))
    store.upsert_location_promotion(_row(region_id="reg_b", entity_id="e2"))
    store.upsert_location_promotion(_row(region_id="reg_c", entity_id="e3"))
    rows = store.list_location_promotions(region_ids=["reg_a", "reg_c"])
    by_region = {(r.region_id, r.entity_id) for r in rows}
    assert by_region == {("reg_a", "e1"), ("reg_c", "e3")}, (
        f"batched read must return exactly the requested regions' rows; got {by_region!r}"
    )


def test_batched_empty_region_ids_returns_empty_without_query(store) -> None:
    """An empty ``region_ids`` list is a no-op empty result (no degenerate
    ``ANY('{}')`` round-trip)."""
    store.upsert_location_promotion(_row(region_id="reg_a", entity_id="e1"))
    assert store.list_location_promotions(region_ids=[]) == []


def test_requires_exactly_one_selector(store) -> None:
    """No Silent Fallbacks: passing neither selector, or both, raises rather
    than silently returning everything or nothing."""
    with pytest.raises(ValueError):
        store.list_location_promotions()
    with pytest.raises(ValueError):
        store.list_location_promotions(region_id="reg_a", region_ids=["reg_a"])


def test_pg_location_promotion_row_has_no_save_id() -> None:
    """PgLocationPromotionRow must NOT carry save_id (ADR-115 A6 design decision)."""
    r = _row()
    assert not hasattr(r, "save_id"), (
        "PgLocationPromotionRow must NOT carry save_id — the Postgres key is session_id. "
        "D2 consumer-lift (location_resolver.py, location_view.py) stops passing save_id."
    )


# ---------------------------------------------------------------------------
# cross-session isolation
# ---------------------------------------------------------------------------


def test_cross_session_isolation(monkeypatch, migrated_db: str) -> None:
    """Session B's PgPromotionStore must see NONE of session A's promotions."""
    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    pool = db_pool.get_pool()
    try:
        slug_a = f"promo_iso_a_{uuid.uuid4().hex[:8]}"
        slug_b = f"promo_iso_b_{uuid.uuid4().hex[:8]}"
        sid_a = sessions.ensure_session(
            pool, slug=slug_a, mode="solo", genre_slug="g", world_slug="w"
        )
        sid_b = sessions.ensure_session(
            pool, slug=slug_b, mode="solo", genre_slug="g", world_slug="w"
        )

        store_a = PgPromotionStore(pool, session_id=sid_a)
        store_b = PgPromotionStore(pool, session_id=sid_b)

        # Seed A only.
        store_a.upsert_location_promotion(_row(region_id="shared_region", entity_id="secret_npc"))

        # B sees nothing.
        rows_b = store_b.list_location_promotions(region_id="shared_region")
        assert rows_b == []

        # A still sees its own row.
        rows_a = store_a.list_location_promotions(region_id="shared_region")
        assert len(rows_a) == 1 and rows_a[0].entity_id == "secret_npc"
    finally:
        db_pool.close_pool()
