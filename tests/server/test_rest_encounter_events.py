"""Tests for GET /api/sessions/{slug}/encounter_events.

Task 22: GM panel REST endpoint that exposes the SQLite encounter event
timeline to the dashboard EncounterTab.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from sidequest.game.persistence import (
    GameMode,
    SqliteStore,
    db_path_for_slug,
    upsert_game,
)
from sidequest.server.rest import create_rest_router

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.state.save_dir = tmp_path
    app.state.genre_pack_search_paths = []
    app.state.today_fn = lambda: date(2026, 4, 25)
    app.include_router(create_rest_router())
    return TestClient(app)


def _seed_game_with_events(tmp_path: Path, slug: str) -> None:
    """Create a game row + insert a few ENCOUNTER_* rows into the events table."""
    db = db_path_for_slug(tmp_path, slug)
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(db)
    store.initialize()
    upsert_game(
        store,
        slug=slug,
        mode=GameMode.SOLO,
        genre_slug="test_genre",
        world_slug="test_world",
    )
    # Insert rows directly — bypasses the watcher hub so the test has no
    # dependency on a live hub binding.
    rows = [
        (
            "ENCOUNTER_STARTED",
            json.dumps(
                {
                    "encounter_type": "combat",
                    "player_metric_threshold": 10,
                    "opponent_metric_threshold": 10,
                    "turn": 1,
                }
            ),
        ),
        (
            "ENCOUNTER_BEAT_APPLIED",
            json.dumps(
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
        ),
        (
            "ENCOUNTER_RESOLVED",
            json.dumps(
                {
                    "outcome": "player_victory",
                    "final_player_metric": 10,
                    "final_opponent_metric": 4,
                    "triggering_side": "player",
                    "turn": 3,
                }
            ),
        ),
    ]
    for kind, payload_json in rows:
        store._conn.execute(
            "INSERT INTO events (kind, payload_json, created_at) VALUES (?, ?, datetime('now'))",
            (kind, payload_json),
        )
    store._conn.commit()
    store.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_get_encounter_events_returns_ordered_rows(tmp_path: Path) -> None:
    """GET /api/sessions/{slug}/encounter_events returns rows in seq order."""
    slug = "test-encounter-rest"
    _seed_game_with_events(tmp_path, slug)
    client = _make_client(tmp_path)

    resp = client.get(f"/api/sessions/{slug}/encounter_events")
    assert resp.status_code == 200
    data = resp.json()

    assert isinstance(data, list)
    assert len(data) == 3

    # Rows come back in insertion (seq) order.
    kinds = [row["kind"] for row in data]
    assert kinds[0] == "ENCOUNTER_STARTED"
    assert kinds[-1] == "ENCOUNTER_RESOLVED"


def test_get_encounter_events_payload_structure(tmp_path: Path) -> None:
    """Each row has seq, kind, payload, created_at keys."""
    slug = "test-encounter-rest-structure"
    _seed_game_with_events(tmp_path, slug)
    client = _make_client(tmp_path)

    resp = client.get(f"/api/sessions/{slug}/encounter_events")
    assert resp.status_code == 200
    for row in resp.json():
        assert "seq" in row
        assert "kind" in row
        assert "payload" in row
        assert "created_at" in row
        assert isinstance(row["payload"], dict)


def test_get_encounter_events_beat_applied_fields(tmp_path: Path) -> None:
    """Beat-applied row carries actor_side, beat_kind, outcome_tier."""
    slug = "test-encounter-rest-beat"
    _seed_game_with_events(tmp_path, slug)
    client = _make_client(tmp_path)

    resp = client.get(f"/api/sessions/{slug}/encounter_events")
    assert resp.status_code == 200
    beat_rows = [r for r in resp.json() if r["kind"] == "ENCOUNTER_BEAT_APPLIED"]
    assert beat_rows, "expected at least one ENCOUNTER_BEAT_APPLIED row"
    p = beat_rows[0]["payload"]
    assert p["actor_side"] == "player"
    assert p["beat_kind"] == "strike"
    assert p["outcome_tier"] == "Success"


def test_get_encounter_events_404_for_missing_slug(tmp_path: Path) -> None:
    """GET /api/sessions/{slug}/encounter_events returns 404 when slug is unknown."""
    client = _make_client(tmp_path)
    resp = client.get("/api/sessions/does-not-exist/encounter_events")
    assert resp.status_code == 404


def test_get_encounter_events_empty_for_no_encounter_rows(tmp_path: Path) -> None:
    """Returns an empty list when the game exists but has no ENCOUNTER_* events."""
    slug = "test-encounter-rest-empty"
    db = db_path_for_slug(tmp_path, slug)
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(db)
    store.initialize()
    upsert_game(
        store,
        slug=slug,
        mode=GameMode.SOLO,
        genre_slug="test_genre",
        world_slug="test_world",
    )
    # Insert a non-encounter row to confirm the filter works.
    store._conn.execute(
        "INSERT INTO events (kind, payload_json, created_at) VALUES (?, ?, datetime('now'))",
        ("NARRATION", json.dumps({"text": "The dungeon echoes."})),
    )
    store._conn.commit()
    store.close()

    client = _make_client(tmp_path)
    resp = client.get(f"/api/sessions/{slug}/encounter_events")
    assert resp.status_code == 200
    assert resp.json() == []
