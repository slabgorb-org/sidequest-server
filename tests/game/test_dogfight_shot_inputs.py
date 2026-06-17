"""Task 11 — build_dogfight_shot_inputs: assemble per-role SWN shot inputs.

Tests cover:
- fails loud when pack isn't SWN-bound (ValueError, match="SWN")
- happy path: returns (shot_inputs, geometry_modifiers) with correct per-role dicts
- opponent entry uses cdef stats; player entry uses pc_* args (sheet wins over frame)
- fail loud when geometry_modifiers is None on the cdef
- fail loud when opponent_weapon id can't resolve (weapon_lookup returns None)
- fail loud when encounter missing an opponent or player actor
"""

from __future__ import annotations

import pytest

from sidequest.game.dogfight_shot import build_dogfight_shot_inputs
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.genre.models.inventory import DamageSpec
from sidequest.genre.models.rules import ConfrontationDef, GeometryModifiers

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _minimal_confrontation(**overrides) -> ConfrontationDef:
    """Build a minimal valid ConfrontationDef (social/dial defaults).

    Replicates the helper from tests/genre/test_geometry_modifiers.py.
    category=social keeps it off the hp_depletion reserved-key validator.
    """
    kwargs = {
        "type": "test_clash",
        "label": "Test Clash",
        "category": "social",
        "player_metric": {"name": "advantage", "threshold": 3},
        "opponent_metric": {"name": "pressure", "threshold": 3},
        "beats": [
            {
                "id": "jab",
                "label": "Jab",
                "kind": "push",
                "stat_check": "wit",
            }
        ],
    }
    kwargs.update(overrides)
    return ConfrontationDef(**kwargs)


def _dogfight_cdef(**overrides) -> ConfrontationDef:
    """A ConfrontationDef wired for dogfight: both weapon ids, full frame stats,
    and a non-None geometry_modifiers block."""
    kwargs = {
        "opponent_default_stats": {
            "Reflex": 12,
            "Intellect": 10,
            "hp": 8,
            "armor_class": 16,
            "armor": 5,
            "dexterity": 12,
            "pilot_skill": 1,
            "attack_bonus": 1,
        },
        "player_default_stats": {
            "hp": 8,
            "armor_class": 16,
            "armor": 5,
            "pilot_skill": 0,
            "attack_bonus": 0,
        },
        "opponent_weapon": "opp_laser",
        "player_weapon": "pc_laser",
        "geometry_modifiers": GeometryModifiers(
            aspect={"tail_on": 2, "head_on": -2},
            range={"gun": 2, "far": -4},
        ),
    }
    kwargs.update(overrides)
    return _minimal_confrontation(**kwargs)


def _enc() -> StructuredEncounter:
    """Minimal 2-actor encounter: player 'red', opponent 'blue'."""
    return StructuredEncounter(
        encounter_type="dogfight",
        win_condition="hp_depletion",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=1_000_000),
        opponent_metric=EncounterMetric(
            name="momentum", current=0, starting=0, threshold=1_000_000
        ),
        actors=[
            EncounterActor(name="PC", role="red", side="player"),
            EncounterActor(name="Ace", role="blue", side="opponent"),
        ],
    )


class _FakeWeapon:
    """Minimal stand-in for a weapon catalog item."""

    def __init__(self, weapon_id: str, dice: str = "1d4", ap: int = 20) -> None:
        self.name = weapon_id
        self.damage = DamageSpec(dice=dice, armor_piercing=ap)


def _weapon_lookup(weapon_id: str) -> _FakeWeapon | None:
    weapons = {
        "opp_laser": _FakeWeapon("opp_laser", "1d4", 20),
        "pc_laser": _FakeWeapon("pc_laser", "1d6", 10),
    }
    return weapons.get(weapon_id)


# ---------------------------------------------------------------------------
# Fail-loud: non-SWN ruleset
# ---------------------------------------------------------------------------


def test_build_inputs_fails_loud_when_not_swn():
    with pytest.raises(ValueError, match="SWN"):
        build_dogfight_shot_inputs(
            ruleset_slug="dial",
            cdef=None,
            encounter=None,
            pc_stats={},
            pc_pilot_skill=0,
            pc_attack_bonus=0,
            weapon_lookup=lambda wid: None,
        )


# ---------------------------------------------------------------------------
# Fail-loud: missing geometry_modifiers
# ---------------------------------------------------------------------------


def test_build_inputs_fails_loud_when_no_geometry_modifiers():
    cdef = _dogfight_cdef(geometry_modifiers=None)
    with pytest.raises(ValueError, match="geometry_modifiers"):
        build_dogfight_shot_inputs(
            ruleset_slug="swn",
            cdef=cdef,
            encounter=_enc(),
            pc_stats={},
            pc_pilot_skill=0,
            pc_attack_bonus=0,
            weapon_lookup=_weapon_lookup,
        )


# ---------------------------------------------------------------------------
# Fail-loud: weapon id can't resolve
# ---------------------------------------------------------------------------


def test_build_inputs_fails_loud_when_opponent_weapon_unresolvable():
    cdef = _dogfight_cdef(opponent_weapon="unknown_cannon")
    with pytest.raises(ValueError, match="unknown_cannon"):
        build_dogfight_shot_inputs(
            ruleset_slug="swn",
            cdef=cdef,
            encounter=_enc(),
            pc_stats={"Reflex": 14},
            pc_pilot_skill=2,
            pc_attack_bonus=1,
            weapon_lookup=_weapon_lookup,
        )


def test_build_inputs_fails_loud_when_player_weapon_unresolvable():
    cdef = _dogfight_cdef(player_weapon="mystery_gun")
    with pytest.raises(ValueError, match="mystery_gun"):
        build_dogfight_shot_inputs(
            ruleset_slug="swn",
            cdef=cdef,
            encounter=_enc(),
            pc_stats={"Reflex": 14},
            pc_pilot_skill=2,
            pc_attack_bonus=1,
            weapon_lookup=_weapon_lookup,
        )


# ---------------------------------------------------------------------------
# Fail-loud: missing required frame stat (_require_stat)
# ---------------------------------------------------------------------------


def test_build_inputs_fails_loud_when_player_frame_missing_armor_class():
    cdef = _dogfight_cdef(
        player_default_stats={
            # armor_class omitted
            "hp": 8,
            "armor": 5,
            "pilot_skill": 0,
            "attack_bonus": 0,
        }
    )
    with pytest.raises(ValueError, match="armor_class"):
        build_dogfight_shot_inputs(
            ruleset_slug="swn",
            cdef=cdef,
            encounter=_enc(),
            pc_stats={"Reflex": 14},
            pc_pilot_skill=2,
            pc_attack_bonus=1,
            weapon_lookup=_weapon_lookup,
        )


def test_build_inputs_fails_loud_when_opponent_frame_missing_armor():
    cdef = _dogfight_cdef(
        opponent_default_stats={
            "Reflex": 12,
            "Intellect": 10,
            "hp": 8,
            "armor_class": 16,
            # armor omitted
            "dexterity": 12,
            "pilot_skill": 1,
            "attack_bonus": 1,
        }
    )
    with pytest.raises(ValueError, match="armor"):
        build_dogfight_shot_inputs(
            ruleset_slug="swn",
            cdef=cdef,
            encounter=_enc(),
            pc_stats={"Reflex": 14},
            pc_pilot_skill=2,
            pc_attack_bonus=1,
            weapon_lookup=_weapon_lookup,
        )


# ---------------------------------------------------------------------------
# Fail-loud: encounter missing an actor side
# ---------------------------------------------------------------------------


def test_build_inputs_fails_loud_when_encounter_missing_opponent_actor():
    enc = StructuredEncounter(
        encounter_type="dogfight",
        win_condition="hp_depletion",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=1_000_000),
        opponent_metric=EncounterMetric(
            name="momentum", current=0, starting=0, threshold=1_000_000
        ),
        actors=[
            EncounterActor(name="PC", role="red", side="player"),
            # no opponent actor
        ],
    )
    with pytest.raises(ValueError, match="opponent"):
        build_dogfight_shot_inputs(
            ruleset_slug="swn",
            cdef=_dogfight_cdef(),
            encounter=enc,
            pc_stats={},
            pc_pilot_skill=0,
            pc_attack_bonus=0,
            weapon_lookup=_weapon_lookup,
        )


def test_build_inputs_fails_loud_when_encounter_missing_player_actor():
    enc = StructuredEncounter(
        encounter_type="dogfight",
        win_condition="hp_depletion",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=1_000_000),
        opponent_metric=EncounterMetric(
            name="momentum", current=0, starting=0, threshold=1_000_000
        ),
        actors=[
            EncounterActor(name="Ace", role="blue", side="opponent"),
            # no player actor
        ],
    )
    with pytest.raises(ValueError, match="player"):
        build_dogfight_shot_inputs(
            ruleset_slug="swn",
            cdef=_dogfight_cdef(),
            encounter=enc,
            pc_stats={},
            pc_pilot_skill=0,
            pc_attack_bonus=0,
            weapon_lookup=_weapon_lookup,
        )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_build_inputs_happy_path_returns_shot_inputs_and_geometry_modifiers():
    cdef = _dogfight_cdef()
    enc = _enc()
    pc_stats = {"Reflex": 14, "Intellect": 11}

    result = build_dogfight_shot_inputs(
        ruleset_slug="swn",
        cdef=cdef,
        encounter=enc,
        pc_stats=pc_stats,
        pc_pilot_skill=2,
        pc_attack_bonus=1,
        weapon_lookup=_weapon_lookup,
    )

    shot_inputs, geometry_modifiers = result

    # geometry_modifiers is the cdef's
    assert geometry_modifiers is cdef.geometry_modifiers

    # Both roles present (opponent role = "blue", player role = "red")
    assert "blue" in shot_inputs, f"expected 'blue' in shot_inputs, got {list(shot_inputs)}"
    assert "red" in shot_inputs, f"expected 'red' in shot_inputs, got {list(shot_inputs)}"


def test_build_inputs_opponent_entry_correct():
    """Opponent slot reads from cdef.opponent_default_stats and targets the PC frame."""
    cdef = _dogfight_cdef()
    enc = _enc()

    shot_inputs, _ = build_dogfight_shot_inputs(
        ruleset_slug="swn",
        cdef=cdef,
        encounter=enc,
        pc_stats={"Reflex": 14},
        pc_pilot_skill=2,
        pc_attack_bonus=1,
        weapon_lookup=_weapon_lookup,
    )

    opp = shot_inputs["blue"]  # opponent role
    # Targets the PC frame — uses player_default_stats
    assert opp["target_ac"] == 16, f"expected target_ac=16 (PC frame), got {opp['target_ac']}"
    assert opp["target_armor"] == 5, f"expected target_armor=5, got {opp['target_armor']}"
    # Gunnery terms from cdef.opponent_default_stats
    assert opp["pilot_skill"] == 1, f"expected pilot_skill=1 from cdef, got {opp['pilot_skill']}"
    assert opp["attack_bonus"] == 1, f"expected attack_bonus=1, got {opp['attack_bonus']}"
    # Weapon is the DamageSpec from opp_laser
    assert isinstance(opp["weapon"], DamageSpec)
    assert opp["weapon"].dice == "1d4"
    assert opp["weapon_name"] == "opp_laser"
    # attacker_stats is opponent_ability_scores() — reserved combat keys stripped,
    # leaving only ability scores so the to-hit lookup never sees the reserved keys.
    assert "armor_class" not in opp["attacker_stats"]
    assert "armor" not in opp["attacker_stats"]
    assert "pilot_skill" not in opp["attacker_stats"]
    assert "attack_bonus" not in opp["attacker_stats"]
    assert "hp" not in opp["attacker_stats"]
    assert "dexterity" not in opp["attacker_stats"]
    assert opp["attacker_stats"]["Reflex"] == 12
    assert opp["attacker_stats"]["Intellect"] == 10


def test_build_inputs_player_entry_correct():
    """Player slot: pc_pilot_skill/pc_attack_bonus win over frame defaults; targets opponent AC."""
    cdef = _dogfight_cdef()
    enc = _enc()
    pc_stats = {"Reflex": 14, "Intellect": 11}

    shot_inputs, _ = build_dogfight_shot_inputs(
        ruleset_slug="swn",
        cdef=cdef,
        encounter=enc,
        pc_stats=pc_stats,
        pc_pilot_skill=2,
        pc_attack_bonus=1,
        weapon_lookup=_weapon_lookup,
    )

    pc = shot_inputs["red"]  # player role
    # Targets the opponent frame
    assert pc["target_ac"] == 16, f"expected target_ac=16 (opp frame), got {pc['target_ac']}"
    assert pc["target_armor"] == 5, f"expected target_armor=5, got {pc['target_armor']}"
    # pc_pilot_skill arg wins over player_default_stats.pilot_skill (0)
    assert pc["pilot_skill"] == 2, (
        f"expected pilot_skill=2 from pc_pilot_skill arg, got {pc['pilot_skill']}"
    )
    assert pc["attack_bonus"] == 1, (
        f"expected attack_bonus=1 from pc_attack_bonus arg, got {pc['attack_bonus']}"
    )
    # attacker_stats is pc_stats dict
    assert pc["attacker_stats"] is pc_stats
    # Weapon is the DamageSpec from pc_laser
    assert isinstance(pc["weapon"], DamageSpec)
    assert pc["weapon"].dice == "1d6"
    assert pc["weapon_name"] == "pc_laser"


def test_build_inputs_sheet_pilot_skill_wins_over_frame_default():
    """Passing pc_pilot_skill=3 must produce player entry pilot_skill=3, not
    the frame default of 0 in player_default_stats."""
    cdef = _dogfight_cdef()
    enc = _enc()

    shot_inputs, _ = build_dogfight_shot_inputs(
        ruleset_slug="swn",
        cdef=cdef,
        encounter=enc,
        pc_stats={},
        pc_pilot_skill=3,
        pc_attack_bonus=0,
        weapon_lookup=_weapon_lookup,
    )
    assert shot_inputs["red"]["pilot_skill"] == 3
