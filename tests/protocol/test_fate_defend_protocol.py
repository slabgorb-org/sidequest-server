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


# ---------------------------------------------------------------------------
# Story 126-14: a defend-CONCESSION signal on FATE_THROW(action="defend").
# The engine concede branch already exists (dispatch_fate_defense(conceded=...),
# the _resolve_attack fold, the `ledger_full ... or p.conceded` clause,
# FatePendingDefense.conceded) but has no player-side wire. This story adds a
# `concede` field to the throw; faces become optional ON THE CONCEDE PATH ONLY
# (a concession does not roll), while a non-concede defend throw MUST still carry
# four valid faces (the anti-backdoor invariant 126-8 set).
# RED: FateThrowPayload has no `concede` field and `face` is still required.
# ---------------------------------------------------------------------------


def test_fate_throw_defend_concede_valid_without_faces():
    # AC-1: a concession carries no dice — the defender folds ("no roll_4df, no
    # reported faces required"). The payload must accept action="defend" + concede
    # with NO `face` supplied.
    p = FateThrowPayload(
        request_id="d1",
        action="defend",
        concede=True,
        throw_params=_throw_params(),
    )
    assert p.action == "defend"
    assert p.concede is True


def test_fate_throw_defend_concede_field_defaults_false():
    # AC-1: a normal (rolled) defend throw is NOT a concession — the flag defaults
    # False so an existing defend throw keeps its physics-is-the-roll meaning.
    p = FateThrowPayload(
        request_id="d1",
        action="defend",
        skill="Athletics",
        throw_params=_throw_params(),
        face=(0, 1, -1, 0),
    )
    assert p.concede is False


def test_fate_throw_non_concede_defend_still_requires_faces():
    # AC-1 anti-backdoor guard: faces are optional ONLY when conceding. A defend
    # throw that is NOT a concession and supplies NO faces must still be rejected
    # loud — an empty/absent faces field must never re-open the
    # server-rolls-for-the-player door (No Silent Fallbacks, lang-review #11).
    with pytest.raises(ValidationError):
        FateThrowPayload(
            request_id="d1",
            action="defend",
            skill="Athletics",
            throw_params=_throw_params(),
            # no `face` and no `concede` → invalid
        )
