"""Story 126-7 (ADR-148): the FATE_THROW wire contract.

A player's PROACTIVE Fate roll is physics-is-the-roll — the four settled dF
faces ARE the roll. ``FateThrowPayload`` carries the action intent + the
authoritative ``face[4]`` + the ``throw_params`` gesture. Faces are
authoritative at the wire: exactly four, each in {-1, 0, 1}, ``extra='forbid'``
(inherited from ProtocolBase). The message routes in the GameMessage union as
``FATE_THROW`` so the registry can dispatch it (no optional-dice-on-FATE_ACTION
backdoor — that would be a silent fallback, ADR-148 §2).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.protocol.dice import ThrowParams
from sidequest.protocol.enums import MessageType
from sidequest.protocol.fate import FateThrowPayload
from sidequest.protocol.messages import GameMessage

_TP = {"velocity": [0.0, 4.0, -1.0], "angular": [0.5, 0.5, 0.5], "position": [0.5, 0.5]}


def _payload(**over):
    base = {
        "request_id": "r1",
        "action": "overcome",
        "skill": "Athletics",
        "throw_params": _TP,
        "face": [-1, 0, 1, 1],
    }
    base.update(over)
    return base


def test_valid_payload_parses():
    p = FateThrowPayload(**_payload())
    assert tuple(p.face) == (-1, 0, 1, 1)
    assert isinstance(p.throw_params, ThrowParams)
    assert p.throw_params.velocity == (0.0, 4.0, -1.0)


def test_rejects_wrong_face_count():
    with pytest.raises(ValidationError):
        FateThrowPayload(**_payload(face=[0, 0, 0]))


def test_rejects_too_many_faces():
    with pytest.raises(ValidationError):
        FateThrowPayload(**_payload(face=[0, 0, 0, 0, 1]))


def test_rejects_out_of_range_face():
    with pytest.raises(ValidationError):
        FateThrowPayload(**_payload(face=[0, 0, 0, 2]))


def test_rejects_extra_field():
    # extra='forbid' from ProtocolBase: an optional `bonus`/`face` smuggled onto
    # the wire is a silent fallback the faces-required contract forbids.
    with pytest.raises(ValidationError):
        FateThrowPayload(**_payload(bonus=99))


def test_only_roll_verbs_are_accepted():
    # Non-roll verbs (concede / compel_*) stay on FATE_ACTION — they never throw.
    with pytest.raises(ValidationError):
        FateThrowPayload(**_payload(action="concede"))


def test_routes_in_game_message_union():
    msg = GameMessage.model_validate(
        {"type": "FATE_THROW", "payload": _payload(), "player_id": "p1"}
    )
    assert msg.root.type == MessageType.FATE_THROW
    assert tuple(msg.root.payload.face) == (-1, 0, 1, 1)
