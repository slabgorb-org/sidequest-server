"""Per-tool test fixtures."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from sidequest.agents import narrator_perception_filter as _npf

if TYPE_CHECKING:
    from sidequest.game.repository import SaveRepository
    from sidequest.game.session import GameSnapshot


@pytest.fixture(autouse=True)
def _pg_isolation(pg_isolation: None):
    """Every tool test runs against an isolated per-worker Postgres database.

    Story 158-78: the body moved to the ``pg_isolation`` fixture in
    ``tests/conftest.py`` so modules outside this directory can request the same
    isolation — an autouse fixture here protects only this directory, but
    ``pg_store_with`` is imported from further up the tree. This shim keeps the
    isolation automatic for the tool suite.
    """
    yield


def _unique_slug(prefix: str) -> str:
    """A session slug unique to this call.

    Story 158-78: these helpers previously defaulted every caller to the single
    literal ``"tool-test"``, so two stores built in one database landed on the
    SAME ``sessions`` row and the second silently overwrote the first — the
    reload came back with one test's ``SessionMeta`` wrapped around another
    test's ``GameSnapshot``. Mirrors the ``wiring-{uuid4}`` slug that
    ``tests/integration/test_mutation_wiring.py`` already uses.
    """
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def pg_store_with(snapshot: GameSnapshot, *, slug: str | None = None) -> SaveRepository:
    """Build a real PgSaveRepository, init the session, and persist ``snapshot``.

    ADR-115 F1 replacement for the per-file ``SqliteStore.open_in_memory()`` +
    ``init_session`` + ``save`` helper. Tools read/mutate/save through the
    returned repository; ``repository.load()`` round-trips the mutation back.

    ``slug`` defaults to a unique per-call value so two stores cannot collide;
    pass an explicit slug only when a test needs to address a known session.
    """
    from sidequest.game import db_pool
    from sidequest.server.session_state import _build_pg_repos_for_slug

    repo, _dungeon, _sink = _build_pg_repos_for_slug(
        db_pool.get_pool(),
        slug=slug if slug is not None else _unique_slug("tool-test"),
        mode="solo",
        genre_slug=snapshot.genre_slug,
        world_slug=snapshot.world_slug,
    )
    repo.init_session()
    repo.save(snapshot)
    return repo


def pg_empty_store(*, slug: str | None = None) -> SaveRepository:
    """Build a PgSaveRepository with no persisted snapshot — ``load()`` returns
    None. ADR-115 F1 replacement for an un-saved ``SqliteStore.open_in_memory()``
    (the "no active session" error path)."""
    from sidequest.game import db_pool
    from sidequest.server.session_state import _build_pg_repos_for_slug

    repo, _dungeon, _sink = _build_pg_repos_for_slug(
        db_pool.get_pool(),
        slug=slug if slug is not None else _unique_slug("tool-test-empty"),
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
