"""Verify dispatch surfaces PC spoken dialogue to the whole MP party.

Playtest 2026-05-17 (Keith + Sebby): quoted dialogue a player typed to
an NPC never reached peers — only the narrator's reply did. The fix
extracts the verbatim quoted spans from each drained action and
broadcasts them, attributed to the speaking PC, to every socket in the
room (exclude_socket_id=None — the speaker's own transcript shows it
too), so the spoken line survives barrier-fire instead of being wiped
with the ACTION_REVEAL CLEARED.

These tests RED until ``PlayerSpeechMessage`` / ``SpokenLinePayload``
and ``_broadcast_player_speech_to_party`` exist.
"""

from unittest.mock import MagicMock

from sidequest.handlers.player_action import _broadcast_player_speech_to_party
from sidequest.protocol.messages import PlayerSpeechMessage
from sidequest.server.session_room import PendingAction


def test_quoted_dialogue_broadcast_attributed_to_speaking_pc() -> None:
    room = MagicMock()
    calls: list[tuple[object, dict]] = []
    room.broadcast.side_effect = lambda msg, **kw: calls.append((msg, kw)) or []
    pending = [
        (
            "p1",
            PendingAction(
                character_name="Rux",
                action='I step forward and say "Well met. What news from the north?"',
            ),
        ),
        (
            "p2",
            PendingAction(
                character_name="Mara", action='I nod, then add "And what of the southern road?"'
            ),
        ),
    ]

    _broadcast_player_speech_to_party(room, pending, round_no=4)

    assert room.broadcast.call_count == 2
    assert all(isinstance(m, PlayerSpeechMessage) for m, _ in calls)
    speakers = [m.payload.character_name for m, _ in calls]
    lines = [str(m.payload.text) for m, _ in calls]
    rounds = [m.payload.round for m, _ in calls]
    assert speakers == ["Rux", "Mara"]
    assert lines == [
        "Well met. What news from the north?",
        "And what of the southern road?",
    ]
    assert all(r == 4 for r in rounds)
    # Goes to everyone — the speaker's own transcript shows their line too.
    assert all(kw.get("exclude_socket_id") is None for _, kw in calls)


def test_action_without_quotes_emits_nothing() -> None:
    room = MagicMock()
    pending = [
        ("p1", PendingAction(character_name="Rux", action="I search the chest for traps.")),
    ]
    _broadcast_player_speech_to_party(room, pending, round_no=1)
    room.broadcast.assert_not_called()


def test_multiple_quoted_spans_in_one_action_emit_one_message_each() -> None:
    room = MagicMock()
    calls: list[tuple[object, dict]] = []
    room.broadcast.side_effect = lambda msg, **kw: calls.append((msg, kw)) or []
    pending = [
        ("p1", PendingAction(character_name="Rux", action='"Hold." Then, softer: "Please."')),
    ]

    _broadcast_player_speech_to_party(room, pending, round_no=2)

    assert room.broadcast.call_count == 2
    assert [str(m.payload.text) for m, _ in calls] == ["Hold.", "Please."]
    assert all(m.payload.character_name == "Rux" for m, _ in calls)


def test_empty_pending_is_a_noop() -> None:
    room = MagicMock()
    _broadcast_player_speech_to_party(room, [], round_no=9)
    room.broadcast.assert_not_called()
