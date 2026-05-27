"""SWN P4: SwnRulesetModule.roll_initiative — 1d8 + swn_attribute_modifier(DEX),
sorted descending, one InitiativeEntry per actor, deterministic under a seed."""

from __future__ import annotations

import random

from sidequest.game.ruleset.swn import SwnRulesetModule, swn_attribute_modifier
from sidequest.protocol.models import InitiativeEntry


def test_one_entry_per_actor_sorted_descending():
    mod = SwnRulesetModule()
    result = mod.roll_initiative(
        actor_dex_scores={"Rux": 14, "Raider": 8, "Scout": 18},
        rng=random.Random(42),
    )
    assert result is not None
    assert all(isinstance(e, InitiativeEntry) for e in result)
    assert {e.token_id for e in result} == {"Rux", "Raider", "Scout"}
    values = [e.value for e in result]
    assert values == sorted(values, reverse=True)


def test_value_is_d8_plus_dex_mod_in_range():
    mod = SwnRulesetModule()
    result = mod.roll_initiative(actor_dex_scores={"Ace": 18}, rng=random.Random(7))
    assert result is not None
    (entry,) = result
    assert swn_attribute_modifier(18) == 2
    assert 3 <= entry.value <= 10


def test_deterministic_under_seed():
    mod = SwnRulesetModule()
    scores = {"A": 12, "B": 13, "C": 7}
    a = mod.roll_initiative(actor_dex_scores=scores, rng=random.Random(99))
    b = mod.roll_initiative(actor_dex_scores=scores, rng=random.Random(99))
    assert [(e.token_id, e.value) for e in a] == [(e.token_id, e.value) for e in b]


def test_empty_actor_map_returns_empty_list():
    result = SwnRulesetModule().roll_initiative(actor_dex_scores={}, rng=random.Random(1))
    assert result == []
