"""SqliteStore save/load OTEL wiring — sprint 3 cold-subsystem audit.

Pre-audit: ``game/persistence.py`` and ``game/world_save.py`` together
emitted 1 watcher event total — the slot-reinit span on
``init_session``. The save/load loop ran every turn but the GM panel
saw no proof that snapshots persisted, recovered, or migrated.

This test pins three new state_transition events:
- ``save:snapshot_saved``     on every successful save()
- ``save:snapshot_loaded``    on every load() with a snapshot present
- ``save:snapshot_load_empty`` on load() against a fresh store

All three publish through the module-level ``_watcher_publish``
indirection. Tests monkeypatch the persistence module's binding so
unit-level capture works without binding the hub to an event loop.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from sidequest.game.persistence import SqliteStore
from sidequest.game.session import GameSnapshot


@pytest.fixture
def captured_watcher_events(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    captured: list[dict[str, Any]] = []

    def _capture(event_type, fields, *, component="sidequest-server", severity="info"):
        captured.append(
            {
                "event_type": event_type,
                "fields": fields,
                "component": component,
                "severity": severity,
            }
        )

    from sidequest.game import persistence

    monkeypatch.setattr(persistence, "_watcher_publish", _capture)
    yield captured


def _save_events(captured: list[dict], op: str) -> list[dict]:
    return [
        e
        for e in captured
        if e["component"] == "persistence"
        and e["event_type"] == "state_transition"
        and e["fields"].get("op") == op
    ]


def test_save_publishes_snapshot_saved_event(captured_watcher_events: list[dict]) -> None:
    """A successful save must publish a snapshot_saved event with the
    snapshot's identifying slugs and population counts."""
    store = SqliteStore.open_in_memory()
    store.init_session("caverns_and_claudes", "caverns_sunden")
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="caverns_sunden",
        characters=[],
        npcs=[],
    )
    store.save(snap)

    events = _save_events(captured_watcher_events, "snapshot_saved")
    assert len(events) == 1, (
        f"expected exactly one snapshot_saved event, got {len(events)}: "
        f"{[e['fields'] for e in captured_watcher_events]}"
    )
    fields = events[0]["fields"]
    assert fields["genre_slug"] == "caverns_and_claudes"
    assert fields["world_slug"] == "caverns_sunden"
    assert fields["character_count"] == 0
    assert fields["npc_count"] == 0
    assert fields["round"] == 1
    assert fields["interaction"] == 1
    assert fields["byte_size"] > 0
    assert fields["save_path"] == "<in-memory>"


def test_load_empty_publishes_load_empty_event(captured_watcher_events: list[dict]) -> None:
    """``load`` against a fresh store returns None and emits the
    snapshot_load_empty event so the GM panel can distinguish "no save
    yet" from "load wasn't called this session."""
    store = SqliteStore.open_in_memory()
    store.init_session("caverns_and_claudes", "caverns_sunden")
    result = store.load()
    assert result is None

    events = _save_events(captured_watcher_events, "snapshot_load_empty")
    assert len(events) == 1
    assert events[0]["fields"]["save_path"] == "<in-memory>"
    assert _save_events(captured_watcher_events, "snapshot_loaded") == []


def test_save_then_load_publishes_loaded_event(captured_watcher_events: list[dict]) -> None:
    """Round-trip: save publishes snapshot_saved, load publishes
    snapshot_loaded (not snapshot_load_empty). Verifies the success
    branch of load() reaches the new emit."""
    store = SqliteStore.open_in_memory()
    store.init_session("caverns_and_claudes", "caverns_sunden")
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="caverns_sunden",
        characters=[],
        npcs=[],
    )
    store.save(snap)
    loaded = store.load()
    assert loaded is not None

    saved_events = _save_events(captured_watcher_events, "snapshot_saved")
    loaded_events = _save_events(captured_watcher_events, "snapshot_loaded")
    empty_events = _save_events(captured_watcher_events, "snapshot_load_empty")
    assert len(saved_events) == 1
    assert len(loaded_events) == 1
    assert empty_events == []  # load saw a snapshot, not empty
    fields = loaded_events[0]["fields"]
    assert fields["genre_slug"] == "caverns_and_claudes"
    assert fields["character_count"] == 0
    assert fields["migration_applied"] is False
