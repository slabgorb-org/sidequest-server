"""PgSaveRepository save/load OTEL wiring (ADR-115 F1).

The GM panel must see proof that snapshots persist, recover, or come up
empty — not just that the save/load loop ran. ``PgSnapshot.save`` /
``PgSnapshot.load`` publish three ``state_transition`` watcher events
(the same vocabulary the retired SqliteStore emitted):

- ``save:snapshot_saved``      on every successful save()
- ``save:snapshot_loaded``     on every load() with a snapshot present
- ``save:snapshot_load_empty`` on load() against a fresh session

All three publish through the ``sidequest.game.pg.snapshot`` module-level
``_watcher_publish`` indirection, which this test monkeypatches to capture
without binding the hub to an event loop.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from sidequest.game.session import GameSnapshot


@pytest.fixture
def pg_repo(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """A real PgSaveRepository on a per-worker throwaway PG db (ADR-115 F1)."""
    import psycopg

    from sidequest.game import db_pool
    from sidequest.server.session_state import _build_pg_repos_for_slug

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
    repo, _dungeon, _sink = _build_pg_repos_for_slug(
        db_pool.get_pool(),
        slug="persistence-otel",
        mode="solo",
        genre_slug="caverns_and_claudes",
        world_slug="caverns_sunden",
    )
    try:
        yield repo
    finally:
        db_pool.close_pool()


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

    from sidequest.game.pg import snapshot as _pg_snapshot

    monkeypatch.setattr(_pg_snapshot, "_watcher_publish", _capture)
    yield captured


def _save_events(captured: list[dict], op: str) -> list[dict]:
    return [
        e
        for e in captured
        if e["component"] == "persistence"
        and e["event_type"] == "state_transition"
        and e["fields"].get("op") == op
    ]


def test_save_publishes_snapshot_saved_event(pg_repo, captured_watcher_events: list[dict]) -> None:
    """A successful save must publish a snapshot_saved event with the
    snapshot's identifying slugs and population counts."""
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="caverns_sunden",
        characters=[],
        npcs=[],
    )
    pg_repo.save(snap)

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
    assert fields["save_path"] == "<postgres>"


def test_load_empty_publishes_load_empty_event(
    pg_repo, captured_watcher_events: list[dict]
) -> None:
    """``load`` against a fresh session returns None and emits the
    snapshot_load_empty event so the GM panel can distinguish "no save
    yet" from "load wasn't called this session."""
    result = pg_repo.load()
    assert result is None

    events = _save_events(captured_watcher_events, "snapshot_load_empty")
    assert len(events) == 1
    assert events[0]["fields"]["save_path"] == "<postgres>"
    assert _save_events(captured_watcher_events, "snapshot_loaded") == []


def test_save_then_load_publishes_loaded_event(
    pg_repo, captured_watcher_events: list[dict]
) -> None:
    """Round-trip: save publishes snapshot_saved, load publishes
    snapshot_loaded (not snapshot_load_empty). Verifies the success
    branch of load() reaches the emit."""
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="caverns_sunden",
        characters=[],
        npcs=[],
    )
    pg_repo.save(snap)
    loaded = pg_repo.load()
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
