"""road_warrior CWN combat — production-path wiring proof (Story 86-1, spec §6.4).

The GM panel is the lie detector (CLAUDE.md OTEL Observability Principle): a bound
ruleset is only real if its spans fire on a production turn. This drives a REAL
strike through the production ``dispatch_dice_throw`` against the ACTUAL loaded
``road_warrior`` pack — not a synthetic MagicMock — and asserts on OTEL spans +
ablative HP state, never on source text (CLAUDE.md forbids source-grep wiring
tests).

RED until Plan 1 lands: today road_warrior runs the native dial engine and its
combat confrontation is ``resolution_mode: opposed_check`` (dial metrics, no
ablative pool), so no ``state_patch.hp`` fires and the opponent's HP never
depletes. After binding to ``ruleset: cwn`` with a ``win_condition: hp_depletion``
combat confrontation whose strike beats carry damage, a real strike depletes the
opponent's ablative HP and the ADR-114 ``state_patch.hp`` span fires — proving the
CWN module engaged from a production code path, not improvised narration.

Behavioral, not implementation-coupled: the test reads the combat confrontation's
first strike beat (id + stat_check) from the LOADED content, so it survives beat
renames; it asserts HP depletion + the span, not HOW Dev wires the damage.

The attacker is armed with the real ``sawed_off_shotgun`` from the pack's item
catalog (damage 3d4) — CWN strike damage flows from the equipped weapon (Priority
2/3 of the damage resolver), exactly as in real play, mirroring how
``tests/integration/test_space_opera_hp_e2e.py`` arms with ``blaster_sidearm``.

Pattern precedent: tests/integration/test_space_opera_hp_e2e.py (real pack + HP
depletion), tests/server/test_awn_combat_dispatch.py (AWN production dispatch).
``otel_capture`` is provided by tests/server/conftest.py.
"""

from __future__ import annotations

import pytest

from tests._helpers.genre_paths import GENRE_PACKS_DIR, find_pack_path

# All six standard-CWN abilities high enough that a forced face=20 to-hit connects
# regardless of which one the strike beat keys on.
_STATS = {name: 14 for name in ("STR", "DEX", "CON", "INT", "WIS", "CHA")}


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


def _load_road_warrior():
    from sidequest.genre.loader import load_genre_pack

    if not _has_real_content():
        pytest.skip("sidequest-content not on disk")
    return load_genre_pack(find_pack_path("road_warrior"))


def _combat_confrontation(pack):
    combats = [c for c in pack.rules.confrontations if c.category == "combat"]
    assert len(combats) == 1, (
        f"road_warrior must ship exactly one combat confrontation; found {len(combats)}"
    )
    return combats[0]


def _first_strike_beat(cdef):
    for beat in cdef.beats:
        kind = beat.kind.value if hasattr(beat.kind, "value") else beat.kind
        if kind == "strike":
            return beat
    raise AssertionError(
        f"road_warrior combat confrontation must expose at least one strike beat; "
        f"beats={[b.id for b in cdef.beats]}"
    )


def _make_snapshot_and_encounter(*, attacker: str, opponent: str, opponent_hp: int, opp_ac: int):
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory
    from sidequest.game.encounter import (
        EncounterActor,
        EncounterMetric,
        EncounterPhase,
        StructuredEncounter,
    )
    from sidequest.game.session import GameSnapshot, Npc
    from sidequest.game.turn import TurnManager

    # Armed with the real catalog weapon so the strike resolves weapon damage
    # (Priority 2/3 of the damage resolver) — faithful to real play, mirrors the
    # space_opera hp e2e's blaster_sidearm.
    atk_core = CreatureCore(
        name=attacker,
        description="Road runner, jacket full of road dust.",
        personality="reckless",
        inventory=Inventory(items=[{"id": "sawed_off_shotgun", "name": "Sawed-off Shotgun"}]),
        hp={"current": 12, "max": 12, "base_max": 12},
        armor_class=13,
    )
    attacker_char = Character(
        core=atk_core,
        char_class="Wheelman",
        race="Human",
        backstory="Born to the wheel.",
        stats=dict(_STATS),
    )
    opp_core = CreatureCore(
        name=opponent,
        description="Raider on a scrap-bike.",
        personality="cruel",
        inventory=Inventory(),
        hp={"current": opponent_hp, "max": opponent_hp, "base_max": opponent_hp},
        armor_class=opp_ac,
    )

    snap = GameSnapshot(
        genre_slug="road_warrior",
        world_slug="the_circuit",
        turn_manager=TurnManager(),
    )
    snap.characters.append(attacker_char)
    snap.npcs.append(Npc(core=opp_core))

    enc = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=7),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=7),
        beat=0,
        structured_phase=EncounterPhase.Setup,
        secondary_stats=None,
        actors=[
            EncounterActor(name=attacker, role="combatant", side="player"),
            EncounterActor(name=opponent, role="combatant", side="opponent"),
        ],
        outcome=None,
        resolved=False,
        mood_override=None,
        narrator_hints=[],
    )
    return snap, enc


def test_road_warrior_strike_depletes_ablative_hp_and_fires_span(otel_capture, monkeypatch):
    """A real strike against the loaded road_warrior pack resolves through the
    production dispatcher under the CWN ruleset: the opponent's ablative HP
    depletes and the ADR-114 ``state_patch.hp`` span fires.

    Forces the trauma/save dice LOW (the cwn path rolls them via the ``random``
    module imported into ``sidequest.server.dispatch.dice``); damage faces roll
    via a different, untouched module. face=[20] guarantees the to-hit connects.
    """
    monkeypatch.setattr("sidequest.server.dispatch.dice.random.randint", lambda a, b: a)

    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.server.dispatch.dice import dispatch_dice_throw

    pack = _load_road_warrior()
    cdef = _combat_confrontation(pack)
    strike = _first_strike_beat(cdef)
    ods = cdef.opponent_default_stats or {}
    opp_hp = ods.get("hp", 20)
    opp_ac = ods.get("armor_class", 12)

    snap, enc = _make_snapshot_and_encounter(
        attacker="Vance", opponent="Scrag", opponent_hp=opp_hp, opp_ac=opp_ac
    )
    target = snap.find_creature_core("Scrag")
    assert target is not None
    assert target.hp.current == target.hp.max  # precondition: full HP

    dispatch_dice_throw(
        payload=DiceThrowPayload(
            request_id="rw-cwn-strike",
            throw_params=ThrowParams(
                velocity=(0.0, 5.0, -2.0),
                angular=(1.0, 1.0, 1.0),
                position=(0.5, 0.5),
            ),
            face=[20],
            beat_id=strike.id,
        ),
        rolling_player_id="player-vance",
        character_name="Vance",
        character_stats=dict(_STATS),
        encounter=enc,
        pack=pack,
        genre_slug="road_warrior",
        session_id="rw-cwn-strike",
        round_number=1,
        room_broadcast=[].append,
        snapshot=snap,
    )

    span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert "state_patch.hp" in span_names, (
        f"ADR-114 ablative HP: a state_patch.hp span must fire when a road_warrior "
        f"strike connects under the bound CWN ruleset — proving the engine resolved "
        f"the hit, not the narrator improvising; got spans: {span_names}"
    )
    assert target.hp.current < target.hp.max, (
        f"the strike must deplete the opponent's ablative HP pool (CWN hp_depletion "
        f"combat), not move a native dial metric; hp={target.hp.current}/"
        f"{target.hp.max}"
    )
