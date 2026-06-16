"""Barrier-fire dispatch must NOT wipe sealed peer reveals.

Playtest 2026-05-17 (Keith): "leave the current player turns visible
after they seal their turns." The dispatch-time
``_broadcast_cleared_to_party(reason="dispatch")`` blanked every reveal
row the instant the last player sealed — so the whole table went empty
through the entire narrator-thinking gap. The clear now moves to the
turn boundary (client flushes on NARRATION_END); the disconnect-path
clear is unaffected.

This wiring test pins the new contract: driving the 2-player barrier to
fire produces NO ACTION_REVEAL CLEARED. (The speaking-PC PLAYER_SPEECH
from the 2026-05-17 dialogue fix must still fire.)
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
async def test_barrier_fire_does_not_clear_peer_reveals(
    session_handler_factory,
) -> None:
    handler1, sd1, room = session_handler_factory(
        slug="test-mp-keep-reveals",
        mode=GameMode.MULTIPLAYER,
        seat_players=[("p1", "Rux"), ("p2", "Mara")],
        active_player=("p1", "Rux"),
    )
    handler2, sd2, _ = session_handler_factory(
        slug="test-mp-keep-reveals",
        mode=GameMode.MULTIPLAYER,
        seat_players=[("p1", "Rux"), ("p2", "Mara")],
        active_player=("p2", "Mara"),
        existing_room=room,
    )

    async def fake_execute(sd, action, turn_context):
        return []

    handler1._execute_narration_turn = fake_execute  # type: ignore[method-assign]
    handler2._execute_narration_turn = fake_execute  # type: ignore[method-assign]

    cleared: list[ActionRevealMessage] = []
    speech: list[PlayerSpeechMessage] = []
    orig_broadcast = room.broadcast

    def capturing_broadcast(msg, **kw):
        if isinstance(msg, ActionRevealMessage) and (
            msg.payload.status == ActionRevealStatus.CLEARED
        ):
            cleared.append(msg)
        elif isinstance(msg, PlayerSpeechMessage):
            speech.append(msg)
        return orig_broadcast(msg, **kw)

    room.broadcast = capturing_broadcast  # type: ignore[method-assign]

    await handler1._handle_player_action(
        PlayerActionMessage(
            payload=PlayerActionPayload(
                action=NonBlankString.model_validate('I face the warden and say "Open the gate."'),
                round=0,
            ),
            player_id="p1",
        )
    )
    await handler2._handle_player_action(
        PlayerActionMessage(
            payload=PlayerActionPayload(
                action=NonBlankString.model_validate("I watch the walls."),
                round=0,
            ),
            player_id="p2",
        )
    )

    # The barrier fired and the narrator dispatched — but sealed reveals
    # stay on screen. No CLEARED at dispatch.
    assert cleared == [], (
        f"barrier-fire still wiped {len(cleared)} reveal row(s); sealed turns "
        f"must remain visible until the turn resolves"
    )
    # The dialogue-visibility fix is unaffected.
    assert len(speech) == 1
    assert speech[0].payload.character_name == "Rux"
