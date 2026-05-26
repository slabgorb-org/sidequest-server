"""Per-tool test fixtures."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from sidequest.agents import narrator_perception_filter as _npf


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
