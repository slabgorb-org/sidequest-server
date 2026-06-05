"""Story 86-2 (AC1): Vehicle AC — stationary −4, moving +driver Drive.

CWN Vehicle Combat §2.4.8 (extracted in the design doc §4.1):

    Vehicle AC applies to melee & ranged attacks against the vehicle.
    Stationary = −4 (penalty: a parked rig is easy to hit).
    Moving     = + the driver's Drive skill to AC.
    Attacks vs the vehicle use the *vehicle's* AC, not the driver's.

This is a pure, stateless calculation — no game state, no OTEL. RED
until Dev adds ``sidequest/game/vehicle_combat.py`` with a
``vehicle_ac`` helper.

Proposed seam (TEA contract, open to Dev refinement):
    vehicle_ac(base_ac: int, *, drive_modifier: int, moving: bool) -> int
"""

from __future__ import annotations

import pytest

from sidequest.game.vehicle_combat import vehicle_ac


def test_stationary_vehicle_takes_minus_four() -> None:
    """A stationary vehicle is −4 AC relative to its base (easier to hit).
    Driver Drive does not apply when parked."""
    assert vehicle_ac(10, drive_modifier=2, moving=False) == 6


def test_moving_vehicle_adds_driver_drive_modifier() -> None:
    """A moving vehicle adds the driver's Drive skill modifier to base AC."""
    assert vehicle_ac(10, drive_modifier=2, moving=True) == 12


def test_moving_with_zero_drive_is_base_ac() -> None:
    """A moving vehicle whose driver has no Drive bonus sits at base AC —
    the motion bonus is the Drive modifier itself, not a flat +N."""
    assert vehicle_ac(10, drive_modifier=0, moving=True) == 10


def test_stationary_penalty_is_independent_of_drive() -> None:
    """The −4 stationary penalty is fixed; a high-Drive driver gets no
    benefit while parked (the bonus is *for motion*)."""
    assert vehicle_ac(10, drive_modifier=5, moving=False) == 6


def test_negative_drive_modifier_lowers_moving_ac() -> None:
    """A negative Drive modifier (unskilled driver) lowers the moving AC —
    the helper applies the signed modifier, it does not clamp at base."""
    assert vehicle_ac(10, drive_modifier=-1, moving=True) == 9


@pytest.mark.parametrize(
    ("base", "drive", "moving", "expected"),
    [
        (8, 1, True, 9),
        (8, 1, False, 4),
        (12, 3, True, 15),
        (12, 3, False, 8),
    ],
)
def test_vehicle_ac_table(base: int, drive: int, moving: bool, expected: int) -> None:
    """Spread of base/Drive/motion combinations — guards against a sign
    flip or a swapped moving/stationary branch."""
    assert vehicle_ac(base, drive_modifier=drive, moving=moving) == expected
