import pytest
from pydantic import ValidationError
from sidequest.genre.models.rules import RulesConfig, SwnConfig

SIX = ["Physique", "Reflex", "Intellect", "Cunning", "Resolve", "Influence"]
GOOD_MAP = {
    "STRENGTH": "Physique", "CONSTITUTION": "Resolve", "DEXTERITY": "Reflex",
    "INTELLIGENCE": "Intellect", "WISDOM": "Cunning", "CHARISMA": "Influence",
}


def test_swn_pack_with_complete_map_validates():
    rc = RulesConfig(ruleset="swn", ability_score_names=SIX,
                     swn=SwnConfig(attribute_map=GOOD_MAP))
    assert rc.swn.attribute_map["WISDOM"] == "Cunning"


def test_swn_pack_missing_attribute_map_fails_loud():
    # ruleset=swn auto-populates SwnConfig() with an EMPTY map -> must reject
    with pytest.raises(ValidationError, match="attribute_map"):
        RulesConfig(ruleset="swn", ability_score_names=SIX)


def test_swn_pack_missing_one_key_fails_loud():
    partial = {k: v for k, v in GOOD_MAP.items() if k != "WISDOM"}
    with pytest.raises(ValidationError, match="WISDOM"):
        RulesConfig(ruleset="swn", ability_score_names=SIX,
                    swn=SwnConfig(attribute_map=partial))


def test_swn_pack_map_to_undeclared_stat_fails_loud():
    bad = {**GOOD_MAP, "WISDOM": "Nonexistent"}
    with pytest.raises(ValidationError, match="Nonexistent"):
        RulesConfig(ruleset="swn", ability_score_names=SIX,
                    swn=SwnConfig(attribute_map=bad))


def test_native_pack_ignores_attribute_map():
    # native packs never carry swn; no attribute_map requirement
    rc = RulesConfig(ruleset="native", ability_score_names=SIX)
    assert rc.swn is None
