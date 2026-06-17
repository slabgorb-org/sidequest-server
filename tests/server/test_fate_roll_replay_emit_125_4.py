"""RED — build_fate_roll_payload must EMIT the dice-animation replay fields
(Story 125-4, ADR-144 F3g follow-up).

F3c projected the engine's ``FateOutcome`` onto ``FateRollPayload`` but the 3D
FateDiceTray still rendered the idle pickup row: ``FATE_ROLL`` carried no
``throw_params``/``seed`` to replay (unlike ``DICE_RESULT``). 125-4 makes both
REQUIRED on the payload — but a required field is only half the fix. The
PROJECTION is the production caller (``handlers/fate_action.py`` →
``build_fate_roll_payload(result.action_roll)``), so the wiring test that
matters is: does the projection actually POPULATE a real gesture + seed, or is
it winging it? (CLAUDE.md "Every Test Suite Needs a Wiring Test" / "Verify
Wiring, Not Just Existence".)

These call the REAL projection against real ``FateOutcome``s and assert the
emitted payload carries a genuine ``ThrowParams`` and an ``int`` seed — and that
adding the replay fields did not disturb the existing legibility mapping.

RED today: ``build_fate_roll_payload`` constructs ``FateRollPayload`` WITHOUT
``throw_params``/``seed``; once those are required the call raises
``ValidationError`` (the projection has not caught up to the contract).
"""

from __future__ import annotations

from sidequest.game.ruleset.fate_resolution import FateOutcome, FateTier, ladder_name
from sidequest.protocol.dice import ThrowParams


def _succeed_outcome() -> FateOutcome:
    """4dF = (+1,+1,0,-1) -> roll 1; ladder 4 vs opposition 2 -> +2 shifts -> Succeed."""
    return FateOutcome(
        dice=(1, 1, 0, -1),
        roll_total=1,
        ladder_total=4,
        opposition=2,
        shifts=2,
        tier=FateTier.Succeed,
    )


def _style_outcome() -> FateOutcome:
    return FateOutcome(
        dice=(1, 1, 1, 0),
        roll_total=3,
        ladder_total=6,
        opposition=3,
        shifts=3,
        tier=FateTier.SucceedWithStyle,
    )


def test_projection_emits_a_real_throw_params_gesture():
    """The projection populates a ThrowParams — not None — so the 3D dice can
    animate instead of falling back to the idle (null) render."""
    from sidequest.game.ruleset.fate_projection import build_fate_roll_payload

    p = build_fate_roll_payload(_succeed_outcome())
    assert isinstance(p.throw_params, ThrowParams)
    # A gesture, not a degenerate zero-throw: at least one component is non-zero
    # along velocity or angular (an all-zero throw would never tumble).
    assert any(p.throw_params.velocity) or any(p.throw_params.angular)


def test_projection_emits_an_int_seed():
    """The replay seed is an int (the spectator-replay contract, per DICE_RESULT)."""
    from sidequest.game.ruleset.fate_projection import build_fate_roll_payload

    p = build_fate_roll_payload(_style_outcome())
    assert isinstance(p.seed, int)


def test_replay_fields_do_not_disturb_the_legibility_mapping():
    """Adding throw_params/seed must not regress the faithful F3c mapping."""
    from sidequest.game.ruleset.fate_projection import build_fate_roll_payload

    out = _succeed_outcome()
    p = build_fate_roll_payload(out)
    assert tuple(p.dice) == out.dice
    assert p.roll_total == out.roll_total
    assert p.ladder_total == out.ladder_total
    assert p.shifts == out.shifts
    assert p.opposition == out.opposition
    assert p.tier == out.tier
    assert p.ladder_name == ladder_name(out.ladder_total)
    assert p.succeeded_with_style is False
