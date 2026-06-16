"""SqliteStore — location_promotions table CRUD (Story 54-6 / ADR-109).

Pins three load-bearing contracts:

1. The schema is additive — a pre-54-6 ``save.db`` with no
   ``location_promotions`` table gains it transparently on next ``SqliteStore``
   open via the existing ``CREATE TABLE IF NOT EXISTS`` mechanism.
2. ``upsert_location_promotion`` is idempotent under the PRIMARY KEY
   ``(save_id, region_id, entity_id)`` — re-engagement updates the existing
   row rather than minting a duplicate.
3. Promotions are scoped by both ``save_id`` and ``region_id`` — promotions
   in one save/region do not leak into another.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sidequest.game.persistence import LocationPromotionRow, SqliteStore


@pytest.fixture
def store(tmp_path: Path) -> SqliteStore:
    return SqliteStore(tmp_path / "save.db")


def test_fresh_store_has_no_promotions(store: SqliteStore) -> None:
    rows = store.list_location_promotions(save_id="default", region_id="ropefoot")
    assert rows == []


def test_upsert_minted_promotion_persists(store: SqliteStore) -> None:
    row = LocationPromotionRow(
        save_id="default",
        region_id="ropefoot",
        entity_id="overturned_lamp",
        provenance="yes_and_minted",
        label="the overturned lamp",
        promoted_at_turn=12,
        promoted_canon="A lamp lies on its side near the rope, oil spreading.",
        new_tier="yes_and",
        new_binding_kind=None,
        new_binding_ref=None,
    )
    store.upsert_location_promotion(row)
    rows = store.list_location_promotions(save_id="default", region_id="ropefoot")
    assert len(rows) == 1
    assert rows[0].entity_id == "overturned_lamp"
    assert rows[0].provenance == "yes_and_minted"
    assert rows[0].label == "the overturned lamp"
    assert rows[0].promoted_at_turn == 12
    assert rows[0].promoted_canon == ("A lamp lies on its side near the rope, oil spreading.")
    assert rows[0].new_tier == "yes_and"
    assert rows[0].new_binding_kind is None
    assert rows[0].new_binding_ref is None


def test_upsert_replaces_existing_row_by_primary_key(store: SqliteStore) -> None:
    """ON CONFLICT (save_id, region_id, entity_id) DO UPDATE — re-engagement
    of the same entity updates rather than mints a duplicate row."""
    row1 = LocationPromotionRow(
        save_id="default",
        region_id="ropefoot",
        entity_id="cobwebs",
        provenance="yes_and_promoted",
        label="cobwebs",
        promoted_at_turn=5,
        promoted_canon="First touch.",
        new_tier="yes_and",
        new_binding_kind=None,
        new_binding_ref=None,
    )
    store.upsert_location_promotion(row1)

    row2 = LocationPromotionRow(
        save_id="default",
        region_id="ropefoot",
        entity_id="cobwebs",
        provenance="yes_and_promoted",
        label="cobwebs",
        promoted_at_turn=9,
        promoted_canon="Re-engaged.",
        new_tier="yes_and",
        new_binding_kind=None,
        new_binding_ref=None,
    )
    store.upsert_location_promotion(row2)

    rows = store.list_location_promotions(save_id="default", region_id="ropefoot")
    assert len(rows) == 1
    assert rows[0].promoted_at_turn == 9
    assert rows[0].promoted_canon == "Re-engaged."


def test_upsert_preserves_binding_fields(store: SqliteStore) -> None:
    row = LocationPromotionRow(
        save_id="default",
        region_id="the_glenross_arms",
        entity_id="bar",
        provenance="yes_and_promoted",
        label="the bar",
        promoted_at_turn=3,
        promoted_canon="the bar",
        new_tier="yes_and",
        new_binding_kind="location_feature",
        new_binding_ref="glenross_arms_bar",
    )
    store.upsert_location_promotion(row)
    rows = store.list_location_promotions(save_id="default", region_id="the_glenross_arms")
    assert len(rows) == 1
    assert rows[0].new_binding_kind == "location_feature"
    assert rows[0].new_binding_ref == "glenross_arms_bar"


def test_promotions_scoped_by_save_and_region(store: SqliteStore) -> None:
    """Cross-save and cross-region promotions must not leak."""
    for save_id, region_id, eid in [
        ("default", "ropefoot", "a"),
        ("default", "the_dropmouth", "b"),
        ("other_save", "ropefoot", "c"),
    ]:
        store.upsert_location_promotion(
            LocationPromotionRow(
                save_id=save_id,
                region_id=region_id,
                entity_id=eid,
                provenance="yes_and_minted",
                label=eid,
                promoted_at_turn=1,
                promoted_canon=eid,
                new_tier="yes_and",
                new_binding_kind=None,
                new_binding_ref=None,
            )
        )

    ropefoot_default = store.list_location_promotions(save_id="default", region_id="ropefoot")
    assert {r.entity_id for r in ropefoot_default} == {"a"}

    dropmouth_default = store.list_location_promotions(save_id="default", region_id="the_dropmouth")
    assert {r.entity_id for r in dropmouth_default} == {"b"}

    ropefoot_other = store.list_location_promotions(save_id="other_save", region_id="ropefoot")
    assert {r.entity_id for r in ropefoot_other} == {"c"}


def test_list_orders_by_promoted_at_turn(store: SqliteStore) -> None:
    """list_location_promotions must return rows in promoted_at_turn order
    so the resolver and the GM panel see a stable, time-ordered manifest."""
    for eid, turn in [("late", 30), ("early", 5), ("middle", 12)]:
        store.upsert_location_promotion(
            LocationPromotionRow(
                save_id="default",
                region_id="ropefoot",
                entity_id=eid,
                provenance="yes_and_minted",
                label=eid,
                promoted_at_turn=turn,
                promoted_canon=eid,
                new_tier="yes_and",
                new_binding_kind=None,
                new_binding_ref=None,
            )
        )
    rows = store.list_location_promotions(save_id="default", region_id="ropefoot")
    assert [r.entity_id for r in rows] == ["early", "middle", "late"]


def test_existing_save_without_table_migrates_transparently(tmp_path: Path) -> None:
    """A save.db that predates 54-6 must gain the table on open via
    ``CREATE TABLE IF NOT EXISTS`` — no manual migration step.

    This is the AC-1 contract: ``location_promotions`` exists after
    ``SqliteStore.open()`` against any save (fresh OR pre-54)."""
    db_path = tmp_path / "save.db"
    # Simulate a pre-54-6 save with only the game_state table.
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE game_state (id INTEGER PRIMARY KEY CHECK (id = 1), "
            "snapshot_json TEXT NOT NULL, saved_at TEXT NOT NULL)"
        )
        conn.commit()

    # Sanity: the table doesn't exist yet.
    with sqlite3.connect(db_path) as conn:
        result = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='location_promotions'"
        ).fetchone()
        assert result is None

    # Opening via SqliteStore must add the table.
    store = SqliteStore(db_path)
    rows = store.list_location_promotions(save_id="default", region_id="ropefoot")
    assert rows == []

    # And subsequent upsert against the migrated save works.
    store.upsert_location_promotion(
        LocationPromotionRow(
            save_id="default",
            region_id="ropefoot",
            entity_id="x",
            provenance="yes_and_minted",
            label="x",
            promoted_at_turn=1,
            promoted_canon="x",
            new_tier="yes_and",
            new_binding_kind=None,
            new_binding_ref=None,
        )
    )
    rows = store.list_location_promotions(save_id="default", region_id="ropefoot")
    assert len(rows) == 1
