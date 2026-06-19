"""Protocol surface for the Fate DEFEND barrier (spec 2026-06-18 §6, story 126-8):
FATE_DEFEND_REQUEST (server→client) and action="defend" on FATE_THROW.

RED: FateDefendRequestPayload / FateDefendRequestMessage / MessageType.
FATE_DEFEND_REQUEST do not exist yet, and the FateThrowPayload.action Literal does
not yet include "defend" — all imports/constructions below fail until the protocol
layer (plan Task 1) lands.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.protocol import GameMessage
from sidequest.protocol.dice import ThrowParams
from sidequest.protocol.enums import MessageType
from sidequest.protocol.fate import FateDefendRequestPayload, FateThrowPayload
from sidequest.protocol.messages import FateDefendRequestMessage


def _throw_params() -> ThrowParams:
    return ThrowParams(velocity=(0.0, 0.0, 0.0), angular=(0.0, 0.0, 0.0), position=(0.0, 0.0))


def test_defend_request_payload_validates():
    p = FateDefendRequestPayload(
        request_id="d1",
        defender="Rux",
        attacker="Bandit",
        attack_skill="Fight",
        attack_total=5,
        mental=False,
    )
    assert p.attack_total == 5
    assert p.defender == "Rux"
    assert p.attacker == "Bandit"


def test_defend_request_message_is_in_game_message_union():
    msg = FateDefendRequestMessage(
        payload=FateDefendRequestPayload(
            request_id="d1",
            defender="Rux",
            attacker="Bandit",
            attack_skill="Fight",
            attack_total=5,
            mental=False,
        ),
        player_id="p-rux",
    )
    # round-trips through the discriminated union by its type tag
    parsed = GameMessage.model_validate(msg.model_dump())
    assert parsed.root.type == MessageType.FATE_DEFEND_REQUEST
    assert parsed.root.payload.attacker == "Bandit"
    assert parsed.root.payload.attack_total == 5


def test_defend_request_payload_forbids_extra_fields():
    # ProtocolBase sets extra="forbid" — a typo'd/injected field is rejected loud
    # (input validation at the boundary, lang-review #11 / No Silent Fallbacks).
    with pytest.raises(ValidationError):
        FateDefendRequestPayload(
            request_id="d1",
            defender="Rux",
            attacker="Bandit",
            attack_skill="Fight",
            attack_total=5,
            sneaky="nope",
        )


def test_fate_throw_accepts_action_defend_with_request_id():
    p = FateThrowPayload(
        request_id="d1",
        action="defend",
        skill="Athletics",
        throw_params=_throw_params(),
        face=(0, 1, -1, 0),
    )
    assert p.action == "defend"
    assert p.request_id == "d1"


def test_fate_throw_defend_still_enforces_four_faces():
    with pytest.raises(ValidationError):
        FateThrowPayload(
            request_id="d1",
            action="defend",
            skill="Athletics",
            throw_params=_throw_params(),
            face=(0, 1, -1),  # only 3
        )


def test_fate_throw_defend_rejects_out_of_range_face():
    # The dF validator must still bite on a defend throw (faces ∈ {-1,0,1}).
    with pytest.raises(ValidationError):
        FateThrowPayload(
            request_id="d1",
            action="defend",
            skill="Athletics",
            throw_params=_throw_params(),
            face=(0, 1, -1, 2),  # 2 is not a dF face
        )
