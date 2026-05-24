"""Tests for the sealed-letter pacing roster helper.

Bug: sq-playtest 2026-05-12 [BUG-LOW] sealed-letter counter denominator
differs by tab. The fix carries a canonical roster on every TURN_STATUS
broadcast; this module pins the roster shape and a wiring test.
"""

from __future__ import annotations

from sidequest.game.session import GameSnapshot
from sidequest.protocol.messages import TurnStatusEntry, TurnStatusPayload
from sidequest.protocol.types import NonBlankString
from sidequest.server.turn_status_roster import build_turn_status_roster


def _snapshot_with_seats(seats: dict[str, str]) -> GameSnapshot:
    """Build a minimal GameSnapshot with the given player_seats binding."""
    snapshot = GameSnapshot()
    snapshot.player_seats.update(seats)
    return snapshot


# ---------------------------------------------------------------------------
# build_turn_status_roster
# ---------------------------------------------------------------------------


def test_roster_marks_submitted_players() -> None:
    """Player_ids in turn_manager._submitted are emitted as 'submitted'."""
    snapshot = _snapshot_with_seats({"p1": "Carl", "p2": "Donut", "p3": "Katia"})
    object.__getattribute__(snapshot.turn_manager, "_submitted").add("p1")
    object.__getattribute__(snapshot.turn_manager, "_submitted").add("p2")

    roster = build_turn_status_roster(snapshot, ["p1", "p2", "p3"])

    by_id = {entry.player_id.as_str(): entry for entry in roster}
    assert by_id["p1"].status == "submitted"
    assert by_id["p2"].status == "submitted"
    assert by_id["p3"].status == "pending"


def test_roster_uses_seat_names_for_character_name() -> None:
    snapshot = _snapshot_with_seats({"p1": "Carl", "p2": "Donut"})

    roster = build_turn_status_roster(snapshot, ["p1", "p2"])

    by_id = {entry.player_id.as_str(): entry for entry in roster}
    assert by_id["p1"].character_name.as_str() == "Carl"
    assert by_id["p2"].character_name.as_str() == "Donut"


def test_roster_falls_back_to_player_id_when_seat_missing() -> None:
    """A transient state where a PLAYING peer has no seat name should not
    crash the broadcast — we use the player_id as the character_name."""
    snapshot = _snapshot_with_seats({"p1": "Carl"})

    roster = build_turn_status_roster(snapshot, ["p1", "p2"])

    by_id = {entry.player_id.as_str(): entry for entry in roster}
    # p2 has no seat — character_name falls back to the player_id
    assert by_id["p2"].character_name.as_str() == "p2"


def test_roster_skips_blank_player_ids() -> None:
    """Blank player_id would raise NonBlankString — skip rather than crash."""
    snapshot = _snapshot_with_seats({"p1": "Carl"})

    roster = build_turn_status_roster(snapshot, ["", "p1", "  "])

    assert [e.player_id.as_str() for e in roster] == ["p1"]


def test_roster_preserves_iteration_order() -> None:
    """The roster's order follows the playing_player_ids input — this is the
    UI's render order (turn-strip left to right)."""
    snapshot = _snapshot_with_seats({"p1": "Carl", "p2": "Donut", "p3": "Katia"})

    roster = build_turn_status_roster(snapshot, ["p3", "p1", "p2"])

    assert [e.player_id.as_str() for e in roster] == ["p3", "p1", "p2"]


def test_roster_handles_solo_player() -> None:
    """Solo session: roster has one entry, status reflects submission state."""
    snapshot = _snapshot_with_seats({"p1": "Carl"})

    roster = build_turn_status_roster(snapshot, ["p1"])
    assert len(roster) == 1
    assert roster[0].status == "pending"

    object.__getattribute__(snapshot.turn_manager, "_submitted").add("p1")
    roster = build_turn_status_roster(snapshot, ["p1"])
    assert roster[0].status == "submitted"


# ---------------------------------------------------------------------------
# Protocol wire shape — entries field
# ---------------------------------------------------------------------------


def test_turn_status_payload_serializes_entries_on_wire() -> None:
    """The new ``entries`` field is on the wire when populated."""
    payload = TurnStatusPayload(
        player_name=NonBlankString("Carl"),
        status="submitted",
        entries=[
            TurnStatusEntry(
                player_id=NonBlankString("p1"),
                character_name=NonBlankString("Carl"),
                status="submitted",
            ),
            TurnStatusEntry(
                player_id=NonBlankString("p2"),
                character_name=NonBlankString("Donut"),
                status="pending",
            ),
        ],
    )

    dumped = payload.model_dump()
    assert "entries" in dumped
    assert len(dumped["entries"]) == 2
    assert dumped["entries"][0]["player_id"] == "p1"
    assert dumped["entries"][0]["status"] == "submitted"
    assert dumped["entries"][1]["status"] == "pending"


def test_turn_status_payload_omits_entries_when_none() -> None:
    """Legacy callers that don't set ``entries`` produce a wire shape
    identical to the pre-fix wire — the field is absent."""
    payload = TurnStatusPayload(
        player_name=NonBlankString("Carl"),
        status="active",
    )

    dumped = payload.model_dump()
    assert "entries" not in dumped


def test_turn_status_payload_preserves_empty_entries() -> None:
    """An explicit ``entries=[]`` is preserved on the wire — the resolved
    broadcast sends this to signal "round over, no roster"; the UI's
    batch path keys on the field's presence."""
    payload = TurnStatusPayload(
        player_name=NonBlankString("Carl"),
        status="resolved",
        entries=[],
    )

    dumped = payload.model_dump()
    assert "entries" in dumped
    assert dumped["entries"] == []


# ---------------------------------------------------------------------------
# Wiring — every TURN_STATUS broadcast site emits the field
# ---------------------------------------------------------------------------


def test_wiring_player_action_handler_imports_roster_helper() -> None:
    """Production code path: the player_action handler must call the
    roster builder — without this import, the field is never populated."""
    import sidequest.handlers.player_action as handler_mod

    assert hasattr(handler_mod, "build_turn_status_roster")


def test_wiring_active_and_submitted_broadcasts_include_entries() -> None:
    """Both TurnStatusMessage builders in the player_action handler pass
    ``entries=`` to TurnStatusPayload. Source-level pin so a future refactor
    that drops the field is caught by this test rather than only at
    playtest time."""
    from pathlib import Path

    source = Path(
        "/Users/slabgorb/Projects/oq-1/sidequest-server/sidequest/handlers/player_action.py"
    ).read_text()
    # active broadcast
    assert "entries=active_roster" in source
    # submitted broadcast
    assert "entries=submitted_roster" in source


def test_wiring_resolved_broadcast_sends_empty_entries() -> None:
    """The resolved broadcast in websocket_session_handler explicitly sends
    ``entries=[]`` so the UI's batch path clears the roster on round end."""
    from pathlib import Path

    source = Path(
        "/Users/slabgorb/Projects/oq-1/sidequest-server/sidequest/server/websocket_session_handler.py"
    ).read_text()
    assert "entries=[]," in source
