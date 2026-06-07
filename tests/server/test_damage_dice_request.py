"""Unit + integration tests for strike-beat weapon damage dice (ADR-114 / Task 7).

Step 1: failing unit tests for the pure helper ``damage_request_from_spec``.
Step 6: integration test — strike beat reduces target HP and emits state_patch.hp span.
"""

from __future__ import annotations

from sidequest.genre.models.inventory import DamageSpec  # noqa: E402
from sidequest.protocol.dice import DieSides  # noqa: E402

# ---------------------------------------------------------------------------
# Unit tests for damage_request_from_spec (Step 1 / Steps 3-4)
# ---------------------------------------------------------------------------


def test_damage_request_maps_2d6_to_two_d6_specs():
    from sidequest.server.dispatch.dice import damage_request_from_spec

    req = damage_request_from_spec(DamageSpec(dice="2d6", bonus=1), request_id="r1")
    # DiceRequestPayload.dice is list[DieSpec]; each DieSpec has .sides and .count.
    assert len(req.dice) == 2
    sides = [g.sides for g in req.dice]
    assert sides == [DieSides.D6, DieSides.D6]
    assert req.modifier == 1


def test_single_d12_weapon():
    from sidequest.server.dispatch.dice import damage_request_from_spec

    req = damage_request_from_spec(DamageSpec(dice="1d12"), request_id="r2")
    assert len(req.dice) == 1
    assert req.dice[0].sides == DieSides.D12
    assert req.modifier == 0


def test_damage_request_id_propagated():
    from sidequest.server.dispatch.dice import damage_request_from_spec

    req = damage_request_from_spec(DamageSpec(dice="3d8", bonus=2), request_id="dmg-abc")
    assert req.request_id == "dmg-abc"


def test_damage_request_3d8_bonus2():
    from sidequest.server.dispatch.dice import damage_request_from_spec

    req = damage_request_from_spec(DamageSpec(dice="3d8", bonus=2), request_id="r3")
    assert len(req.dice) == 3
    assert all(s.sides == DieSides.D8 for s in req.dice)
    assert req.modifier == 2


def test_damage_request_d4_no_bonus():
    from sidequest.server.dispatch.dice import damage_request_from_spec

    req = damage_request_from_spec(DamageSpec(dice="1d4"), request_id="r4")
    assert len(req.dice) == 1
    assert req.dice[0].sides == DieSides.D4
    assert req.modifier == 0


# ---------------------------------------------------------------------------
# Integration test: strike-beat DICE_THROW reduces target HP + emits span
# (Step 6 — behavior + OTEL, no source-text assertions)
# ---------------------------------------------------------------------------


def _make_strike_beat_with_damage_override():
    """BeatDef: kind=strike, damage_channel=strike, damage_override=1d6."""
    from sidequest.genre.models.rules import BeatDef

    return BeatDef.model_validate(
        {
            "id": "sword_strike",
            "label": "Sword Strike",
            "kind": "strike",
            "base": 2,
            "stat_check": "STRENGTH",
            "damage_channel": "strike",
            "damage_override": {"dice": "1d6", "bonus": 0},
        }
    )


def _make_encounter_with_actors(player_name: str, opponent_name: str):
    """StructuredEncounter with one player-side and one opponent actor."""
    from sidequest.game.encounter import (
        EncounterActor,
        EncounterMetric,
        EncounterPhase,
        StructuredEncounter,
    )

    return StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        beat=0,
        structured_phase=EncounterPhase.Setup,
        secondary_stats=None,
        actors=[
            EncounterActor(name=player_name, role="combatant", side="player"),
            EncounterActor(name=opponent_name, role="combatant", side="opponent"),
        ],
        outcome=None,
        resolved=False,
        mood_override=None,
        narrator_hints=[],
    )


def _make_creature_core(name: str, hp: int = 12) -> object:
    """CreatureCore with a concrete HP pool."""
    from sidequest.game.creature_core import CreatureCore, Inventory

    return CreatureCore(
        name=name,
        description=f"{name} desc",
        personality="aggressive",
        inventory=Inventory(),
        hp={"current": hp, "max": hp, "base_max": hp},
    )


def _make_pack_with_strike_beat(beat):
    """Minimal GenrePack-shaped object with one confrontation containing the beat."""
    from unittest.mock import MagicMock

    from sidequest.genre.models.rules import (
        ConfrontationDef,
        MetricDef,
        RulesConfig,
    )

    cdef = ConfrontationDef(
        type="combat",
        label="Combat",
        category="combat",
        player_metric=MetricDef(name="momentum", starting=0, threshold=10),
        opponent_metric=MetricDef(name="momentum", starting=0, threshold=10),
        beats=[beat],
    )
    rules = RulesConfig(confrontations=[cdef])
    pack = MagicMock()
    pack.rules = rules
    pack.inventory = None  # no catalog needed when damage_override is used
    return pack


def _make_snapshot_with_actors(player_name: str, opponent_name: str):
    """GameSnapshot with player + opponent creatures for edge_resolver."""
    from sidequest.game.character import Character
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager

    snap = GameSnapshot(
        genre_slug="test",
        world_slug="test",
        turn_manager=TurnManager(),
    )
    # Seat player character
    player_core = _make_creature_core(player_name, hp=10)
    player_char = Character(
        core=player_core,
        char_class="Fighter",
        race="Human",
        backstory="Veteran.",
    )
    snap.characters.append(player_char)

    # Add opponent as NPC
    from sidequest.game.session import Npc

    opponent_core = _make_creature_core(opponent_name, hp=12)
    snap.npcs.append(Npc(core=opponent_core))

    return snap


def test_strike_beat_reduces_opponent_hp(otel_capture):
    """Strike beat with damage_override rolls weapon dice and reduces target HP.

    Asserts:
    (a) opponent CreatureCore.hp.current decreased
    (b) a state_patch.hp span fired
    (c) a DICE_RESULT for the damage roll was broadcast (second pair of messages)
    """
    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.protocol.messages import DiceResultMessage
    from sidequest.server.dispatch.dice import dispatch_dice_throw

    beat = _make_strike_beat_with_damage_override()
    pack = _make_pack_with_strike_beat(beat)

    player_name = "Hero"
    opponent_name = "Bandit"

    enc = _make_encounter_with_actors(player_name, opponent_name)
    snap = _make_snapshot_with_actors(player_name, opponent_name)

    # Record opponent HP before
    opponent_core = snap.find_creature_core(opponent_name)
    assert opponent_core is not None, "fixture wiring: opponent must be resolvable"
    hp_before = opponent_core.hp.current

    broadcasts: list[object] = []

    # face=15 with STRENGTH=10 (+0 mod): DC = 10 + |2|*2 = 14; total=15 → Success
    dispatch_dice_throw(
        payload=DiceThrowPayload(
            request_id="check-req-1",
            throw_params=ThrowParams(
                velocity=(0.0, 5.0, -2.0),
                angular=(1.0, 1.0, 1.0),
                position=(0.5, 0.5),
            ),
            face=[15],
            beat_id="sword_strike",
        ),
        rolling_player_id="player-1",
        character_name=player_name,
        character_stats={"STRENGTH": 10},
        encounter=enc,
        pack=pack,
        genre_slug="test",
        session_id="session-dmg-test",
        round_number=1,
        room_broadcast=broadcasts.append,
        snapshot=snap,
    )

    # (a) Opponent HP decreased — damage_override is 1d6 so at least 1 HP off.
    hp_after = opponent_core.hp.current
    assert hp_after < hp_before, (
        f"opponent HP should have decreased after strike beat; before={hp_before} after={hp_after}"
    )

    # (b) state_patch.hp span fired
    from sidequest.telemetry.spans.state_patch import SPAN_STATE_PATCH_HP

    span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert SPAN_STATE_PATCH_HP in span_names, (
        f"state_patch.hp span must fire on strike beat; got spans: {span_names}"
    )

    # (c) A DICE_RESULT for the damage roll was broadcast.
    # The check roll produces: DICE_REQUEST, DICE_RESULT, CONFRONTATION (3 messages).
    # The damage roll produces: DICE_REQUEST, DICE_RESULT (2 more).
    # So there should be >= 2 DiceResultMessage broadcasts total.
    damage_dice_results = [m for m in broadcasts if isinstance(m, DiceResultMessage)]
    assert len(damage_dice_results) >= 2, (
        f"expected at least 2 DICE_RESULT broadcasts (check + damage); "
        f"got {len(damage_dice_results)}: {[type(m).__name__ for m in broadcasts]}"
    )
    # The damage DICE_RESULT must have a different request_id than the check.
    damage_req_ids = {
        m.payload.request_id for m in damage_dice_results if m.payload.request_id != "check-req-1"
    }
    assert damage_req_ids, (
        "damage DICE_RESULT must have a new request_id distinct from the check roll"
    )


def test_strike_beat_with_no_damage_spec_skips_damage(otel_capture):
    """Strike beat with damage_channel=strike but no damage_override and no equipped weapon.

    No HP change should occur. Dispatcher must log loudly but NOT crash and
    NOT fabricate a fake damage number.
    """
    from unittest.mock import MagicMock

    from sidequest.game.encounter import (
        EncounterActor,
        EncounterMetric,
        EncounterPhase,
        StructuredEncounter,
    )
    from sidequest.genre.models.rules import (
        BeatDef,
        ConfrontationDef,
        MetricDef,
        RulesConfig,
    )
    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.server.dispatch.dice import dispatch_dice_throw

    # Beat: damage_channel=strike but NO damage_override
    beat_no_damage = BeatDef.model_validate(
        {
            "id": "unarmed",
            "label": "Punch",
            "kind": "strike",
            "base": 1,
            "stat_check": "STRENGTH",
            "damage_channel": "strike",
            # No damage_override, no weapon in inventory
        }
    )
    cdef = ConfrontationDef(
        type="combat",
        label="Combat",
        category="combat",
        player_metric=MetricDef(name="momentum", starting=0, threshold=10),
        opponent_metric=MetricDef(name="momentum", starting=0, threshold=10),
        beats=[beat_no_damage],
    )
    rules = RulesConfig(confrontations=[cdef])
    pack = MagicMock()
    pack.rules = rules
    pack.inventory = None

    enc = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        beat=0,
        structured_phase=EncounterPhase.Setup,
        secondary_stats=None,
        actors=[
            EncounterActor(name="Puncher", role="combatant", side="player"),
            EncounterActor(name="Victim", role="combatant", side="opponent"),
        ],
        outcome=None,
        resolved=False,
        mood_override=None,
        narrator_hints=[],
    )
    snap = _make_snapshot_with_actors("Puncher", "Victim")
    opponent_core = snap.find_creature_core("Victim")
    hp_before = opponent_core.hp.current

    # face=17 → Success (DC = 10 + 1*2 = 12; 17 > 12)
    dispatch_dice_throw(
        payload=DiceThrowPayload(
            request_id="no-dmg-req",
            throw_params=ThrowParams(
                velocity=(0.0, 5.0, -2.0),
                angular=(1.0, 1.0, 1.0),
                position=(0.5, 0.5),
            ),
            face=[17],
            beat_id="unarmed",
        ),
        rolling_player_id="player-1",
        character_name="Puncher",
        character_stats={"STRENGTH": 10},
        encounter=enc,
        pack=pack,
        genre_slug="test",
        session_id="session-no-dmg",
        round_number=1,
        room_broadcast=None,
        snapshot=snap,
    )

    # HP must NOT have changed — no damage spec to roll
    hp_after = opponent_core.hp.current
    assert hp_after == hp_before, (
        f"HP must not change when no DamageSpec is resolvable; before={hp_before} after={hp_after}"
    )
