"""Story 86-3 (Plan 3): CWN Vehicle Chases §2.6.2 — pace / pursuit math.

Faithful port of Cities Without Number SRD §2.6.2 (Chases and Pursuit),
extracted in the epic design doc §4.2
(``docs/superpowers/specs/2026-06-04-road-warrior-cwn-rig-combat-design.md``):

    The fleeing driver rolls Drive (usually +Dex) — that total IS the pace.
    Passengers may hinder pursuit (skill checks, +1 each, up to +3).
    Each pursuing vehicle rolls Dex/Drive vs the pace, modified by situation:
        can't directly see the pursued   −2
        pursuer flying / pursued not      +3
        pursued flying / pursuer not      −3
        spotter relaying target           +1
        local-terrain knowledge          −2..+2
        half-hearted pursuit              −1
        enraged / vengeful                +1
    Beat the pace  → catch up (→ vehicle combat, Plan 2).
    Tie or under   → fall behind / escape.

Pure, stateless calculation — no game state, no OTEL — mirroring the 86-2
``vehicle_combat.py`` precedent ("the caller supplies the d20"). RED until
Dev adds ``sidequest/game/chase_pace.py``.

Proposed seam (TEA contract, open to Dev refinement):
    chase_check_total(*, d20, attribute_modifier, drive_skill) -> int
    hinder_penalty(passenger_successes: int) -> int
    resolve_pursuit(*, pursuer_total, pace,
                    situational_modifier=0, hinder=0) -> PursuitResult
        — PursuitResult.outcome is CAUGHT iff the pursuer strictly beats the
          pace; CAUGHT also sets converges_to_combat=True (the §2.6.2
          "→ vehicle combat" hand-off into Plan 2).
"""

from __future__ import annotations

import pytest

from sidequest.game.chase_pace import (
    ENRAGED_VENGEFUL,
    HALF_HEARTED_PURSUIT,
    MAX_HINDER,
    PURSUED_FLYING_PURSUER_NOT,
    PURSUER_CANNOT_SEE,
    PURSUER_FLYING_PURSUED_NOT,
    SPOTTER_RELAYING,
    TERRAIN_KNOWLEDGE_MAX,
    TERRAIN_KNOWLEDGE_MIN,
    PursuitOutcome,
    chase_check_total,
    hinder_penalty,
    resolve_pursuit,
)

# ── chase_check_total: Drive, usually +Dex ──────────────────────────


def test_chase_check_sums_d20_attribute_and_drive() -> None:
    """A chase check is d20 + attribute modifier (usually Dex) + Drive skill.
    Both the flee pace and the pursuer roll use this shape (§2.6.2 'Drive,
    usually +Dex')."""
    assert chase_check_total(d20=12, attribute_modifier=2, drive_skill=1) == 15


def test_chase_check_includes_attribute_even_with_no_skill() -> None:
    """The Dex (attribute) bonus applies even to an unskilled driver — the
    pace is not Drive-skill-only. Guards against dropping the '+Dex'."""
    assert chase_check_total(d20=10, attribute_modifier=3, drive_skill=0) == 13


def test_chase_check_includes_skill_even_with_no_attribute() -> None:
    """The Drive skill applies even at a 0 attribute modifier. Guards
    against dropping the Drive term."""
    assert chase_check_total(d20=10, attribute_modifier=0, drive_skill=2) == 12


def test_chase_check_applies_negative_attribute_modifier() -> None:
    """A clumsy driver's negative Dex modifier lowers the pace — the signed
    modifier is applied, not clamped."""
    assert chase_check_total(d20=8, attribute_modifier=-1, drive_skill=0) == 7


# ── hinder_penalty: passengers hinder pursuit, +1 each, cap +3 ───────


def test_hinder_zero_successes_is_zero() -> None:
    """No passenger help → no hindrance to the pursuit."""
    assert hinder_penalty(0) == 0


def test_hinder_one_success_is_one() -> None:
    """Each passenger success hinders pursuit by +1."""
    assert hinder_penalty(1) == 1


def test_hinder_three_successes_is_three() -> None:
    """Three successes → the full +3."""
    assert hinder_penalty(3) == 3


def test_hinder_caps_at_three() -> None:
    """§2.6.2 caps passenger hindrance at +3 no matter how many succeed —
    a fourth (or fifth) success adds nothing."""
    assert hinder_penalty(4) == MAX_HINDER == 3
    assert hinder_penalty(9) == 3


def test_hinder_rejects_negative_successes() -> None:
    """A negative success count is a caller bug, not a silent 0 (No Silent
    Fallbacks)."""
    with pytest.raises(ValueError):
        hinder_penalty(-1)


# ── situational modifier constants: values and signs ────────────────


def test_situational_modifier_values_match_srd() -> None:
    """The §2.6.2 situational modifiers, exact values and signs. A sign flip
    here would silently invert who wins the chase."""
    assert PURSUER_CANNOT_SEE == -2
    assert PURSUER_FLYING_PURSUED_NOT == 3
    assert PURSUED_FLYING_PURSUER_NOT == -3
    assert SPOTTER_RELAYING == 1
    assert HALF_HEARTED_PURSUIT == -1
    assert ENRAGED_VENGEFUL == 1
    assert TERRAIN_KNOWLEDGE_MIN == -2
    assert TERRAIN_KNOWLEDGE_MAX == 2


# ── resolve_pursuit: beat the pace → caught → combat ────────────────


def test_pursuer_beats_pace_is_caught_and_converges_to_combat() -> None:
    """Beating the pace catches the quarry and hands off into vehicle
    combat (§2.6.2 '→ vehicle combat', the Plan 2 convergence)."""
    result = resolve_pursuit(pursuer_total=16, pace=15)
    assert result.outcome is PursuitOutcome.CAUGHT
    assert result.converges_to_combat is True
    assert result.pursuer_effective == 16
    assert result.pace == 15


def test_tie_does_not_catch() -> None:
    """A tie does NOT close the gap — §2.6.2 requires *beating* the pace.
    Tie or under → fall behind / escape."""
    result = resolve_pursuit(pursuer_total=15, pace=15)
    assert result.outcome is PursuitOutcome.EVADED
    assert result.converges_to_combat is False


def test_pursuer_under_pace_evades() -> None:
    """Rolling under the pace means the quarry pulls away."""
    result = resolve_pursuit(pursuer_total=11, pace=15)
    assert result.outcome is PursuitOutcome.EVADED
    assert result.converges_to_combat is False


def test_situational_modifier_can_turn_a_loss_into_a_catch() -> None:
    """A pursuer one short of the pace catches up with a +3 (e.g. flying
    over a grounded quarry): 14 + 3 = 17 > 15."""
    result = resolve_pursuit(
        pursuer_total=14, pace=15, situational_modifier=PURSUER_FLYING_PURSUED_NOT
    )
    assert result.pursuer_effective == 17
    assert result.outcome is PursuitOutcome.CAUGHT


def test_negative_situational_modifier_can_turn_a_catch_into_an_escape() -> None:
    """Losing sight of the quarry (−2) drops a would-be catch under the
    pace: 16 − 2 = 14 < 15."""
    result = resolve_pursuit(pursuer_total=16, pace=15, situational_modifier=PURSUER_CANNOT_SEE)
    assert result.pursuer_effective == 14
    assert result.outcome is PursuitOutcome.EVADED


def test_hinder_is_subtracted_from_the_pursuer() -> None:
    """Passenger hindrance works against the pursuer's effective roll: a
    +3 hinder drops 17 to 14, under the pace of 15."""
    result = resolve_pursuit(pursuer_total=17, pace=15, hinder=3)
    assert result.pursuer_effective == 14
    assert result.outcome is PursuitOutcome.EVADED


def test_effective_aggregates_situational_and_hinder() -> None:
    """Effective pursuer roll = total + situational − hinder. Spotter (+1)
    and a single hinder (−1) net to zero against a 15 → still a catch at 16."""
    result = resolve_pursuit(
        pursuer_total=16,
        pace=15,
        situational_modifier=SPOTTER_RELAYING,
        hinder=hinder_penalty(1),
    )
    assert result.pursuer_effective == 16  # 16 + 1 − 1
    assert result.outcome is PursuitOutcome.CAUGHT


@pytest.mark.parametrize(
    ("pursuer_total", "pace", "sit", "hinder", "caught"),
    [
        (20, 10, 0, 0, True),  # blown past the pace
        (10, 20, 0, 0, False),  # hopelessly behind
        (16, 15, 0, 0, True),  # squeak past
        (15, 15, 0, 0, False),  # tie loses
        (15, 15, 1, 0, True),  # +1 breaks the tie
        (15, 15, 0, 1, False),  # hinder holds the tie-loss
        (18, 15, -3, 0, False),  # quarry flies away
        (12, 15, 3, 0, False),  # +3 still short (15 == pace, tie loses)
    ],
)
def test_resolve_pursuit_table(
    pursuer_total: int, pace: int, sit: int, hinder: int, caught: bool
) -> None:
    """Spread across the catch boundary — guards the strict-beat comparison
    and the situational/hinder aggregation against off-by-one and sign
    errors."""
    result = resolve_pursuit(
        pursuer_total=pursuer_total,
        pace=pace,
        situational_modifier=sit,
        hinder=hinder,
    )
    assert (result.outcome is PursuitOutcome.CAUGHT) is caught
    assert result.converges_to_combat is caught
