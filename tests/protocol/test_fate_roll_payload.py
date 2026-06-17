"""RED tests for Story 118-3 (ADR-144 F3c) — the 4dF roll on the wire.

The Fate dice tuple currently lives ONLY on the OTEL span (``fate.py`` ships
``dice=outcome.dice`` to ``fate_action_resolved_span`` and nowhere else). F3c
promotes it onto the wire so the player can SEE the roll — the four Fudge faces,
the ladder rating, the shift total, the outcome tier, and the succeed-with-style
highlight (the legibility mandate — Sebastien/Jade, a player-facing surface).

A roll is an EVENT (like ``DICE_RESULT``), not change-gated reactive state (like
``FATE_STATE``), so it travels as a dedicated ``FateRollPayload`` on a
``MessageType.FATE_ROLL`` message routed through the ``GameMessage`` union.

This file pins the protocol + projection halves of the spine:
  * a ``FateRollPayload`` (strict ``extra="forbid"``) carrying every legibility field,
  * a ``build_fate_roll_payload(FateOutcome)`` projection that maps the resolved
    roll faithfully (incl. ladder NAME and the succeed-with-style flag), and
  * a routable ``FateRollMessage`` in the ``GameMessage`` discriminated union.

All FAIL today: none of these symbols exist yet (RED). New symbols are imported
INSIDE each test so the file collects and every gap reports as its own ImportError.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.game.ruleset.fate_resolution import FateOutcome, FateTier, ladder_name

# Story 125-4 (ADR-144 F3g follow-up): FateRollPayload now ALSO carries the dice
# animation replay fields ``throw_params`` + ``seed`` (mirroring DICE_RESULT) so the
# 3D FateDiceTray can animate the roll instead of rendering the idle pickup row
# (throwParams=null). Both are REQUIRED — an optional field defaulting to None would
# silently re-introduce the idle-dice bug this story exists to kill (No Silent
# Fallbacks). A valid gesture + seed every direct construction below can reuse.
_THROW = {"velocity": (0.1, 2.0, -0.3), "angular": (1.0, -2.0, 0.5), "position": (0.5, 0.5)}
_SEED = 4242


def _succeed_outcome() -> FateOutcome:
    """4dF = (+1,+1,0,-1) -> roll 1; skill Good(3) -> ladder 4; vs 2 -> +2 shifts -> Succeed."""
    return FateOutcome(
        dice=(1, 1, 0, -1),
        roll_total=1,
        ladder_total=4,
        opposition=2,
        shifts=2,
        tier=FateTier.Succeed,
    )


def _style_outcome() -> FateOutcome:
    """+3 shifts crosses the succeed-with-style threshold."""
    return FateOutcome(
        dice=(1, 1, 1, 0),
        roll_total=3,
        ladder_total=6,
        opposition=3,
        shifts=3,
        tier=FateTier.SucceedWithStyle,
    )


# ---------------------------------------------------------------------------
# AC-1 — the message type tag exists and matches its enum value.
# ---------------------------------------------------------------------------


def test_fate_roll_message_type_enum_value():
    from sidequest.protocol.enums import MessageType

    assert MessageType.FATE_ROLL == "FATE_ROLL"


# ---------------------------------------------------------------------------
# AC-2 — the payload carries every legibility field, strictly.
# ---------------------------------------------------------------------------


def test_payload_carries_all_legibility_fields():
    from sidequest.protocol.models import FateRollPayload

    p = FateRollPayload(
        dice=(1, 1, 0, -1),
        roll_total=1,
        ladder_total=4,
        ladder_name="Great",
        opposition=2,
        shifts=2,
        tier="Succeed",
        succeeded_with_style=False,
        throw_params=_THROW,
        seed=_SEED,
    )
    assert tuple(p.dice) == (1, 1, 0, -1)
    assert p.ladder_total == 4
    assert p.ladder_name == "Great"
    assert p.shifts == 2
    assert p.tier == "Succeed"
    assert p.succeeded_with_style is False


def test_payload_forbids_unknown_fields():
    """Boundary validation (No Silent Fallbacks): an unknown wire field fails loud."""
    from sidequest.protocol.models import FateRollPayload

    with pytest.raises(ValidationError):
        FateRollPayload(
            dice=(0, 0, 0, 0),
            roll_total=0,
            ladder_total=0,
            ladder_name="Mediocre",
            opposition=0,
            shifts=0,
            tier="Tie",
            succeeded_with_style=False,
            throw_params=_THROW,
            seed=_SEED,
            bogus=1,
        )


def test_payload_rejects_wrong_dice_arity():
    """4dF is exactly four faces — three or five must not validate."""
    from sidequest.protocol.models import FateRollPayload

    with pytest.raises(ValidationError):
        FateRollPayload(
            dice=(1, 0, -1),
            roll_total=0,
            ladder_total=0,
            ladder_name="Mediocre",
            opposition=0,
            shifts=0,
            tier="Tie",
            succeeded_with_style=False,
            throw_params=_THROW,
            seed=_SEED,
        )


def test_payload_round_trips_through_json():
    from sidequest.protocol.models import FateRollPayload

    p = FateRollPayload(
        dice=(1, 1, 1, 0),
        roll_total=3,
        ladder_total=6,
        ladder_name="Fantastic",
        opposition=3,
        shifts=3,
        tier="SucceedWithStyle",
        succeeded_with_style=True,
        throw_params=_THROW,
        seed=_SEED,
    )
    restored = FateRollPayload.model_validate_json(p.model_dump_json())
    assert restored == p


# ---------------------------------------------------------------------------
# AC-3 — the projection maps a resolved FateOutcome faithfully.
# ---------------------------------------------------------------------------


def test_build_payload_maps_outcome_fields():
    from sidequest.game.ruleset.fate_projection import build_fate_roll_payload

    out = _succeed_outcome()
    p = build_fate_roll_payload(out)
    assert tuple(p.dice) == out.dice
    assert p.roll_total == out.roll_total
    assert p.ladder_total == out.ladder_total
    assert p.shifts == out.shifts
    assert p.opposition == out.opposition
    assert p.tier == out.tier  # FateTier is a StrEnum -> compares equal to its str
    # The ladder ADJECTIVE is what the player reads — derived from the rating.
    assert p.ladder_name == ladder_name(out.ladder_total)


def test_build_payload_sets_succeed_with_style_flag():
    from sidequest.game.ruleset.fate_projection import build_fate_roll_payload

    assert build_fate_roll_payload(_succeed_outcome()).succeeded_with_style is False
    assert build_fate_roll_payload(_style_outcome()).succeeded_with_style is True


# ---------------------------------------------------------------------------
# AC-4 — the message carries the payload and is routable to the client.
# ---------------------------------------------------------------------------


def test_fate_roll_message_carries_type_and_payload():
    from sidequest.protocol.enums import MessageType
    from sidequest.protocol.messages import FateRollMessage
    from sidequest.protocol.models import FateRollPayload

    msg = FateRollMessage(
        payload=FateRollPayload(
            dice=(0, 0, 0, 0),
            roll_total=0,
            ladder_total=2,
            ladder_name="Fair",
            opposition=2,
            shifts=0,
            tier="Tie",
            succeeded_with_style=False,
            throw_params=_THROW,
            seed=_SEED,
        )
    )
    assert msg.type == MessageType.FATE_ROLL
    assert msg.player_id == ""  # sibling default, per FateStateMessage
    assert isinstance(msg.payload, FateRollPayload)


def test_fate_roll_message_in_game_message_union():
    """Without union membership the message is unroutable on the client side."""
    from sidequest.protocol.messages import FateRollMessage, GameMessage

    union = GameMessage.model_fields["root"].annotation
    members = getattr(union, "__args__", ())
    assert FateRollMessage in members, (
        "FateRollMessage is not a member of the GameMessage union — the 4dF roll "
        "would be unroutable on the client side"
    )


def test_game_message_parses_fate_roll_wire_form():
    from sidequest.protocol.messages import FateRollMessage, GameMessage

    wire = {
        "type": "FATE_ROLL",
        "player_id": "",
        "payload": {
            "dice": [1, 1, 0, -1],
            "roll_total": 1,
            "ladder_total": 4,
            "ladder_name": "Great",
            "opposition": 2,
            "shifts": 2,
            "tier": "Succeed",
            "succeeded_with_style": False,
        },
    }
    parsed = GameMessage.model_validate(wire)
    assert isinstance(parsed.root, FateRollMessage)
    assert tuple(parsed.root.payload.dice) == (1, 1, 0, -1)
    assert parsed.root.payload.ladder_name == "Great"


# ---------------------------------------------------------------------------
# AC-5 (Story 125-4) — the payload carries the dice-animation replay fields.
#
# F3g left the 3D FateDiceTray rendering the idle pickup row because FATE_ROLL
# carried no throw_params/seed to replay (unlike DICE_RESULT). 125-4 promotes
# both onto the payload so the dice can animate. Both are REQUIRED (mirroring
# DiceResultPayload) — fail-loud, so a missing gesture can never silently fall
# back to the idle (null) render.
# ---------------------------------------------------------------------------


def test_payload_carries_throw_params_and_seed():
    """The replay fields are present and typed: a ThrowParams gesture + an int seed."""
    from sidequest.protocol.dice import ThrowParams
    from sidequest.protocol.models import FateRollPayload

    p = FateRollPayload(
        dice=(1, 1, 0, -1),
        roll_total=1,
        ladder_total=4,
        ladder_name="Great",
        opposition=2,
        shifts=2,
        tier="Succeed",
        succeeded_with_style=False,
        throw_params=_THROW,
        seed=_SEED,
    )
    assert isinstance(p.throw_params, ThrowParams)
    assert p.throw_params.velocity == (0.1, 2.0, -0.3)
    assert p.throw_params.angular == (1.0, -2.0, 0.5)
    assert p.throw_params.position == (0.5, 0.5)
    assert p.seed == _SEED


def test_throw_params_is_required():
    """Omitting the gesture must fail loud — never default to the idle (null) render."""
    from sidequest.protocol.models import FateRollPayload

    with pytest.raises(ValidationError):
        FateRollPayload(
            dice=(0, 0, 0, 0),
            roll_total=0,
            ladder_total=2,
            ladder_name="Fair",
            opposition=2,
            shifts=0,
            tier="Tie",
            succeeded_with_style=False,
            seed=_SEED,
        )


def test_seed_is_required():
    """The replay seed must be present (the spectator-replay contract, per DICE_RESULT)."""
    from sidequest.protocol.models import FateRollPayload

    with pytest.raises(ValidationError):
        FateRollPayload(
            dice=(0, 0, 0, 0),
            roll_total=0,
            ladder_total=2,
            ladder_name="Fair",
            opposition=2,
            shifts=0,
            tier="Tie",
            succeeded_with_style=False,
            throw_params=_THROW,
        )


def test_payload_round_trips_with_replay_fields():
    """The new fields survive the JSON wire round-trip alongside the legibility fields."""
    from sidequest.protocol.models import FateRollPayload

    p = FateRollPayload(
        dice=(1, 1, 0, -1),
        roll_total=1,
        ladder_total=4,
        ladder_name="Great",
        opposition=2,
        shifts=2,
        tier="Succeed",
        succeeded_with_style=False,
        throw_params=_THROW,
        seed=_SEED,
    )
    restored = FateRollPayload.model_validate_json(p.model_dump_json())
    assert restored == p
    assert restored.throw_params.velocity == (0.1, 2.0, -0.3)
    assert restored.seed == _SEED
