"""Tests: ENCOUNTER_* state_transition events are persisted to the events table.

Task 20: Persist state_transition events for encounter to the events table.

These tests verify that watcher publish calls with field="encounter" write
typed rows into the SQLite events table — the GM panel lie-detector contract.
"""

from __future__ import annotations

import json

from sidequest.game.persistence import SqliteStore


def _all_events(store: SqliteStore):
    return list(
        store._conn.execute("SELECT kind, payload_json FROM events ORDER BY seq").fetchall()
    )


def test_beat_applied_writes_event_row(store_bound_to_hub, encounter_dispatch_helper):
    store, snapshot, pack = store_bound_to_hub
    encounter_dispatch_helper.run_player_attack(snapshot, pack, beat_id="attack", outcome="Success")
    rows = _all_events(store)
    kinds = [r[0] for r in rows]
    assert "ENCOUNTER_BEAT_APPLIED" in kinds
    payload = next(json.loads(r[1]) for r in rows if r[0] == "ENCOUNTER_BEAT_APPLIED")
    assert payload["actor_side"] == "player"
    assert payload["beat_kind"] == "strike"
    assert payload["outcome_tier"] == "Success"


def test_resolution_writes_event_row_with_structured_outcome(
    store_bound_to_hub,
    encounter_dispatch_helper,
):
    store, snapshot, pack = store_bound_to_hub
    encounter_dispatch_helper.run_to_resolution(snapshot, pack, winner="opponent")
    rows = _all_events(store)
    kinds = [r[0] for r in rows]
    assert kinds[-1] == "ENCOUNTER_RESOLVED"
    payload = json.loads(rows[-1][1])
    assert payload["outcome"] == "opponent_victory"


def test_encounter_timeline_query_returns_ordered_rows(
    store_bound_to_hub, encounter_dispatch_helper
):
    store, snapshot, pack = store_bound_to_hub
    encounter_dispatch_helper.run_to_resolution(snapshot, pack, winner="opponent")
    from sidequest.game.persistence import query_encounter_events

    rows = query_encounter_events(store)
    kinds = [r["kind"] for r in rows]
    assert len(kinds) > 0, "No encounter events were persisted"
    assert kinds[-1] == "ENCOUNTER_RESOLVED"
    # Verify structure: each row has seq, kind, payload, created_at
    for r in rows:
        assert "seq" in r
        assert "kind" in r
        assert "payload" in r
        assert "created_at" in r
