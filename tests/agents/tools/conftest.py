"""Per-tool test fixtures."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from sidequest.agents import narrator_perception_filter as _npf

if TYPE_CHECKING:
    from sidequest.game.repository import SaveRepository
    from sidequest.game.session import GameSnapshot


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Bind the process pool to a per-worker throwaway PG db, clean per test.

    ADR-115 F1: the tool tests persist a snapshot, invoke a tool that mutates +
    saves, then reload to assert the mutation stuck — a real ``PgSaveRepository``
    over an isolated Postgres database (see ``pg_store_with``).
    """
    import psycopg

    from sidequest.game import db_pool

    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(plain, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename <> 'alembic_version'"
        ).fetchall()
        if rows:
            names = ", ".join(f'"{r[0]}"' for r in rows)
            conn.execute(f"TRUNCATE {names} RESTART IDENTITY CASCADE")
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    yield
    db_pool.close_pool()


def pg_store_with(snapshot: GameSnapshot, *, slug: str = "tool-test") -> SaveRepository:
    """Build a real PgSaveRepository, init the session, and persist ``snapshot``.

    ADR-115 F1 replacement for the per-file ``SqliteStore.open_in_memory()`` +
    ``init_session`` + ``save`` helper. Tools read/mutate/save through the
    returned repository; ``repository.load()`` round-trips the mutation back.
    """
    from sidequest.game import db_pool
    from sidequest.server.session_state import _build_pg_repos_for_slug

    repo, _dungeon, _sink = _build_pg_repos_for_slug(
        db_pool.get_pool(),
        slug=slug,
        mode="solo",
        genre_slug=snapshot.genre_slug,
        world_slug=snapshot.world_slug,
    )
    repo.init_session()
    repo.save(snapshot)
    return repo


def pg_empty_store(*, slug: str = "tool-test-empty") -> SaveRepository:
    """Build a PgSaveRepository with no persisted snapshot — ``load()`` returns
    None. ADR-115 F1 replacement for an un-saved ``SqliteStore.open_in_memory()``
    (the "no active session" error path)."""
    from sidequest.game import db_pool
    from sidequest.server.session_state import _build_pg_repos_for_slug

    repo, _dungeon, _sink = _build_pg_repos_for_slug(
        db_pool.get_pool(),
        slug=slug,
        mode="solo",
        genre_slug="test_genre",
        world_slug="test_world",
    )
    return repo


def make_mock_repository() -> MagicMock:
    """Return a MagicMock SaveRepository with the PG location-promotion
    interface, backed by a real region_id-keyed in-memory list so tests can
    assert on written rows without coupling to SqliteStore.

    ``list_location_promotions(*, region_id)`` returns the rows for that
    region; ``upsert_location_promotion(row)`` replaces an existing row with
    the same ``(region_id, entity_id)`` or appends. Shared by
    ``test_resolve_location_entity.py`` and
    ``test_resolve_location_entity_otel.py`` so a future PG-interface change
    is a one-place edit.
    """
    _rows: list[Any] = []
    repo = MagicMock()

    def _list(*, region_id: str) -> list[Any]:
        return [r for r in _rows if r.region_id == region_id]

    def _upsert(row: Any) -> None:
        for i, existing in enumerate(_rows):
            if existing.region_id == row.region_id and existing.entity_id == row.entity_id:
                _rows[i] = row
                return
        _rows.append(row)

    repo.list_location_promotions.side_effect = _list
    repo.upsert_location_promotion.side_effect = _upsert
    return repo


@pytest.fixture(autouse=True)
def _isolate_perception_rules():
    """Snapshot and restore the perception _RULES table across tests.

    Tool modules call ``register_rule`` at import time; without isolation,
    test order would couple rule presence across files.
    """
    snapshot = dict(_npf._RULES)
    try:
        yield
    finally:
        _npf._RULES.clear()
        _npf._RULES.update(snapshot)
