# tests/game/ruleset/test_dial_unchanged.py
import pytest

from sidequest.game.ruleset.dial import DialRulesetModule
from sidequest.genre.models.rules import BeatDef

_N = DialRulesetModule()


def _beat(base=2):
    return BeatDef(id="b", label="B", kind="strike", base=base, stat_check="STRENGTH")


def test_dial_attack_params_equals_stat_mod_and_compute_dc():
    beat = _beat(base=2)
    stats = {"STRENGTH": 16}
    params = _N.attack_params(beat=beat, attacker_stats=stats, attacker_core=None, target_core=None)
    assert params.modifier == _N.stat_modifier(stats, "STRENGTH")  # +3
    assert params.target_number == _N.compute_dc(beat)  # 14


# ---------------------------------------------------------------------------
# Base contract — DialRulesetModule.check_params + save_params raise NotImplementedError
# ---------------------------------------------------------------------------


def test_dial_check_params_raises_not_implemented():
    """DialRulesetModule.check_params must raise NotImplementedError (base fails loud)."""
    with pytest.raises(NotImplementedError):
        _N.check_params(
            stats={"STRENGTH": 10},
            attribute="STRENGTH",
            skill_level=0,
            difficulty_key="tricky",
            label="test",
            cfg=None,
        )


def test_dial_save_params_raises_not_implemented():
    """DialRulesetModule.save_params must raise NotImplementedError (base fails loud)."""
    with pytest.raises(NotImplementedError):
        _N.save_params(
            stats={"WISDOM": 10},
            save="mental",
            level=1,
            label="test",
            cfg=None,
        )
