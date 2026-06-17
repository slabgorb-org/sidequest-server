"""Story 126-4: the authoritative TURN_STATUS roster carries the sealed
player's action text.

ADR-036 makes peer action text visible during the WAIT phase. The text's only
other carrier is the best-effort ACTION_REVEAL frame, so a submitted-only turn
(no preceding composing — fast typist / paste-and-Enter) strands the peer at the
"✓ Sealed" chip with no text when that single frame is missed. Carrying the text
on the authoritative roster (sourced from SessionRoom.pending_actions) lets the
UI recover it (App.tsx batch-entries path + mergePeerRevealsWithSubmittedStatus).

Two layers:
- unit: ``build_turn_status_roster`` stamps ``action`` for sealed players from
  ``pending_action_texts`` and leaves pending players text-less.
- wiring: a real player submission broadcasts ``TURN_STATUS{submitted}`` whose
  roster entry for the submitter carries their action text.
"""

from __future__ import annotations

import pytest

from sidequest.game.persistence import GameMode
from sidequest.protocol.messages import (
    PlayerActionMessage,
    PlayerActionPayload,
    TurnStatusMessage,
)
from sidequest.protocol.types import NonBlankString
from sidequest.server.turn_status_roster import build_turn_status_roster


def test_build_turn_status_roster_stamps_sealed_action_text(
    session_handler_factory,
) -> None:
    _handler, sd, room = session_handler_factory(
        slug="test-mp-roster-text-unit",
        mode=GameMode.MULTIPLAYER,
        seat_players=[("p1", "Rux"), ("p2", "Mara")],
        active_player=("p1", "Rux"),
    )
    snapshot = sd.snapshot
    # p1 has sealed (buffered an action); p2 is still composing.
    object.__getattribute__(snapshot.turn_manager, "_submitted").add("p1")

    roster = build_turn_status_roster(
        snapshot,
        room.playing_player_ids(),
        {"p1": "Rux kneels by the shivering figure and offers his waterskin"},
    )

    by_id = {e.player_id.root: e for e in roster}
    assert by_id["p1"].status == "submitted"
    assert by_id["p1"].action == "Rux kneels by the shivering figure and offers his waterskin"
    # The composing peer carries no text — never a phantom blank.
    assert by_id["p2"].status == "pending"
    assert by_id["p2"].action is None


@pytest.mark.asyncio
async def test_submission_broadcasts_sealed_action_text_on_roster(
    session_handler_factory,
) -> None:
    handler1, sd1, room = session_handler_factory(
        slug="test-mp-roster-text-wiring",
        mode=GameMode.MULTIPLAYER,
        seat_players=[("p1", "Rux"), ("p2", "Mara")],
        active_player=("p1", "Rux"),
    )

    async def fake_execute(sd, action, turn_context):
        return []

    handler1._execute_narration_turn = fake_execute  # type: ignore[method-assign]

    submitted_rosters: list[list] = []
    orig_broadcast = room.broadcast

    def capturing_broadcast(msg, **kw):
        if isinstance(msg, TurnStatusMessage) and msg.payload.status == "submitted":
            submitted_rosters.append(list(msg.payload.entries or []))
        return orig_broadcast(msg, **kw)

    room.broadcast = capturing_broadcast  # type: ignore[method-assign]

    action_text = "Rux hauls the winch chain hand over hand"
    await handler1._handle_player_action(
        PlayerActionMessage(
            payload=PlayerActionPayload(
                action=NonBlankString.model_validate(action_text),
                round=0,
            ),
            player_id="p1",
        )
    )

    # The TURN_STATUS{submitted} broadcast that flips Rux to "✓ Sealed" on the
    # peer tab must carry Rux's action text on his roster entry.
    assert submitted_rosters, "no TURN_STATUS{submitted} broadcast captured"
    last = submitted_rosters[-1]
    rux = next((e for e in last if e.player_id.root == "p1"), None)
    assert rux is not None, "submitter missing from the roster"
    assert rux.status == "submitted"
    assert rux.action == action_text
