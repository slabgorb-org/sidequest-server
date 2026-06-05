"""AWN config validation — `AwnConfig` + `_validate_awn` wiring (Story 88-1, Items 3).

Mirrors test_cwn_config / test_wwn_config: AWN is a faithful CWN port, so
``AwnConfig`` subclasses ``CwnConfig`` and inherits system_strain/trauma/
attribute_map verbatim. ``hacking`` stays ``None`` (AWN has no cyberspace net-run).
``RulesConfig._validate_awn`` mirrors ``_validate_cwn`` exactly: complete six-key
attribute_map, flavor names declared in ability_score_names, strain max_source a
key of the map, and a valid major_injury_save. ``ruleset_config()`` returns the
awn block when ``ruleset == "awn"``.

These tests fail until Item 3 lands (no ``AwnConfig`` class, no ``awn`` field on
``RulesConfig``, no ``_validate_awn``, no ``awn`` branch in ``ruleset_config()``).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.genre.models.rules import (
    AwnConfig,
    CwnConfig,
    RulesConfig,
    SystemStrainConfig,
    TraumaConfig,
)

# A post-apocalyptic pack maps the six SWN/CWN attributes to its flavor stats.
# For Plan 1 we use the canonical names as flavor (the standard-six sweep is 88-2).
_AWN_FLAVOR = ["Strength", "Dexterity", "Constitution", "Intelligence", "Wisdom", "Charisma"]
_AWN_AMAP = {
    "STRENGTH": "Strength",
    "DEXTERITY": "Dexterity",
    "CONSTITUTION": "Constitution",
    "INTELLIGENCE": "Intelligence",
    "WISDOM": "Wisdom",
    "CHARISMA": "Charisma",
}


def test_awn_config_is_a_cwn_config_subclass():
    # Capability binding (§11.1): the whole point of the subclass is that an
    # AwnConfig IS a CwnConfig, so every `isinstance(cfg, CwnConfig)` site rides
    # free. If this relationship breaks, the FREE sites silently stop covering AWN.
    assert issubclass(AwnConfig, CwnConfig)
    assert isinstance(AwnConfig(attribute_map=_AWN_AMAP), CwnConfig)


def test_awn_config_inherits_cwn_swn_defaults():
    cfg = AwnConfig(attribute_map=_AWN_AMAP)
    assert cfg.save_base == 15
    assert cfg.unarmored_ac == 10
    assert cfg.difficulties["formidable"] == 14
    assert cfg.attribute_map["CONSTITUTION"] == "Constitution"


def test_awn_config_has_strain_and_trauma_defaults():
    cfg = AwnConfig(attribute_map=_AWN_AMAP)
    assert cfg.system_strain.max_source == "CONSTITUTION"
    assert cfg.trauma.default_trauma_target == 6


def test_awn_config_hacking_defaults_to_none():
    # AWN has the "Program" skill but no cyberspace net-run ladder — hacking
    # stays None (inherited CwnConfig default). The hacking gates correctly skip AWN.
    cfg = AwnConfig(attribute_map=_AWN_AMAP)
    assert cfg.hacking is None


def test_rules_awn_requires_complete_attribute_map():
    with pytest.raises(ValidationError, match="attribute_map"):
        RulesConfig(
            ruleset="awn",
            ability_score_names=_AWN_FLAVOR,
            awn=AwnConfig(attribute_map={"STRENGTH": "Strength"}),  # missing 5 keys
        )


def test_rules_awn_rejects_flavor_not_in_ability_scores():
    bad = dict(_AWN_AMAP, CHARISMA="Mutie")  # Mutie not declared in ability_score_names
    with pytest.raises(ValidationError, match="ability_score_names"):
        RulesConfig(
            ruleset="awn", ability_score_names=_AWN_FLAVOR, awn=AwnConfig(attribute_map=bad)
        )


def test_rules_awn_accepts_complete_map():
    rules = RulesConfig(
        ruleset="awn", ability_score_names=_AWN_FLAVOR, awn=AwnConfig(attribute_map=_AWN_AMAP)
    )
    assert rules.awn is not None
    assert rules.awn.attribute_map["INTELLIGENCE"] == "Intelligence"


def test_rules_awn_with_no_config_block_fails_loud():
    # ruleset='awn' but no awn block: validator auto-populates an empty AwnConfig,
    # then rejects it for the missing attribute_map (no silent default).
    with pytest.raises(ValidationError, match="attribute_map"):
        RulesConfig(ruleset="awn", ability_score_names=_AWN_FLAVOR)


def test_rules_awn_empty_attribute_map_hits_none_authored_branch():
    # An explicitly empty attribute_map ({}) takes the distinct "none authored"
    # branch (rules.py:1238) — separate from the partial-map "missing required keys"
    # branch. Asserts the fail-loud message that guards against a silent default.
    with pytest.raises(ValidationError, match="none authored"):
        RulesConfig(ruleset="awn", ability_score_names=_AWN_FLAVOR, awn=AwnConfig(attribute_map={}))


def test_rules_awn_strain_source_must_be_in_map():
    cfg = AwnConfig(
        attribute_map=_AWN_AMAP, system_strain=SystemStrainConfig(max_source="NONEXISTENT")
    )
    with pytest.raises(ValidationError, match="max_source"):
        RulesConfig(ruleset="awn", ability_score_names=_AWN_FLAVOR, awn=cfg)


def test_rules_awn_rejects_bad_major_injury_save():
    cfg = AwnConfig(attribute_map=_AWN_AMAP, trauma=TraumaConfig(major_injury_save="bogus"))
    with pytest.raises(ValidationError, match="major_injury_save"):
        RulesConfig(ruleset="awn", ability_score_names=_AWN_FLAVOR, awn=cfg)


def test_rules_awn_accepts_luck_major_injury_save():
    # CWN/AWN added the Luck save; major_injury_save="luck" must be accepted.
    cfg = AwnConfig(attribute_map=_AWN_AMAP, trauma=TraumaConfig(major_injury_save="luck"))
    rules = RulesConfig(ruleset="awn", ability_score_names=_AWN_FLAVOR, awn=cfg)
    assert rules.awn is not None
    assert rules.awn.trauma.major_injury_save == "luck"


def test_ruleset_config_returns_awn_block():
    rules = RulesConfig(
        ruleset="awn", ability_score_names=_AWN_FLAVOR, awn=AwnConfig(attribute_map=_AWN_AMAP)
    )
    cfg = rules.ruleset_config()
    assert cfg is rules.awn
    # And it satisfies the capability check used by the FREE binding sites.
    assert isinstance(cfg, CwnConfig)


def test_ruleset_config_awn_is_none_when_ruleset_is_not_awn():
    # No cross-contamination: a cwn pack's ruleset_config() must not return an awn block.
    rules = RulesConfig(
        ruleset="cwn",
        ability_score_names=_AWN_FLAVOR,
        cwn=CwnConfig(attribute_map=_AWN_AMAP),
    )
    assert rules.awn is None
    assert rules.ruleset_config() is rules.cwn
