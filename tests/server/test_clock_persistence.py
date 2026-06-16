"""Round-trip test for GameSnapshot.clock_t_hours.

Verifies the new field rides on the existing SqliteStore save/load path
without schema migrations. Old saves without the field load with default 0.0.
"""

from __future__ import annotations

from pathlib import Path

from sidequest.game.persistence import SqliteStore
from sidequest.game.session import GameSnapshot


def test_clock_t_hours_round_trip(tmp_path: Path):
    """Save a GameSnapshot with non-zero clock_t_hours, load, verify."""
    db_path = tmp_path / "test.db"
    store = SqliteStore(db_path)

    snap = GameSnapshot(clock_t_hours=42.0)
    store.save(snap)

    loaded = store.load()
    assert loaded is not None
    assert loaded.snapshot.clock_t_hours == 42.0


def test_clock_t_hours_default_zero():
    """Default value when not set."""
    snap = GameSnapshot()
    assert snap.clock_t_hours == 0.0


def test_clock_t_hours_preserved_through_dict_roundtrip():
    """Pydantic model_dump / model_validate round trip preserves field."""
    snap = GameSnapshot(clock_t_hours=17.5)
    data = snap.model_dump()
    assert data["clock_t_hours"] == 17.5
    restored = GameSnapshot.model_validate(data)
    assert restored.clock_t_hours == 17.5
