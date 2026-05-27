"""Postgres scrapbook_entries adapter tests (ADR-115 A6)."""

from __future__ import annotations

import uuid

import pytest

from sidequest.game import db_pool
from sidequest.game.pg import sessions
from sidequest.game.pg.scrapbook import PgScrapbookStore

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _entry_kwargs(turn_id: int = 1, image_url: str | None = None) -> dict:
    return dict(
        turn_id=turn_id,
        scene_title="The Dark Corridor",
        scene_type="exploration",
        location="cavern_entrance",
        image_url=image_url,
        narrative_excerpt="The torch flickers as you enter.",
        world_facts=["goblins_present", "treasure_rumoured"],
        npcs_present=[{"name": "Gruk", "role": "enemy", "disposition": "hostile"}],
        render_status="rendered",
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
    slug = f"scrap_{uuid.uuid4().hex[:8]}"
    sid = sessions.ensure_session(pool, slug=slug, mode="solo", genre_slug="g", world_slug="w")
    yield PgScrapbookStore(pool, session_id=sid)
    db_pool.close_pool()


# ---------------------------------------------------------------------------
# append_scrapbook_entry + round-trip read
# ---------------------------------------------------------------------------


def test_append_then_turn_ids_returned(store) -> None:
    store.append_scrapbook_entry(**_entry_kwargs(turn_id=3))
    ids = store.scrapbook_turn_ids(max_turn=10)
    assert 3 in ids


def test_append_multiple_turns(store) -> None:
    store.append_scrapbook_entry(**_entry_kwargs(turn_id=1))
    store.append_scrapbook_entry(**_entry_kwargs(turn_id=2))
    store.append_scrapbook_entry(**_entry_kwargs(turn_id=4))
    ids = store.scrapbook_turn_ids(max_turn=10)
    assert ids == {1, 2, 4}


def test_append_with_image_url(store) -> None:
    store.append_scrapbook_entry(**_entry_kwargs(turn_id=1, image_url="https://cdn/img.jpg"))
    url_map = store.scrapbook_image_url_map()
    assert url_map.get(1) == "https://cdn/img.jpg"


def test_append_null_image_url_not_in_url_map(store) -> None:
    store.append_scrapbook_entry(**_entry_kwargs(turn_id=1, image_url=None))
    url_map = store.scrapbook_image_url_map()
    assert 1 not in url_map


# ---------------------------------------------------------------------------
# update_scrapbook_image_url
# ---------------------------------------------------------------------------


def test_update_image_url_returns_true_when_row_exists(store) -> None:
    store.append_scrapbook_entry(**_entry_kwargs(turn_id=5, image_url=None))
    result = store.update_scrapbook_image_url(turn_id=5, image_url="https://cdn/t5.jpg")
    assert result is True


def test_update_image_url_persists(store) -> None:
    store.append_scrapbook_entry(**_entry_kwargs(turn_id=5, image_url=None))
    store.update_scrapbook_image_url(turn_id=5, image_url="https://cdn/t5.jpg")
    url_map = store.scrapbook_image_url_map()
    assert url_map.get(5) == "https://cdn/t5.jpg"


def test_update_image_url_returns_false_when_no_null_row(store) -> None:
    # Row already has an image_url — update should find no NULL row.
    store.append_scrapbook_entry(**_entry_kwargs(turn_id=7, image_url="https://cdn/already.jpg"))
    result = store.update_scrapbook_image_url(turn_id=7, image_url="https://cdn/new.jpg")
    assert result is False


def test_update_image_url_returns_false_when_no_row_at_all(store) -> None:
    result = store.update_scrapbook_image_url(turn_id=99, image_url="https://cdn/x.jpg")
    assert result is False


def test_update_image_url_targets_most_recent_null_row(store) -> None:
    """Two NULL-image rows for the same turn — update patches EXACTLY ONE (the
    most-recently-inserted), proving the LIMIT-1/DESC-id CTE targeting.

    The image_url_map keys by turn_id, so it can't distinguish one-vs-both
    patched. We assert directly against the table: exactly one row at this
    turn has a non-NULL image_url and exactly one is still NULL.
    """
    store.append_scrapbook_entry(**_entry_kwargs(turn_id=2, image_url=None))
    store.append_scrapbook_entry(**_entry_kwargs(turn_id=2, image_url=None))
    result = store.update_scrapbook_image_url(turn_id=2, image_url="https://cdn/t2.jpg")
    assert result is True

    # Raw count: exactly one row patched, exactly one still NULL.
    with store._pool.connection() as conn:
        non_null = conn.execute(
            "SELECT COUNT(*) FROM scrapbook_entries "
            "WHERE session_id = %s AND turn_id = %s AND image_url IS NOT NULL",
            (store._sid, 2),
        ).fetchone()[0]
        still_null = conn.execute(
            "SELECT COUNT(*) FROM scrapbook_entries "
            "WHERE session_id = %s AND turn_id = %s AND image_url IS NULL",
            (store._sid, 2),
        ).fetchone()[0]
    assert non_null == 1, "LIMIT-1 CTE must patch exactly one row, not both"
    assert still_null == 1, "the other NULL-image row must remain untouched"

    url_map = store.scrapbook_image_url_map()
    assert url_map.get(2) == "https://cdn/t2.jpg"


# ---------------------------------------------------------------------------
# scrapbook_turn_ids
# ---------------------------------------------------------------------------


def test_turn_ids_empty_when_no_entries(store) -> None:
    assert store.scrapbook_turn_ids(max_turn=10) == set()


def test_turn_ids_capped_at_max_turn(store) -> None:
    store.append_scrapbook_entry(**_entry_kwargs(turn_id=1))
    store.append_scrapbook_entry(**_entry_kwargs(turn_id=3))
    store.append_scrapbook_entry(**_entry_kwargs(turn_id=5))
    ids = store.scrapbook_turn_ids(max_turn=3)
    assert ids == {1, 3}
    assert 5 not in ids


def test_turn_ids_excludes_turn_zero(store) -> None:
    """A row written at turn_id=0 must be EXCLUDED by the ``turn_id >= 1`` lower bound.

    Writes a single turn_id=0 row (the adapter accepts it), then asserts the
    returned set is empty — proving the row was persisted-but-filtered, not
    merely absent.
    """
    store.append_scrapbook_entry(**_entry_kwargs(turn_id=0))
    ids = store.scrapbook_turn_ids(max_turn=10)
    assert 0 not in ids
    assert ids == set(), "turn_id=0 is the only row; the lower bound must exclude it"


def test_turn_ids_distinct_for_repeated_same_turn(store) -> None:
    store.append_scrapbook_entry(**_entry_kwargs(turn_id=2))
    store.append_scrapbook_entry(**_entry_kwargs(turn_id=2))
    ids = store.scrapbook_turn_ids(max_turn=10)
    assert ids == {2}


# ---------------------------------------------------------------------------
# scrapbook_image_url_map
# ---------------------------------------------------------------------------


def test_image_url_map_empty_when_no_entries(store) -> None:
    assert store.scrapbook_image_url_map() == {}


def test_image_url_map_skips_null_urls(store) -> None:
    store.append_scrapbook_entry(**_entry_kwargs(turn_id=1, image_url=None))
    store.append_scrapbook_entry(**_entry_kwargs(turn_id=2, image_url="https://cdn/t2.jpg"))
    url_map = store.scrapbook_image_url_map()
    assert 1 not in url_map
    assert url_map[2] == "https://cdn/t2.jpg"


# ---------------------------------------------------------------------------
# cross-session isolation
# ---------------------------------------------------------------------------


def test_cross_session_isolation(monkeypatch, migrated_db: str) -> None:
    """Session B's PgScrapbookStore must see NONE of session A's rows."""
    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    pool = db_pool.get_pool()
    try:
        slug_a = f"scrap_iso_a_{uuid.uuid4().hex[:8]}"
        slug_b = f"scrap_iso_b_{uuid.uuid4().hex[:8]}"
        sid_a = sessions.ensure_session(
            pool, slug=slug_a, mode="solo", genre_slug="g", world_slug="w"
        )
        sid_b = sessions.ensure_session(
            pool, slug=slug_b, mode="solo", genre_slug="g", world_slug="w"
        )

        store_a = PgScrapbookStore(pool, session_id=sid_a)
        store_b = PgScrapbookStore(pool, session_id=sid_b)

        # Seed A only.
        store_a.append_scrapbook_entry(**_entry_kwargs(turn_id=1, image_url="https://cdn/a.jpg"))
        store_a.append_scrapbook_entry(**_entry_kwargs(turn_id=2, image_url=None))

        # B sees nothing.
        assert store_b.scrapbook_turn_ids(max_turn=10) == set()
        assert store_b.scrapbook_image_url_map() == {}
        assert store_b.update_scrapbook_image_url(turn_id=1, image_url="https://cdn/b.jpg") is False

        # A still sees its own rows (filter didn't over-prune).
        ids_a = store_a.scrapbook_turn_ids(max_turn=10)
        assert 1 in ids_a and 2 in ids_a
        assert store_a.scrapbook_image_url_map() == {1: "https://cdn/a.jpg"}
    finally:
        db_pool.close_pool()
