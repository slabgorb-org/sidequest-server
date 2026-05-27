"""SWN P4: roll_initiative is optional on the seam — native (and the base
default) return None (no ordering); only SWN populates it."""

from __future__ import annotations

import random

from sidequest.game.ruleset.native import NativeRulesetModule


def test_native_roll_initiative_returns_none():
    result = NativeRulesetModule().roll_initiative(
        actor_dex_scores={"Rux": 12, "Raider": 11},
        rng=random.Random(1),
    )
    assert result is None
