"""Regression — playtest 2026-06-10 (evropi): the persisted forensics surface
went blind to HP-channel strike damage.

A non-Warrior player's ``committed_blow`` (a ``strike`` beat) under
``win_condition="hp_depletion"`` ablated the opponent's HpPool (14→2) via the
HP channel, but the persisted ``ENCOUNTER_BEAT_APPLIED`` event recorded only the
*dial* delta (``opponent_delta=0``). A strike's dial effect is ``own_expr="b"``
(advance the actor's OWN dial), so ``deltas.opponent`` is *legitimately* 0 — the
opponent's HP loss lives entirely on the HP channel, which the dial delta cannot
see. A GM/operator doing post-hoc forensics (exactly what happened in the
playtest) reads ``opponent_delta=0`` and wrongly concludes the killing blow
"ablated nothing".

The fix threads ``apply_beat_hp_channel``'s returned HP-removed (previously
discarded) into ``ApplyResult.hp_removed`` so the dispatch layer can record the
real ablation on the persisted event, separate from the inert dial delta.

These tests pin: (1) ``hp_removed`` carries the real damage, (2) it is DISTINCT
from the suppressed dial ``deltas.opponent``, and (3) it is 0 when no HP channel
runs (so the field never fabricates damage).
"""

from __future__ import annotations

from sidequest.game.beat_kinds import apply_beat
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.turn import TurnManager
from sidequest.genre.models.rules import BeatDef
from sidequest.protocol.dice import RollOutcome

_STATS = {"STR": 12, "DEX": 10, "CON": 11, "INT": 10, "WIS": 10, "CHA": 10}


def _snapshot(*, hero_hp: int = 20, foe_hp: int = 14) -> GameSnapshot:
    snap = GameSnapshot(
        genre_slug="heavy_metal",
        world_slug="evropi",
        turn_manager=TurnManager(),
    )
    snap.characters.append(
        Character(
            core=CreatureCore(
                name="Aestrid",
                description="An elementalist.",
                personality="disciplined",
                inventory=Inventory(),
                hp={"current": hero_hp, "max": hero_hp, "base_max": hero_hp},
            ),
            char_class="Elementalist",
            race="Vaermm",
            backstory="A craft that costs the craftsman.",
            stats=dict(_STATS),
        )
    )
    snap.npcs.append(
        Npc(
            core=CreatureCore(
                name="Daggereye Knife-Captain",
                description="A foe.",
                personality="aggressive",
                inventory=Inventory(),
                hp={"current": foe_hp, "max": foe_hp, "base_max": foe_hp},
                armor_class=10,
            )
        )
    )
    return snap


def _hp_depletion_encounter() -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="combat",
        win_condition="hp_depletion",
        player_metric=EncounterMetric(name="combat", current=0, starting=0, threshold=1_000_000),
        opponent_metric=EncounterMetric(name="combat", current=0, starting=0, threshold=1_000_000),
        actors=[
            EncounterActor(name="Aestrid", role="duelist", side="player"),
            EncounterActor(name="Daggereye Knife-Captain", role="duelist", side="opponent"),
        ],
    )


def _committed_blow() -> BeatDef:
    return BeatDef.model_validate(
        {
            "id": "committed_blow",
            "label": "Committed Blow",
            "kind": "strike",
            "base": 4,
            "stat_check": "STR",
            "damage_channel": "strike",
            "damage_override": {"dice": "2d6", "bonus": 0},
        }
    )


def _player_actor(enc: StructuredEncounter) -> EncounterActor:
    return next(a for a in enc.actors if a.side == "player")


def test_hp_removed_carries_real_strike_damage():
    """A CritSuccess strike that removes 12 HP reports hp_removed=12."""
    snap = _snapshot(foe_hp=14)
    enc = _hp_depletion_encounter()

    result = apply_beat(
        enc,
        _player_actor(enc),
        _committed_blow(),
        RollOutcome.CritSuccess,
        edge_resolver=snap.find_creature_core,
        damage_resolver=lambda: 12,
    )

    assert result.hp_removed == 12
    # ...and the opponent's HpPool actually dropped 14 → 2.
    foe = snap.find_creature_core("Daggereye Knife-Captain")
    assert foe is not None
    assert foe.hp.current == 2


def test_hp_removed_is_distinct_from_suppressed_dial_delta():
    """The whole bug: opponent_delta (dial) is 0 while hp_removed is non-zero.

    A strike's dial rule is own_expr='b' (advance OWN dial), so deltas.opponent
    is legitimately 0 — and under hp_depletion the dial mutation is suppressed
    besides. The persisted event must NOT conflate the two: hp_removed is the
    surface that proves the strike landed.
    """
    snap = _snapshot(foe_hp=14)
    enc = _hp_depletion_encounter()

    result = apply_beat(
        enc,
        _player_actor(enc),
        _committed_blow(),
        RollOutcome.CritSuccess,
        edge_resolver=snap.find_creature_core,
        damage_resolver=lambda: 7,
    )

    assert result.deltas is not None
    assert result.deltas.opponent == 0  # inert dial — the red herring
    assert result.hp_removed == 7  # the real ablation, on its own surface


def test_hp_removed_is_zero_without_a_damage_channel():
    """No strike damage channel → no fabricated HP delta."""
    snap = _snapshot()
    enc = _hp_depletion_encounter()
    non_strike = BeatDef.model_validate(
        {"id": "argue", "label": "Argue", "kind": "push", "base": 1, "stat_check": "INT"}
    )

    result = apply_beat(
        enc,
        _player_actor(enc),
        non_strike,
        RollOutcome.Success,
        edge_resolver=snap.find_creature_core,
        damage_resolver=lambda: 99,  # present but channel != strike → ignored
    )

    assert result.hp_removed == 0
