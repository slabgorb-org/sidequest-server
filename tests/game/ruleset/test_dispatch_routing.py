"""Wiring test: dispatch resolves through the bound RulesetModule, not free functions."""

from __future__ import annotations

from unittest.mock import patch

from sidequest.game.ruleset.dial import DialRulesetModule
from tests.game.ruleset._dispatch_fixture import resolve_one_combat_beat


def test_dispatch_uses_bound_module_for_stat_and_dc():
    real = DialRulesetModule()
    calls = {"stat_modifier": 0, "compute_dc": 0}

    def spy_stat(stats, stat_check):
        calls["stat_modifier"] += 1
        return real.stat_modifier(stats, stat_check)

    def spy_dc(beat):
        calls["compute_dc"] += 1
        return real.compute_dc(beat)

    spy = DialRulesetModule()
    spy.stat_modifier = spy_stat  # type: ignore[method-assign]
    spy.compute_dc = spy_dc  # type: ignore[method-assign]

    with patch("sidequest.server.dispatch.dice.get_ruleset_module", return_value=spy):
        outcome = resolve_one_combat_beat()

    assert calls["stat_modifier"] >= 1, "dispatch did not route stat_modifier through the module"
    assert calls["compute_dc"] >= 1, "dispatch did not route compute_dc through the module"
    assert outcome.encounter_resolved in (True, False)  # smoke: a real outcome came back
