"""Characterization net for the WN-family ruleset modules (ADR-142).

Pins each sibling's CURRENT mechanical output so the upcoming WN core extraction
(shared WithoutNumberRulesetModule base + reparenting) is provably output-preserving.

ALL fixtures are synthetic — no real genre packs are loaded.
"""

from __future__ import annotations

import random

import pytest

from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.system_strain import SystemStrainPool
from sidequest.game.wwn_magic import EffortPool
from sidequest.genre.models.inventory import DamageSpec
from sidequest.genre.models.rules import (
    AwnConfig,
    BeatDef,
    CwnConfig,
    SwnConfig,
    WwnConfig,
)

# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

# Attribute map used across all four rulesets (SWN/WWN/CWN/AWN share six canonical
# attributes; flavor names are arbitrary here — we use the same map everywhere so
# save_params can resolve them).
_AMAP = {
    "STRENGTH": "Brawn",
    "CONSTITUTION": "Body",
    "DEXTERITY": "Reflex",
    "INTELLIGENCE": "Tech",
    "WISDOM": "Instinct",
    "CHARISMA": "Cool",
}

# Four module instances resolved from the registry (wiring test implied).
_SWN = get_ruleset_module("swn")
_WWN = get_ruleset_module("wwn")
_CWN = get_ruleset_module("cwn")
_AWN = get_ruleset_module("awn")

# Config instances keyed by slug.
_SWN_CFG = SwnConfig(attribute_map=_AMAP)
_WWN_CFG = WwnConfig(attribute_map=_AMAP)
_CWN_CFG = CwnConfig(attribute_map=_AMAP)
_AWN_CFG = AwnConfig(attribute_map=_AMAP)

_CFG = {
    "swn": _SWN_CFG,
    "wwn": _WWN_CFG,
    "cwn": _CWN_CFG,
    "awn": _AWN_CFG,
}
_MOD = {
    "swn": _SWN,
    "wwn": _WWN,
    "cwn": _CWN,
    "awn": _AWN,
}

# Stats used in attack / save tests.  Reflex(DEX)=14 → modifier +1.
_STATS = {"Brawn": 10, "Body": 12, "Reflex": 14, "Tech": 10, "Instinct": 8, "Cool": 13}

# Beat used for attack_params: stat_check = "Reflex" (DEX-flavored), attack_bonus=2, combat_skill=1.
_BEAT = BeatDef.model_validate(
    {
        "id": "strike",
        "label": "Strike",
        "kind": "strike",
        "base": 0,
        "stat_check": "Reflex",
        "attack_bonus": 2,
        "combat_skill": 1,
    }
)

# Target core: AC 13.
_TARGET = CreatureCore(name="Raider", description="d", personality="p", armor_class=13)


# ---------------------------------------------------------------------------
# Row 1 — attack_params (all four slugs)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", ["swn", "wwn", "cwn", "awn"])
def test_characterize_attack_params(slug):
    """Pin attack_params(modifier, target_number) across all four WN siblings.

    Reflex=14 → +1 (SWN tight curve: 14-17 → +1).
    attack_bonus=2 + combat_skill=1 + attr_mod=1 = modifier 4.
    target_number = target AC = 13.
    """
    params = _MOD[slug].attack_params(
        beat=_BEAT,
        attacker_stats=_STATS,
        attacker_core=None,
        target_core=_TARGET,
    )
    assert params.modifier == 4
    assert params.target_number == 13


# ---------------------------------------------------------------------------
# Row 2 — save_params for physical/evasion/mental (all four)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", ["swn", "wwn", "cwn", "awn"])
@pytest.mark.parametrize(
    "save, expected_modifier, expected_difficulty",
    [
        # physical: better of STR(Brawn=10→0) / CON(Body=12→0) = 0.  Level 1 → 15-(1-1)=15.
        ("physical", 0, 15),
        # evasion: better of DEX(Reflex=14→+1) / INT(Tech=10→0) = 1.  Level 1 → 15.
        ("evasion", 1, 15),
        # mental: better of WIS(Instinct=8→0) / CHA(Cool=13→0) = 0.  Level 1 → 15.
        ("mental", 0, 15),
    ],
)
def test_characterize_save_params_attribute_saves(
    slug, save, expected_modifier, expected_difficulty
):
    """Pin save_params for the three attribute saves across all four WN siblings."""
    params = _MOD[slug].save_params(
        stats=_STATS,
        save=save,
        level=1,
        label=f"{save} save",
        cfg=_CFG[slug],
    )
    assert (params.sides, params.count) == (20, 1)
    assert params.modifier == expected_modifier
    assert params.difficulty == expected_difficulty


# ---------------------------------------------------------------------------
# Row 3 — save_params for luck save (wwn/cwn/awn only; swn has no luck save)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", ["wwn", "cwn", "awn"])
def test_characterize_luck_save(slug):
    """Pin the luck save: no attribute modifier, target = save_base - (level-1).

    Level 1 → difficulty 15 - 0 = 15.  modifier always 0 (no attr mod).
    """
    params = _MOD[slug].save_params(
        stats=_STATS,
        save="luck",
        level=1,
        label="Luck save",
        cfg=_CFG[slug],
    )
    assert (params.sides, params.count) == (20, 1)
    assert params.modifier == 0
    assert params.difficulty == 15


# ---------------------------------------------------------------------------
# Row 4 — resolve_trauma (wwn/cwn/awn only; swn is passthrough)
# ---------------------------------------------------------------------------

# Seeded RNG: Random(42). The trauma die is "1d6"; base_total=8.
# The default trauma_target for CwnConfig/WwnConfig/AwnConfig is 6.
# random.Random(42).randint(1, 6) → we'll capture after first run.

_TRAUMA_SPEC = DamageSpec(dice="2d6", trauma_die="1d6", trauma_rating=2)


@pytest.mark.parametrize("slug", ["wwn", "cwn", "awn"])
def test_characterize_resolve_trauma(slug):
    """Pin (base_total, final_total, traumatic) for resolve_trauma with seed=42."""
    rng = random.Random(42)
    result = _MOD[slug].resolve_trauma(
        spec=_TRAUMA_SPEC,
        base_total=8,
        cfg=_CFG[slug],
        rng=rng,
    )
    # random.Random(42).randint(1,6) = 6 → meets target 6 → traumatic → final_total = 8*2 = 16
    assert result.base_total == 8
    assert result.final_total == 16
    assert result.traumatic is True


# ---------------------------------------------------------------------------
# Row 5 — apply_system_strain kind="temporary" (wwn/cwn/awn only)
# ---------------------------------------------------------------------------


def _strain_core(current=2, max=12, permanent=0) -> CreatureCore:
    return CreatureCore(
        name="Jax",
        description="runner",
        personality="cool",
        system_strain=SystemStrainPool(current=current, max=max, permanent=permanent),
    )


@pytest.mark.parametrize("slug", ["wwn", "cwn", "awn"])
def test_characterize_apply_system_strain_temporary(slug):
    """Pin (applied, current, max, delta) for apply_system_strain kind='temporary'."""
    core = _strain_core(current=2, max=12)
    result = _MOD[slug].apply_system_strain(
        core=core,
        kind="temporary",
        amount=3,
        source="test_drug",
        cfg=_CFG[slug],
    )
    # 2 + 3 = 5 < max 12 → applied, current=5, delta=3
    assert result.applied is True
    assert result.current == 5
    assert result.max == 12
    assert result.delta == 3


# ---------------------------------------------------------------------------
# Row 6 — commit_effort then reclaim_effort (all four)
#
# SWN psionics use source "psionic". WWN uses "high_mage". CWN/AWN use "high_mage"
# (they inherit the same effort engine from SwnRulesetModule via CwnRulesetModule).
# ---------------------------------------------------------------------------


def _effort_core(slug: str) -> CreatureCore:
    """Build a core with the appropriate effort source key seeded."""
    source = "psionic" if slug == "swn" else "high_mage"
    return CreatureCore(
        name="Caster",
        description="magic user",
        personality="methodical",
        hp=HpPool(current=10, max=10, base_max=10),
        effort={source: EffortPool(source=source, max=3)},
    )


@pytest.mark.parametrize("slug", ["swn", "wwn", "cwn", "awn"])
def test_characterize_commit_and_reclaim_effort(slug):
    """Pin (applied, available) after commit_effort then reclaim_effort."""
    source = "psionic" if slug == "swn" else "high_mage"
    core = _effort_core(slug)

    # Commit 2 points
    commit_result = _MOD[slug].commit_effort(
        core=core, source=source, points=2, duration="maintained"
    )
    assert commit_result.applied is True
    assert commit_result.available == 1  # 3 - 2 = 1

    # Reclaim the maintained commitment
    reclaim_result = _MOD[slug].reclaim_effort(core=core, source=source, trigger="maintained")
    assert reclaim_result.applied is True
    assert reclaim_result.available == 3  # back to full


# ---------------------------------------------------------------------------
# Row 7 — resolve_downed (wwn/cwn/awn only)
# ---------------------------------------------------------------------------


def _downed_core() -> CreatureCore:
    return CreatureCore(
        name="Hero",
        description="brave",
        personality="determined",
        hp=HpPool(current=0, max=10, base_max=10),
    )


@pytest.mark.parametrize("slug", ["wwn", "cwn", "awn"])
def test_characterize_resolve_downed(slug):
    """Pin that resolve_downed returns mortal=True and appends a Mortal Injury status."""
    core = _downed_core()
    # scene_traumatic=False so no save is rolled — deterministic, no RNG needed.
    result = _MOD[slug].resolve_downed(
        core=core,
        save_target=15,
        scene_traumatic=False,
        cfg=_CFG[slug],
        rng=random.Random(0),
    )
    assert result.mortal is True
    # Exactly one Mortal Injury status appended (no major injury when not traumatic).
    assert len(core.statuses) == 1
    assert "Mortal Injury" in core.statuses[0].text
