"""Wiring test: handle_yield advances the room's session clock on scene end."""

from __future__ import annotations

from pathlib import Path

from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.game.persistence import GameMode, SqliteStore
from sidequest.game.session import GameSnapshot
from sidequest.server.dispatch.yield_action import handle_yield
from sidequest.server.session_room import SessionRoom


def _make_room_with_yield_ready_encounter(tmp_path: Path) -> tuple[SessionRoom, GameSnapshot]:
    """Set up a room/snapshot with a yieldable encounter and one player actor."""
    room = SessionRoom(slug="test_world", mode=GameMode.SOLO)
    snap = GameSnapshot()
    # Construct a minimal encounter with one player-side actor (not yet
    # withdrawn, so handle_yield can flip the bit and resolve).
    actor = EncounterActor(
        name="TestActor",
        role="protagonist",
        side="player",
        withdrawn=False,
    )
    enc = StructuredEncounter(
        encounter_type="negotiation",
        player_metric=EncounterMetric(name="resolve", threshold=10),
        opponent_metric=EncounterMetric(name="pressure", threshold=10),
        actors=[actor],
        resolved=False,
    )
    snap.encounter = enc
    room.bind_world(snapshot=snap, store=SqliteStore(tmp_path / "t.db"))
    return room, snap


def test_handle_yield_advances_session_clock(tmp_path, otel_capture):
    room, snap = _make_room_with_yield_ready_encounter(tmp_path)
    assert snap.clock_t_hours == 0.0

    handle_yield(snap, room=room, player_id="p1", player_name="TestActor")

    assert snap.clock_t_hours == 1.0
    span_names = {s.name for s in otel_capture.get_finished_spans()}
    assert "clock.advance" in span_names
    # NOTE: encounter.status_cleared spans only fire if the snapshot has
    # Characters carrying scene-bounded statuses (Scratch/Boon). The bare
    # encounter constructed here has no such statuses; the scratch-sweep
    # behavior itself is covered by tests/server/test_status_clear.py.
    # The dual-span emission ("did the system advance time AND sweep the
    # scratch?") is exercised in Task F's E2E test.
