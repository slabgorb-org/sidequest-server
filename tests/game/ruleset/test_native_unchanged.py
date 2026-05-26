# tests/game/ruleset/test_native_unchanged.py
from sidequest.game.ruleset.native import NativeRulesetModule
from sidequest.genre.models.rules import BeatDef

_N = NativeRulesetModule()


def _beat(base=2):
    return BeatDef(id="b", label="B", kind="strike", base=base, stat_check="STRENGTH")


def test_native_attack_params_equals_stat_mod_and_compute_dc():
    beat = _beat(base=2)
    stats = {"STRENGTH": 16}
    params = _N.attack_params(beat=beat, attacker_stats=stats, attacker_core=None, target_core=None)
    assert params.modifier == _N.stat_modifier(stats, "STRENGTH")   # +3
    assert params.target_number == _N.compute_dc(beat)              # 14
