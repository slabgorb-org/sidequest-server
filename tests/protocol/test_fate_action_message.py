from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.protocol.enums import MessageType
from sidequest.protocol.fate import FateActionPayload
from sidequest.protocol.messages import FateActionMessage, GameMessage


def test_payload_defaults_and_required():
    p = FateActionPayload(request_id="r1", action="attack", skill="Fight", target="Thug")
    assert p.action == "attack"
    assert p.difficulty == 0
    assert p.invoke_aspect == ""
    assert p.aspect_text == ""


def test_payload_rejects_unknown_action():
    # full_defense is the exact d20/Pathfinder creep ADR-144 purged from the
    # Fate path (defend is reactive/engine-rolled, never player-submitted).
    # Asserting the payload rejects it is a standing regression guard.
    with pytest.raises(ValidationError):
        FateActionPayload(request_id="r1", action="full_defense", skill="Fight")


def test_concede_is_a_valid_action_value():
    p = FateActionPayload(request_id="r1", action="concede", skill="")
    assert p.action == "concede"


def test_message_parses_through_the_discriminated_union():
    wire = {
        "type": "FATE_ACTION",
        "payload": {
            "request_id": "r1",
            "action": "create_advantage",
            "skill": "Notice",
            "difficulty": 2,
            "aspect_text": "Pinned Down",
        },
        "player_id": "p1",
    }
    msg = GameMessage.model_validate(wire)
    assert isinstance(msg.root, FateActionMessage)
    assert msg.root.type == MessageType.FATE_ACTION
    assert msg.root.payload.aspect_text == "Pinned Down"
    assert msg.root.player_id == "p1"
