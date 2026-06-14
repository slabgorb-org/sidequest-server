from __future__ import annotations

import random

import pytest

from sidequest.game.ruleset.fate import FateRulesetModule
from sidequest.game.ruleset.fate_resolution import FateOutcome, Opposition


def test_slug():
    assert FateRulesetModule.slug == "fate"


def test_fate_does_not_award_native_turn_xp():
    # Fate advances by milestones, not an XP tick (ADR-144).
    assert FateRulesetModule().awards_native_turn_xp is False


def test_resolve_action_returns_outcome():
    module = FateRulesetModule()
    outcome = module.resolve_action(
        skill_rating=2,
        opposition=Opposition(value=1, kind="passive"),
        rng=random.Random(3),
        actor="Detective",
    )
    assert isinstance(outcome, FateOutcome)
    assert outcome.shifts == outcome.ladder_total - 1


@pytest.mark.parametrize(
    "call",
    [
        lambda m: m.find_confrontation([], "fight"),
        lambda m: m.stat_modifier({}, "STRENGTH"),
        lambda m: m.compute_dc(None),
        lambda m: m.attack_params(
            beat=None, attacker_stats={}, attacker_core=None, target_core=None
        ),
        lambda m: m.resolve_damage(beat=None, actor_core=None, pack=None),
        lambda m: m.apply_beat(
            encounter=None,
            actor=None,
            beat=None,
            outcome=None,
            turn=0,
            edge_resolver=None,
            damage_resolver=None,
        ),
    ],
)
def test_d20_surface_fails_loud(call):
    # Fate's paradigm has no beats/DCs/d20 — these raise (No Silent Fallbacks)
    # until ADR-144 F5 demotes them to base default-raise.
    module = FateRulesetModule()
    with pytest.raises(NotImplementedError):
        call(module)
