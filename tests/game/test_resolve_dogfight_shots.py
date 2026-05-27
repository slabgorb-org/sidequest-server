"""Task 10 — resolve_dogfight_shots: SWN two-pass shot resolution.

Tests cover:
- hit ablates target HP by (raw_damage - effective_armor)
- a graze (hit but fully soaked) leaves HP unchanged with hit=True, applied=0
- miss does no damage
- mutual kill resolves mutual_destruction (two-pass: both shots compute vs pre-shot HP)
- source is DERIVED from the shooter's encounter-actor side
- missing d20 for a shooter raises ValueError (no silent skip)
- missing target core raises ValueError (no silent no-op)
"""

from __future__ import annotations

import pytest

import sidequest.game.dogfight_shot as ds
from sidequest.game.creature_core import CreatureCore, Inventory, hp_pool_from_hp
from sidequest.game.dogfight_shot import GunSolution, resolve_dogfight_shots
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.ruleset.resolution import AttackRollParams
from sidequest.genre.models.inventory import DamageSpec


def _core(name: str, hp: int) -> CreatureCore:
    return CreatureCore(
        name=name,
        description="a pilot",
        personality="bold",
        inventory=Inventory(),
        hp=hp_pool_from_hp(hp),
        armor_class=16,
    )


def _enc() -> StructuredEncounter:
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


def _gs(
    shooter_role: str,
    shooter: str,
    target_role: str,
    target: str,
    modifier: int,
    *,
    armor_piercing: int = 20,
    target_armor: int = 5,
) -> GunSolution:
    return GunSolution(
        shooter_role=shooter_role,
        shooter_name=shooter,
        target_role=target_role,
        target_name=target,
        attack=AttackRollParams(modifier=modifier, target_number=16),
        weapon=DamageSpec(dice="1d4", armor_piercing=armor_piercing),
        weapon_name="Multifocal Laser",
        target_armor=target_armor,
        geometry_modifier=4,
    )


def test_npc_hit_ablates_player_hp(monkeypatch):
    monkeypatch.setattr(ds, "_roll_damage_dice", lambda spec: 3)  # deterministic 1d4 -> 3
    cores = {"PC": _core("PC", 8), "Ace": _core("Ace", 8)}
    enc = _enc()
    gs = _gs("blue", "Ace", "red", "PC", modifier=13)  # 13 + d20=10 = 23 >= 16 hit
    res = resolve_dogfight_shots(
        encounter=enc,
        gun_solutions=[gs],
        d20_by_shooter={"blue": 10},  # 23 >= 16 → hit
        edge_resolver=lambda n: cores.get(n),
    )
    # AP 20 vs armor 5 -> effective_armor=0; applied = 3 - 0 = 3
    assert cores["PC"].hp.current == 5
    blue_shot = next(s for s in res.shots if s.shooter_role == "blue")
    assert blue_shot.hit
    # "blue" actor is side="opponent" → source derives to "npc".
    assert blue_shot.source == "npc"


def test_graze_hit_fully_soaked_leaves_hp_unchanged(monkeypatch):
    """A hit whose raw damage is fully absorbed by armor (raw < eff_armor)
    leaves HP unchanged but is still recorded as a hit with applied=0."""
    monkeypatch.setattr(ds, "_roll_damage_dice", lambda spec: 3)  # 1d4 -> 3
    cores = {"PC": _core("PC", 8), "Ace": _core("Ace", 8)}
    enc = _enc()
    # armor 10, no AP → eff_armor=10; applied = max(0, 3 - 10) = 0
    gs = _gs("blue", "Ace", "red", "PC", modifier=13, armor_piercing=0, target_armor=10)
    res = resolve_dogfight_shots(
        encounter=enc,
        gun_solutions=[gs],
        d20_by_shooter={"blue": 10},  # 23 >= 16 → hit
        edge_resolver=lambda n: cores.get(n),
    )
    assert cores["PC"].hp.current == 8  # unchanged — fully soaked
    shot = next(s for s in res.shots if s.shooter_role == "blue")
    assert shot.hit is True
    assert shot.applied == 0


def test_player_shooter_source_is_player(monkeypatch):
    """A player-side shooter's ShotResult.source derives to 'player'."""
    monkeypatch.setattr(ds, "_roll_damage_dice", lambda spec: 3)
    cores = {"PC": _core("PC", 8), "Ace": _core("Ace", 8)}
    enc = _enc()
    # "red" actor is side="player".
    gs = _gs("red", "PC", "blue", "Ace", modifier=13)
    res = resolve_dogfight_shots(
        encounter=enc,
        gun_solutions=[gs],
        d20_by_shooter={"red": 10},
        edge_resolver=lambda n: cores.get(n),
    )
    shot = next(s for s in res.shots if s.shooter_role == "red")
    assert shot.source == "player"


def test_miss_does_no_damage():
    cores = {"PC": _core("PC", 8), "Ace": _core("Ace", 8)}
    enc = _enc()
    gs = _gs("blue", "Ace", "red", "PC", modifier=0)
    res = resolve_dogfight_shots(
        encounter=enc,
        gun_solutions=[gs],
        d20_by_shooter={"blue": 5},  # 5 + 0 = 5 < 16 → miss
        edge_resolver=lambda n: cores.get(n),
    )
    assert cores["PC"].hp.current == 8
    assert all(not s.hit for s in res.shots)


def test_mutual_kill_resolves_mutual_destruction(monkeypatch):
    monkeypatch.setattr(ds, "_roll_damage_dice", lambda spec: 4)
    cores = {"PC": _core("PC", 3), "Ace": _core("Ace", 3)}
    enc = _enc()
    shots = [
        _gs("blue", "Ace", "red", "PC", modifier=20),
        _gs("red", "PC", "blue", "Ace", modifier=20),
    ]
    res = resolve_dogfight_shots(
        encounter=enc,
        gun_solutions=shots,
        d20_by_shooter={"blue": 10, "red": 10},  # 30 >= 16 → both hit
        edge_resolver=lambda n: cores.get(n),
    )
    # AP 20 negates armor 5 → effective_armor=0; both take 4 damage
    # Pre-shot HP was 3 for both → both go to 0
    assert cores["PC"].hp.current == 0
    assert cores["Ace"].hp.current == 0
    assert enc.outcome == "mutual_destruction"
    assert res.depletion is not None


def test_missing_d20_for_shooter_raises():
    cores = {"PC": _core("PC", 8), "Ace": _core("Ace", 8)}
    enc = _enc()
    gs = _gs("blue", "Ace", "red", "PC", modifier=13)
    with pytest.raises(ValueError, match="d20_by_shooter"):
        resolve_dogfight_shots(
            encounter=enc,
            gun_solutions=[gs],
            d20_by_shooter={},  # missing blue
            edge_resolver=lambda n: cores.get(n),
        )


def test_missing_target_core_raises():
    enc = _enc()
    gs = _gs("blue", "Ace", "red", "PC", modifier=13)
    with pytest.raises(ValueError, match="target"):
        resolve_dogfight_shots(
            encounter=enc,
            gun_solutions=[gs],
            d20_by_shooter={"blue": 10},
            edge_resolver=lambda n: None,  # no cores at all
        )
