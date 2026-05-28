from __future__ import annotations

from sidequest.genre.models.rules import CwnConfig, RulesConfig, SwnConfig

_FLAVOR = ["Brawn", "Reflex", "Body", "Tech", "Instinct", "Cool"]
_AMAP = {
    "STRENGTH": "Brawn",
    "DEXTERITY": "Reflex",
    "CONSTITUTION": "Body",
    "INTELLIGENCE": "Tech",
    "WISDOM": "Instinct",
    "CHARISMA": "Cool",
}


def test_ruleset_config_returns_cwn_block_for_cwn():
    rules = RulesConfig(
        ruleset="cwn", ability_score_names=_FLAVOR, cwn=CwnConfig(attribute_map=_AMAP)
    )
    cfg = rules.ruleset_config()
    assert isinstance(cfg, CwnConfig)
    assert cfg.attribute_map["INTELLIGENCE"] == "Tech"


def test_ruleset_config_returns_swn_block_for_swn():
    rules = RulesConfig(
        ruleset="swn", ability_score_names=_FLAVOR, swn=SwnConfig(attribute_map=_AMAP)
    )
    cfg = rules.ruleset_config()
    assert isinstance(cfg, SwnConfig)
    # A CwnConfig is also a SwnConfig (subclass), so guard against the wrong block:
    assert not isinstance(cfg, CwnConfig)


def test_ruleset_config_returns_none_for_native():
    rules = RulesConfig(ruleset="native")
    assert rules.ruleset_config() is None
