import pytest
from sidequest.game.ruleset.swn import SwnRulesetModule
from sidequest.genre.models.rules import SwnConfig

MAP = {
    "STRENGTH": "Physique", "CONSTITUTION": "Resolve", "DEXTERITY": "Reflex",
    "INTELLIGENCE": "Intellect", "WISDOM": "Cunning", "CHARISMA": "Influence",
}
CFG = SwnConfig(attribute_map=MAP)
MOD = SwnRulesetModule()

# Flavor-keyed stat block. Physique 14 -> +1, Resolve 8 -> 0, Cunning 18 -> +2, Influence 8 -> 0.
STATS = {"Physique": 14, "Reflex": 10, "Intellect": 10, "Cunning": 18, "Resolve": 8, "Influence": 8}


def test_physical_save_uses_best_of_mapped_str_con():
    # physical = best(STRENGTH<-Physique +1, CONSTITUTION<-Resolve 0) = +1
    p = MOD.save_params(stats=STATS, save="physical", level=1, label="Physical save", cfg=CFG)
    assert p.modifier == 1
    assert p.difficulty == 15  # save_base 15 - (level-1)
    assert p.sides == 20 and p.count == 1


def test_mental_save_uses_best_of_mapped_wis_cha():
    # mental = best(WISDOM<-Cunning +2, CHARISMA<-Influence 0) = +2
    p = MOD.save_params(stats=STATS, save="mental", level=1, label="Mental save", cfg=CFG)
    assert p.modifier == 2


def test_save_modifier_is_not_dead_zero():
    # Regression: pre-map, _stat("STRENGTH") fell back to 10 -> mod 0 for every save.
    p = MOD.save_params(stats=STATS, save="physical", level=1, label="x", cfg=CFG)
    assert p.modifier != 0


def test_attack_params_still_uses_flavor_stat_without_map():
    # Attack beats declare flavor stat_check; no map needed.
    class Beat:
        stat_check = "Physique"
        attack_bonus = 2
        combat_skill = 1
    class Core:
        armor_class = 13
    a = MOD.attack_params(beat=Beat(), attacker_stats=STATS, attacker_core=None, target_core=Core())
    assert a.modifier == 2 + 1 + 1  # attack_bonus + combat_skill + Physique(14)->+1
    assert a.target_number == 13


def test_stat_lookup_raises_on_absent_stat():
    with pytest.raises(KeyError):
        MOD.stat_modifier({"Physique": 12}, "Nonexistent")
