"""Story 126-7 (ADR-148): the player-path resolution primitive.

``resolve_action_from_faces`` resolves a Fate action from the client's settled
dF faces and NEVER touches an rng — the determinative half of the d20
physics-is-the-roll mirror. ``resolve_action`` (the rng/NPC path) is unchanged;
both feed the shared ``_build_outcome`` so the Fate ladder math (sum + skill +
invoke, classify) lives in one place (SOUL: bind the ruleset, don't rebalance).
"""

from __future__ import annotations

import random

import pytest

from sidequest.game.ruleset.fate_resolution import (
    Opposition,
    resolve_action,
    resolve_action_from_faces,
)


def test_from_faces_matches_equivalent_rng_roll():
    opp = Opposition(value=1, kind="passive")
    out = resolve_action_from_faces(
        skill_rating=3, opposition=opp, faces=(1, 1, 0, -1), invoke_bonus=0
    )
    # roll_total = 1+1+0-1 = 1 ; ladder_total = 1 + 3 = 4 ; shifts = 4 - 1 = 3
    assert out.dice == (1, 1, 0, -1)
    assert out.roll_total == 1
    assert out.ladder_total == 4
    assert out.shifts == 3


def test_from_faces_applies_invoke_bonus():
    opp = Opposition(value=0, kind="passive")
    out = resolve_action_from_faces(
        skill_rating=2, opposition=opp, faces=(0, 0, 0, 0), invoke_bonus=2
    )
    assert out.roll_total == 0
    assert out.ladder_total == 4  # 0 + 2 + 2


def test_from_faces_rejects_wrong_count():
    opp = Opposition(value=0, kind="passive")
    with pytest.raises(ValueError):
        resolve_action_from_faces(skill_rating=1, opposition=opp, faces=(1, 0, 1))


def test_from_faces_rejects_out_of_range():
    opp = Opposition(value=0, kind="passive")
    with pytest.raises(ValueError):
        resolve_action_from_faces(skill_rating=1, opposition=opp, faces=(1, 0, 1, 2))


def test_rng_path_unchanged():
    # The NPC path still rolls 4dF; the refactor must preserve its behavior.
    opp = Opposition(value=0, kind="passive")
    out = resolve_action(skill_rating=1, opposition=opp, rng=random.Random(7))
    assert -4 <= out.roll_total <= 4
    assert out.ladder_total == out.roll_total + 1
