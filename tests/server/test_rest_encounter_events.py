"""Tests for GET /api/sessions/{slug}/encounter_events.

Task 22: GM panel REST endpoint that exposes the encounter event timeline to
the dashboard EncounterTab.

ADR-115 D7: the endpoint reads ENCOUNTER_* rows from Postgres via
``PgForensicReader.encounter_events`` (was the SQLite events table). These
tests seed a migrated PG pool through the real PgEventStore and drive the
endpoint via TestClient, binding the process-global pool to the migrated db.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sidequest.game import db_pool
from sidequest.game.pg import sessions as pg_sessions
from sidequest.game.pg.events import PgEventStore
from sidequest.server.rest import create_rest_router

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def pg_client(monkeypatch, migrated_db: str, tmp_path: Path):
    """Bind the process-global pool to migrated_db; yield (client, pool)."""
    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    pool = db_pool.get_pool()

    app = FastAPI()
    app.state.save_dir = tmp_path
    app.state.genre_pack_search_paths = []
    app.state.today_fn = lambda: date(2026, 4, 25)
    app.include_router(create_rest_router())
    client = TestClient(app)
    yield client, pool
    db_pool.close_pool()


def _seed_game_with_events(pool, slug: str) -> None:
    """Create a session row + insert a few ENCOUNTER_* events into PG."""
    sid = pg_sessions.ensure_session(
        pool, slug=slug, mode="solo", genre_slug="test_genre", world_slug="test_world"
    )
    ev = PgEventStore(pool, session_id=sid)
    ev.append_event(
        kind="ENCOUNTER_STARTED",
        payload_json=json.dumps(
            {
                "encounter_type": "combat",
                "player_metric_threshold": 10,
                "opponent_metric_threshold": 10,
                "turn": 1,
            }
        ),
    )
    ev.append_event(
        kind="ENCOUNTER_BEAT_APPLIED",
        payload_json=json.dumps(
            {
                "actor": "Sam",
                "actor_side": "player",
                "beat_id": "attack",
                "beat_kind": "strike",
                "outcome_tier": "Success",
                "own_delta": 2,
                "opponent_delta": 0,
                "turn": 1,
            }
        ),
    )
    ev.append_event(
        kind="ENCOUNTER_RESOLVED",
        payload_json=json.dumps(
            {
                "outcome": "player_victory",
                "final_player_metric": 10,
                "final_opponent_metric": 4,
                "triggering_side": "player",
                "turn": 3,
            }
        ),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_get_encounter_events_returns_ordered_rows(pg_client) -> None:
    """GET /api/sessions/{slug}/encounter_events returns rows in seq order."""
    client, pool = pg_client
    slug = "test-encounter-rest"
    _seed_game_with_events(pool, slug)

    resp = client.get(f"/api/sessions/{slug}/encounter_events")
    assert resp.status_code == 200
    data = resp.json()

    assert isinstance(data, list)
    assert len(data) == 3

    # Rows come back in insertion (seq) order.
    kinds = [row["kind"] for row in data]
    assert kinds[0] == "ENCOUNTER_STARTED"
    assert kinds[-1] == "ENCOUNTER_RESOLVED"


def test_get_encounter_events_payload_structure(pg_client) -> None:
    """Each row has seq, kind, payload, created_at keys."""
    client, pool = pg_client
    slug = "test-encounter-rest-structure"
    _seed_game_with_events(pool, slug)

    resp = client.get(f"/api/sessions/{slug}/encounter_events")
    assert resp.status_code == 200
    for row in resp.json():
        assert "seq" in row
        assert "kind" in row
        assert "payload" in row
        assert "created_at" in row
        assert isinstance(row["payload"], dict)


def test_get_encounter_events_beat_applied_fields(pg_client) -> None:
    """Beat-applied row carries actor_side, beat_kind, outcome_tier."""
    client, pool = pg_client
    slug = "test-encounter-rest-beat"
    _seed_game_with_events(pool, slug)

    resp = client.get(f"/api/sessions/{slug}/encounter_events")
    assert resp.status_code == 200
    beat_rows = [r for r in resp.json() if r["kind"] == "ENCOUNTER_BEAT_APPLIED"]
    assert beat_rows, "expected at least one ENCOUNTER_BEAT_APPLIED row"
    p = beat_rows[0]["payload"]
    assert p["actor_side"] == "player"
    assert p["beat_kind"] == "strike"
    assert p["outcome_tier"] == "Success"


def test_get_encounter_events_404_for_missing_slug(pg_client) -> None:
    """GET /api/sessions/{slug}/encounter_events returns 404 when slug is unknown."""
    client, _pool = pg_client
    resp = client.get("/api/sessions/does-not-exist/encounter_events")
    assert resp.status_code == 404


def test_get_encounter_events_empty_for_no_encounter_rows(pg_client) -> None:
    """Returns an empty list when the session exists but has no ENCOUNTER_* events."""
    client, pool = pg_client
    slug = "test-encounter-rest-empty"
    sid = pg_sessions.ensure_session(
        pool, slug=slug, mode="solo", genre_slug="test_genre", world_slug="test_world"
    )
    # Insert a non-encounter row to confirm the filter works.
    PgEventStore(pool, session_id=sid).append_event(
        kind="NARRATION", payload_json=json.dumps({"text": "The dungeon echoes."})
    )

    resp = client.get(f"/api/sessions/{slug}/encounter_events")
    assert resp.status_code == 200
    assert resp.json() == []
