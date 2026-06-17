import pytest
from pydantic import ValidationError

from sidequest.genre.models.rules import CwnConfig, FateConfig, RulesConfig

_WN_ATTRS = ("STRENGTH", "CONSTITUTION", "DEXTERITY", "INTELLIGENCE", "WISDOM", "CHARISMA")
_FLAVOR_STATS = ("Strength", "Constitution", "Dexterity", "Intelligence", "Wisdom", "Charisma")
_CWN_AMAP = dict(zip(_WN_ATTRS, _FLAVOR_STATS, strict=True))


def _conf(mode: str) -> dict:
    return {
        "type": "duel",
        "label": "Duel",
        "category": "social",
        "resolution_mode": mode,
        "player_metric": {"name": "x", "starting": 0, "threshold": 3},
        "opponent_metric": {"name": "y", "starting": 0, "threshold": 3},
    }


def _fate_rules(**kwargs) -> RulesConfig:
    """Minimal valid Fate RulesConfig (empty FateConfig; all fields have defaults)."""
    return RulesConfig(ruleset="fate", fate=FateConfig(), **kwargs)


def _cwn_rules(**kwargs) -> RulesConfig:
    """Minimal valid CWN RulesConfig — all six WN attributes wired."""
    return RulesConfig(
        ruleset="cwn",
        ability_score_names=list(_FLAVOR_STATS),
        cwn=CwnConfig(attribute_map=_CWN_AMAP),
        **kwargs,
    )


def test_fate_pack_rejects_opposed_check():
    with pytest.raises(ValidationError, match="opposed_check"):
        _fate_rules(confrontations=[_conf("opposed_check")])


def test_fate_pack_allows_contest():
    cfg = _fate_rules(confrontations=[_conf("contest")])
    assert cfg.confrontations[0].resolution_mode == "contest"


def test_wn_pack_still_allows_opposed_check():
    # The invariant (spec §0): cwn/awn keep opposed_check via the dial engine.
    cfg = _cwn_rules(confrontations=[_conf("opposed_check")])
    assert cfg.confrontations[0].resolution_mode == "opposed_check"
