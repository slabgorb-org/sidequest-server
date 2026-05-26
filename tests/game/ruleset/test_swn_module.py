from sidequest.game.creature_core import CreatureCore, HpPool


def _core(*, name="Mara", ac=10, **kw):
    return CreatureCore(
        name=name, description="d", personality="p",
        hp=HpPool(current=8, max=8, base_max=8), armor_class=ac, **kw,
    )


def test_creature_core_has_armor_class_default_10():
    core = CreatureCore(name="x", description="d", personality="p")
    assert core.armor_class == 10


def test_creature_core_armor_class_settable():
    assert _core(ac=15).armor_class == 15


# SwnConfig tests (Task 5) — SRD-sourced constants verified from PDF pp. 46-47
from sidequest.genre.models.rules import RulesConfig, SwnConfig


def test_rules_swn_config_defaults():
    rules = RulesConfig(ruleset="swn")
    assert rules.swn is not None
    assert rules.swn.unarmored_ac == 10
    # SRD p.46: "saving throw scores start at 15, decrease by one point each time
    # you advance a level" — save_base=15 is the level-1 target before attribute mod.
    assert rules.swn.save_base == 15


def test_rules_swn_config_absent_for_native():
    assert RulesConfig().swn is None


# SwnRulesetModule tests (Task 6) — modifier curve + attack_params
import pytest
from sidequest.game.ruleset.swn import SwnRulesetModule, swn_attribute_modifier
from sidequest.genre.models.rules import BeatDef

_S = SwnRulesetModule()


@pytest.mark.parametrize(
    "score,mod",
    [(3, -2), (4, -1), (7, -1), (8, 0), (13, 0), (14, 1), (17, 1), (18, 2)],
)
def test_swn_modifier_curve(score, mod):
    assert swn_attribute_modifier(score) == mod


def test_swn_attack_params_uses_target_ac_and_attack_bonus():
    beat = BeatDef.model_validate(
        {
            "id": "shoot",
            "label": "Shoot",
            "kind": "strike",
            "base": 0,
            "stat_check": "DEXTERITY",
            "combat_skill": 1,
            "attack_bonus": 2,
        }
    )
    attacker_stats = {"DEXTERITY": 14}  # +1 SWN modifier
    target = _core(ac=13)
    params = _S.attack_params(
        beat=beat,
        attacker_stats=attacker_stats,
        attacker_core=None,
        target_core=target,
    )
    assert params.modifier == 2 + 1 + 1  # attack_bonus + combat_skill + DEX mod = 4
    assert params.target_number == 13  # target AC


# ---------------------------------------------------------------------------
# Task 7 — registry + end-to-end wiring (registering SwnRulesetModule)
# ---------------------------------------------------------------------------

from sidequest.game.ruleset import get_ruleset_module


def test_swn_registered():
    assert get_ruleset_module("swn").slug == "swn"


def test_swn_attack_resolves_vs_ac_through_dispatch():
    from unittest.mock import MagicMock, patch

    from sidequest.game.creature_core import CreatureCore, HpPool
    from sidequest.game.encounter import (
        EncounterActor,
        EncounterMetric,
        EncounterPhase,
        StructuredEncounter,
    )
    from sidequest.game.session import GameSnapshot
    from sidequest.genre.models.rules import (
        BeatDef,
        ConfrontationDef,
        MetricDef,
        RulesConfig,
        SwnConfig,
    )
    from sidequest.protocol.dice import RollOutcome
    from sidequest.server.dispatch.dice import dispatch_dice_throw

    # Reuse dispatch fixtures (CORRECT PATH — no `dispatch` subdir):
    from tests.server.test_dice_dispatch import _make_snapshot, _throw

    # Build an encounter with a player roller (Bob) AND an opponent target (Raider).
    # _opposite_side_first_actor(encounter, "player") returns the first actor
    # whose side == "opponent" — so we seat "Raider" on side="opponent".
    enc = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(
            name="momentum",
            current=0,
            starting=0,
            threshold=10,
        ),
        opponent_metric=EncounterMetric(
            name="momentum",
            current=0,
            starting=0,
            threshold=10,
        ),
        beat=0,
        structured_phase=EncounterPhase.Setup,
        secondary_stats=None,
        actors=[
            EncounterActor(name="Bob", role="combatant", side="player"),
            EncounterActor(name="Raider", role="combatant", side="opponent"),
        ],
        outcome=None,
        resolved=False,
        mood_override=None,
        narrator_hints=[],
    )

    cdef = ConfrontationDef(
        type="combat",
        label="SWN Combat",
        category="combat",
        player_metric=MetricDef(name="momentum", starting=0, threshold=10),
        opponent_metric=MetricDef(name="momentum", starting=0, threshold=10),
        beats=[
            BeatDef.model_validate(
                {
                    "id": "strike",
                    "label": "Strike",
                    "kind": "strike",
                    "base": 2,
                    "stat_check": "DEXTERITY",
                    "combat_skill": 1,
                    "attack_bonus": 2,
                }
            )
        ],
    )
    rules = MagicMock(spec=RulesConfig)
    rules.confrontations = [cdef]
    rules.ruleset = "swn"
    rules.swn = SwnConfig()
    pack = MagicMock()
    pack.rules = rules

    snap = _make_snapshot()
    target = CreatureCore(
        name="Raider",
        description="d",
        personality="p",
        hp=HpPool(current=6, max=6, base_max=6),
        armor_class=13,
    )

    def _find_creature_core(self_or_name, name=None):
        # When patching at class level, first arg is self; at instance it would be name.
        # We patch at the class level via patch so self_or_name = instance, name = the name arg.
        lookup = name if name is not None else self_or_name
        return target if lookup == "Raider" else None

    # GameSnapshot is a pydantic BaseModel; pydantic's __setattr__/__delattr__ guard
    # instance attrs — patch at the class level so teardown can restore properly.
    with patch.object(GameSnapshot, "find_creature_core", _find_creature_core):
        # face 11 + modifier (attack_bonus=2 + combat_skill=1 + DEX14 mod=1 = 4) = 15 >= AC 13
        # margin = 15 - 13 = 2 < DECISIVE_MARGIN(3) → Success (not CritSuccess)
        outcome = dispatch_dice_throw(
            payload=_throw(face=11, beat_id="strike"),
            rolling_player_id="p1",
            character_name="Bob",
            character_stats={"DEXTERITY": 14},
            encounter=enc,
            pack=pack,  # type: ignore[arg-type]
            genre_slug="test",
            session_id="s1",
            round_number=1,
            room_broadcast=None,
            snapshot=snap,
        )
    assert outcome.outcome is RollOutcome.Success
    assert outcome.result.difficulty == 13  # the target's AC was the target number
    assert outcome.result.total == 15


# ---------------------------------------------------------------------------
# Task 8 — check_params (2d6 skill check) + save_params (d20 save, best-of-two attr)
# ---------------------------------------------------------------------------


def test_swn_skill_check_params_2d6():
    from sidequest.genre.models.rules import SwnConfig
    cfg = SwnConfig()
    # 2d6 + DEX mod (+1) + skill level (2) vs "tricky"(10)
    p = _S.check_params(stats={"DEXTERITY": 14}, attribute="DEXTERITY", skill_level=2,
                        difficulty_key="tricky", label="Notice", cfg=cfg)
    assert (p.sides, p.count) == (6, 2)
    assert p.modifier == 1 + 2
    assert p.difficulty == 10
    assert p.label == "Notice"


def test_swn_save_params_d20_best_of_two_attrs():
    from sidequest.genre.models.rules import SwnConfig
    cfg = SwnConfig()
    # Mental save = better of WIS/CHA mod, added to the d20 roll.
    # WIS 14 (+1), CHA 8 (0) -> best = +1. Target = save_base(15) - (level(3)-1) = 13.
    p = _S.save_params(stats={"WISDOM": 14, "CHARISMA": 8}, save="mental", level=3,
                       label="Mental save", cfg=cfg)
    assert (p.sides, p.count) == (20, 1)
    assert p.modifier == 1          # best of WIS/CHA mods, ADDED to the roll
    assert p.difficulty == 13       # save_base(15) - (level(3) - 1) = 13  [SRD p.46: starts at 15, -1/level]
    assert p.label == "Mental save"


# ---------------------------------------------------------------------------
# Fix 3 — save_params explicit guard for unknown/None save category
# ---------------------------------------------------------------------------


def test_swn_save_params_bogus_save_raises():
    """save_params must raise ValueError (not opaque KeyError) for unknown save."""
    from sidequest.genre.models.rules import SwnConfig
    cfg = SwnConfig()
    with pytest.raises(ValueError, match="unknown save category"):
        _S.save_params(stats={"STRENGTH": 10}, save="bogus", level=1, label="bad", cfg=cfg)
