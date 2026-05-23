"""Wiring test: the MP dispatch path surfaces PC speech to the party.

CLAUDE.md mandates an integration test proving the component is reached
from production code. This drives ``_handle_player_action`` end-to-end
for two players (mocked narrator). It asserts that a player's quoted
dialogue is broadcast as a PLAYER_SPEECH to the room *before* the
ACTION_REVEAL CLEARED wipes the wait-phase strip — i.e. the spoken
line survives barrier-fire into the shared transcript.

Playtest 2026-05-17 (Keith + Sebby).
"""

from __future__ import annotations

import pytest

from sidequest.game.persistence import GameMode
from sidequest.protocol.messages import (
    ActionRevealMessage,
    ActionRevealStatus,
    PlayerActionMessage,
    PlayerActionPayload,
    PlayerSpeechMessage,
)
from sidequest.protocol.types import NonBlankString


@pytest.mark.asyncio
async def test_quoted_dialogue_reaches_party_before_narration(
    session_handler_factory,
) -> None:
    handler1, sd1, room = session_handler_factory(
        slug="test-mp-speech",
        mode=GameMode.MULTIPLAYER,
        seat_players=[("p1", "Rux"), ("p2", "Mara")],
        active_player=("p1", "Rux"),
    )
    handler2, sd2, _ = session_handler_factory(
        slug="test-mp-speech",
        mode=GameMode.MULTIPLAYER,
        seat_players=[("p1", "Rux"), ("p2", "Mara")],
        active_player=("p2", "Mara"),
        existing_room=room,
    )

    async def fake_execute(sd, action, turn_context):
        broadcasts.append(("NARRATION_DISPATCH", None))
        return []

    handler1._execute_narration_turn = fake_execute  # type: ignore[method-assign]
    handler2._execute_narration_turn = fake_execute  # type: ignore[method-assign]

    broadcasts: list[tuple[str, object]] = []
    orig_broadcast = room.broadcast

    def capturing_broadcast(msg, **kw):
        if isinstance(msg, PlayerSpeechMessage):
            broadcasts.append(("SPEECH", msg))
        elif isinstance(msg, ActionRevealMessage) and (
            msg.payload.status == ActionRevealStatus.CLEARED
        ):
            # Item 3 (2026-05-17): no dispatch-time clear anymore — sealed
            # turns stay visible until the turn resolves. Capturing it
            # lets us assert it never happens.
            broadcasts.append(("CLEARED", msg))
        return orig_broadcast(msg, **kw)

    room.broadcast = capturing_broadcast  # type: ignore[method-assign]

    await handler1._handle_player_action(
        PlayerActionMessage(
            payload=PlayerActionPayload(
                action=NonBlankString.model_validate(
                    'I face the warden and say "Open the gate. We carry the seal of Duke Halloran."'
                ),
            ),
            player_id="p1",
        )
    )
    await handler2._handle_player_action(
        PlayerActionMessage(
            payload=PlayerActionPayload(
                action=NonBlankString.model_validate(
                    "I keep my hand near my blade and watch the walls."
                ),
            ),
            player_id="p2",
        )
    )

    kinds = [k for k, _ in broadcasts]
    assert "SPEECH" in kinds, f"PLAYER_SPEECH never broadcast; saw {kinds}"

    speech_msgs = [m for k, m in broadcasts if k == "SPEECH"]
    assert len(speech_msgs) == 1
    assert speech_msgs[0].payload.character_name == "Rux"
    assert str(speech_msgs[0].payload.text) == "Open the gate. We carry the seal of Duke Halloran."

    # Mara's plain action produced no speech.
    assert all(m.payload.character_name != "Mara" for m in speech_msgs)

    # Item 3 (2026-05-17): sealed reveals are NOT wiped at barrier-fire.
    assert "CLEARED" not in kinds, f"barrier-fire wiped reveals: {kinds}"

    # Speech must precede the narrator dispatch so it lands in the
    # transcript rather than being garbage-collected.
    first_speech = kinds.index("SPEECH")
    assert kinds.index("NARRATION_DISPATCH") > first_speech, f"speech after narration: {kinds}"
