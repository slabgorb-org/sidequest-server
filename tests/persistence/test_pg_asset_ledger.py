"""RED tests for Story 65-2 — Postgres asset_ledger table + PgAssetLedgerStore.

Targets not-yet-existing code:
  - Alembic migration adding the ``asset_ledger`` table (AC1)
  - ``sidequest.game.pg.asset_ledger.PgAssetLedgerStore`` (AC2)

Substrate is PostgreSQL per ADR-115 (the original story prose said SqliteStore —
see context-story-65-2.md "SUBSTRATE CORRECTION"). The content sha256 already
lives inside ``r2_key`` (``artifacts/<world>/<session>/<kind>/<sha256>.<ext>``),
so there is no ``md5``/``size_bytes`` column — see the Content-hash guardrail.

Mirrors tests/persistence/test_pg_scrapbook.py for fixtures + isolation shape.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest

from sidequest.game import db_pool
from sidequest.game.pg import sessions

# The module under test does not exist yet — import inside fixtures/tests so
# collection still runs and the failure is a clear ImportError at call time.


def _ledger_kwargs(
    r2_key: str = "artifacts/flickering_reach/42/portrait/abc123.png",
    asset_type: str = "portrait",
    entity_ref: str = "gruk_the_unwashed",
    created_turn: int = 1,
) -> dict:
    return dict(
        r2_key=r2_key,
        asset_type=asset_type,
        entity_ref=entity_ref,
        created_turn=created_turn,
    )


@pytest.fixture
def store(monkeypatch, migrated_db: str):
    from sidequest.game.pg.asset_ledger import PgAssetLedgerStore

    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    pool = db_pool.get_pool()
    slug = f"ledger_{uuid.uuid4().hex[:8]}"
    sid = sessions.ensure_session(pool, slug=slug, mode="solo", genre_slug="g", world_slug="w")
    yield PgAssetLedgerStore(pool, session_id=sid)
    db_pool.close_pool()


# ---------------------------------------------------------------------------
# AC1 — table exists via migration with the expected shape
# ---------------------------------------------------------------------------


def test_asset_ledger_table_exists_after_migration(migrated_db: str) -> None:
    """The migration creates ``asset_ledger`` with the expected columns."""
    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(plain) as conn:
        cols = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'asset_ledger'"
            ).fetchall()
        }
    assert {"r2_key", "asset_type", "entity_ref", "created_turn", "session_id", "created_at"} <= cols
    # md5/size_bytes were dropped (hash is in the key) — they must NOT exist.
    assert "md5" not in cols, "md5 column was dropped per Content-hash reconciliation"
    assert "size_bytes" not in cols, "size_bytes column was dropped — no resume consumer"


def test_r2_key_is_primary_key(migrated_db: str) -> None:
    """``r2_key`` is the primary key — a second insert of the same key conflicts."""
    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    db_pool.close_pool()
    import os

    os.environ["SIDEQUEST_DATABASE_URL"] = plain
    pool = db_pool.get_pool()
    try:
        sid = sessions.ensure_session(
            pool, slug=f"pk_{uuid.uuid4().hex[:8]}", mode="solo", genre_slug="g", world_slug="w"
        )
        with pool.connection() as conn:
            conn.execute(
                "INSERT INTO asset_ledger "
                "(r2_key, asset_type, entity_ref, created_turn, session_id, created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                ("artifacts/w/1/portrait/h.png", "portrait", "e", 1, sid, "t"),
            )
            with pytest.raises(psycopg.errors.UniqueViolation):
                conn.execute(
                    "INSERT INTO asset_ledger "
                    "(r2_key, asset_type, entity_ref, created_turn, session_id, created_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    ("artifacts/w/1/portrait/h.png", "portrait", "e2", 2, sid, "t2"),
                )
    finally:
        db_pool.close_pool()


def test_dangling_session_id_rejected(migrated_db: str) -> None:
    """A row referencing a non-existent session_id violates the FK."""
    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(plain) as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "INSERT INTO asset_ledger "
                "(r2_key, asset_type, entity_ref, created_turn, session_id, created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                ("artifacts/w/1/portrait/x.png", "portrait", "e", 1, 999_999_999, "t"),
            )


# ---------------------------------------------------------------------------
# AC2 — PgAssetLedgerStore: idempotent upsert + read-back + isolation
# ---------------------------------------------------------------------------


def test_append_then_list_returns_row(store) -> None:
    store.append(**_ledger_kwargs())
    rows = store.list_assets()
    assert len(rows) == 1
    assert rows[0]["r2_key"] == "artifacts/flickering_reach/42/portrait/abc123.png"
    assert rows[0]["asset_type"] == "portrait"
    assert rows[0]["entity_ref"] == "gruk_the_unwashed"


def test_append_is_idempotent_on_r2_key(store) -> None:
    """Writing the same r2_key twice yields ONE row (upsert), not a duplicate."""
    store.append(**_ledger_kwargs(created_turn=1))
    store.append(**_ledger_kwargs(created_turn=5))  # same r2_key, later turn
    rows = store.list_assets()
    assert len(rows) == 1, "ON CONFLICT (r2_key) DO UPDATE must collapse to one row"


def test_append_distinct_keys_kept_separate(store) -> None:
    store.append(**_ledger_kwargs(r2_key="artifacts/w/1/portrait/a.png"))
    store.append(**_ledger_kwargs(r2_key="artifacts/w/1/illustration/b.png", asset_type="illustration"))
    rows = store.list_assets()
    assert {r["r2_key"] for r in rows} == {
        "artifacts/w/1/portrait/a.png",
        "artifacts/w/1/illustration/b.png",
    }


def test_list_empty_when_no_rows(store) -> None:
    assert store.list_assets() == []


def test_cross_session_isolation(monkeypatch, migrated_db: str) -> None:
    """Session B's ledger store sees none of session A's rows."""
    from sidequest.game.pg.asset_ledger import PgAssetLedgerStore

    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    pool = db_pool.get_pool()
    try:
        sid_a = sessions.ensure_session(
            pool, slug=f"iso_a_{uuid.uuid4().hex[:8]}", mode="solo", genre_slug="g", world_slug="w"
        )
        sid_b = sessions.ensure_session(
            pool, slug=f"iso_b_{uuid.uuid4().hex[:8]}", mode="solo", genre_slug="g", world_slug="w"
        )
        store_a = PgAssetLedgerStore(pool, session_id=sid_a)
        store_b = PgAssetLedgerStore(pool, session_id=sid_b)

        store_a.append(**_ledger_kwargs(r2_key="artifacts/w/1/portrait/a.png"))

        assert store_b.list_assets() == []
        assert len(store_a.list_assets()) == 1
    finally:
        db_pool.close_pool()


def test_injection_safe_entity_ref_roundtrips_literally(store) -> None:
    """A SQL-ish entity_ref is stored verbatim (parameterized query, not f-string)."""
    nasty = "'); DROP TABLE asset_ledger; --"
    store.append(**_ledger_kwargs(entity_ref=nasty))
    rows = store.list_assets()
    assert rows[0]["entity_ref"] == nasty
    # Table still exists / is queryable after the "injection".
    assert isinstance(store.list_assets(), list)
