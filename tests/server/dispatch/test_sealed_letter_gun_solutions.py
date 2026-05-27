"""Tests for gun solution detection and SWN param computation in the sealed-letter resolver.

Task 9 — dogfight SWN feature. Verifies:
- When a cell view sets ``gun_solution=True`` for an actor, the resolver emits a
  populated ``GunSolution`` on ``SealedLetterOutcome.gun_solutions``.
- Geometry modifier is summed from aspect + range on the shooter's per_actor_state.
- SWN ``ship_attack_params`` is invoked correctly (modifier = attack_bonus + pilot_skill
  + best-of(DEX,INT)-mod + geometry_modifier; target_number = target_ac).
- No gun solution → empty list.
- Backward-compat: callers that pass no SWN kwargs still work and get an empty list.
- A shooter with a gun_solution but no shot_inputs entry raises ValueError (no silent skip).
"""

from __future__ import annotations

import pytest

from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.genre.models.inventory import DamageSpec
from sidequest.genre.models.rules import GeometryModifiers, InteractionCell, InteractionTable
from sidequest.server.dispatch.sealed_letter import resolve_sealed_letter_lookup


class _Cfg:
    attribute_map = {
        "STRENGTH": "Physique",
        "CONSTITUTION": "Physique",
        "DEXTERITY": "Reflex",
        "INTELLIGENCE": "Intellect",
        "WISDOM": "Resolve",
        "CHARISMA": "Cunning",
    }


def _table(blue_gun: bool = True) -> InteractionTable:
    return InteractionTable(
        version="1",
        starting_state="merge",
        maneuvers_consumed=["straight", "loop"],
        cells=[
            InteractionCell(
                pair=["straight", "loop"],
                name="blue_on_six",
                red_view={
                    "gun_solution": False,
                    "target_aspect": "head_on",
                    "target_range": "close",
                },
                blue_view={
                    "gun_solution": blue_gun,
                    "target_aspect": "tail_on",
                    "target_range": "gun",
                },
            )
        ],
    )


def _enc() -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="dogfight",
        player_metric=EncounterMetric(name="hits", current=0, threshold=3),
        opponent_metric=EncounterMetric(name="hits", current=0, threshold=3),
        actors=[
            EncounterActor(name="PC", role="red", side="player"),
            EncounterActor(name="Ace", role="blue", side="opponent"),
        ],
    )


# ---------------------------------------------------------------------------
# Happy path: blue gets the gun solution
# ---------------------------------------------------------------------------


def test_resolver_emits_one_gun_solution_for_blue() -> None:
    """Blue's cell view sets gun_solution=True → one GunSolution with correct SWN params."""
    enc = _enc()
    gm = GeometryModifiers(
        aspect={"tail_on": 2, "head_on": -2},
        range={"gun": 2, "close": 0},
    )
    # Reflex=12 → SWN mod 0 (8-13 band); Intellect=10 → SWN mod 0.
    # best_mod = 0; geometry = tail_on(+2) + gun(+2) = 4.
    # modifier = attack_bonus(1) + pilot_skill(1) + best_mod(0) + geometry(4) = 6.
    shot_inputs = {
        "blue": {
            "attacker_stats": {"Reflex": 12, "Intellect": 10},
            "pilot_skill": 1,
            "attack_bonus": 1,
            "target_ac": 16,
            "target_armor": 5,
            "weapon": DamageSpec(dice="1d4", armor_piercing=20),
            "weapon_name": "Multifocal Laser",
        },
    }
    outcome = resolve_sealed_letter_lookup(
        enc,
        {"red": "straight", "blue": "loop"},
        _table(),
        geometry_modifiers=gm,
        shot_inputs=shot_inputs,
        swn_cfg=_Cfg(),
    )
    assert len(outcome.gun_solutions) == 1
    gs = outcome.gun_solutions[0]
    assert gs.shooter_role == "blue"
    assert gs.shooter_name == "Ace"
    assert gs.target_role == "red"
    assert gs.target_name == "PC"
    # Modifier: attack_bonus 1 + pilot 1 + best_mod 0 + geometry 4 = 6
    assert gs.attack.modifier == 6
    assert gs.attack.target_number == 16
    assert gs.target_armor == 5
    assert gs.geometry_modifier == 4


# ---------------------------------------------------------------------------
# No gun solution → empty list
# ---------------------------------------------------------------------------


def test_resolver_no_gun_solution_returns_empty_list() -> None:
    """Cell gives no actor a gun_solution → outcome.gun_solutions is empty."""
    enc = _enc()
    outcome = resolve_sealed_letter_lookup(
        enc,
        {"red": "straight", "blue": "loop"},
        _table(blue_gun=False),
        geometry_modifiers=GeometryModifiers(),
        shot_inputs={},
        swn_cfg=_Cfg(),
    )
    assert outcome.gun_solutions == []


# ---------------------------------------------------------------------------
# Backward-compat: no SWN kwargs → no gun solutions
# ---------------------------------------------------------------------------


def test_resolver_without_swn_kwargs_still_works_no_solutions() -> None:
    """Existing callers that pass no SWN kwargs still get a valid outcome with no solutions."""
    enc = _enc()
    outcome = resolve_sealed_letter_lookup(
        enc,
        {"red": "straight", "blue": "loop"},
        _table(),
    )
    assert outcome.gun_solutions == []


# ---------------------------------------------------------------------------
# Error path: gun_solution but no shot_inputs entry → ValueError (no silent skip)
# ---------------------------------------------------------------------------


def test_resolver_partial_swn_kwargs_raises() -> None:
    """Passing some-but-not-all SWN kwargs is a miswire, not a silent no-op → ValueError."""
    enc = _enc()
    gm = GeometryModifiers(aspect={"tail_on": 2}, range={"gun": 2})
    # geometry_modifiers + shot_inputs provided, swn_cfg omitted (None) -> partial -> raise.
    with pytest.raises(ValueError, match="requires geometry_modifiers, shot_inputs, and"):
        resolve_sealed_letter_lookup(
            enc,
            {"red": "straight", "blue": "loop"},
            _table(blue_gun=True),
            geometry_modifiers=gm,
            shot_inputs={},  # empty dict still counts as "provided"
            swn_cfg=None,
        )


def test_resolver_raises_if_shooter_missing_from_shot_inputs() -> None:
    """A gun_solution actor with no shot_inputs entry must raise ValueError, not silently skip."""
    enc = _enc()
    gm = GeometryModifiers(aspect={"tail_on": 2}, range={"gun": 2})
    # blue has gun_solution but shot_inputs is empty
    with pytest.raises(ValueError, match="gun_solution but no shot_inputs"):
        resolve_sealed_letter_lookup(
            enc,
            {"red": "straight", "blue": "loop"},
            _table(blue_gun=True),
            geometry_modifiers=gm,
            shot_inputs={},  # missing "blue" entry
            swn_cfg=_Cfg(),
        )
