from __future__ import annotations

import dataclasses

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.fate_opponent import OpponentDecision, decide_opponent_action
from sidequest.game.fate_sheet import Aspect, FateSheet
from sidequest.game.session import GameSnapshot, Npc


def _pc(name: str, skills: dict[str, int], sheet: FateSheet | None = None) -> Character:
    fs = sheet if sheet is not None else FateSheet(skills=skills)
    core = CreatureCore(name=name, description="d", personality="p", fate_sheet=fs)
    return Character(core=core, char_class="Agent", race="Human", backstory="b")


def _npc(name: str, skills: dict[str, int], sheet: FateSheet | None = None) -> Npc:
    fs = sheet if sheet is not None else FateSheet(skills=skills)
    return Npc(core=CreatureCore(name=name, description="d", personality="p", fate_sheet=fs))


def _enc(actors: list[EncounterActor], *, category: str = "combat") -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="duel",
        category=category,
        player_metric=EncounterMetric(name="p", threshold=10),
        opponent_metric=EncounterMetric(name="o", threshold=10),
        actors=actors,
    )


def _wounded_consequence_sheet(skills: dict[str, int]) -> FateSheet:
    sheet = FateSheet(skills=skills)
    sheet.consequences[0].aspect = Aspect(text="Bruised Ribs", kind="consequence")
    return sheet


def _wounded_stress_sheet(skills: dict[str, int], track: str) -> FateSheet:
    sheet = FateSheet(skills=skills)
    sheet.stress[track].boxes[0].checked = True
    return sheet


# (a) targets the wounded PC (filled consequence) over an unwounded PC.
def test_targets_wounded_pc_over_unwounded():
    enc = _enc(
        [
            EncounterActor(name="Healthy", role="lead", side="player"),
            EncounterActor(name="Wounded", role="muscle", side="player"),
            EncounterActor(name="Thug", role="foe", side="opponent"),
        ]
    )
    snap = GameSnapshot(
        genre_slug="fate_test",
        characters=[
            _pc("Healthy", {"Fight": 3}),
            _pc("Wounded", {"Fight": 1}, sheet=_wounded_consequence_sheet({"Fight": 1})),
        ],
        npcs=[_npc("Thug", {"Fight": 2})],
        encounter=enc,
    )
    decision = decide_opponent_action(
        encounter=enc, snapshot=snap, opponent=enc.find_actor("Thug"), mental=False
    )
    assert decision is not None
    assert decision.target == "Wounded"


def test_targets_wounded_pc_via_checked_stress_box():
    enc = _enc(
        [
            EncounterActor(name="Healthy", role="lead", side="player"),
            EncounterActor(name="Stressed", role="muscle", side="player"),
            EncounterActor(name="Thug", role="foe", side="opponent"),
        ]
    )
    snap = GameSnapshot(
        genre_slug="fate_test",
        characters=[
            _pc("Healthy", {"Fight": 3}),
            _pc("Stressed", {"Fight": 1}, sheet=_wounded_stress_sheet({"Fight": 1}, "physical")),
        ],
        npcs=[_npc("Thug", {"Fight": 2})],
        encounter=enc,
    )
    decision = decide_opponent_action(
        encounter=enc, snapshot=snap, opponent=enc.find_actor("Thug"), mental=False
    )
    assert decision is not None
    assert decision.target == "Stressed"


def test_mental_track_ignores_physical_stress_for_wound_check():
    # A PC with a checked PHYSICAL box is NOT wounded for a MENTAL conflict.
    enc = _enc(
        [
            EncounterActor(name="PhysHurt", role="lead", side="player"),
            EncounterActor(name="Fresh", role="muscle", side="player"),
            EncounterActor(name="Manipulator", role="foe", side="opponent"),
        ]
    )
    snap = GameSnapshot(
        genre_slug="fate_test",
        characters=[
            _pc(
                "PhysHurt", {"Provoke": 1}, sheet=_wounded_stress_sheet({"Provoke": 1}, "physical")
            ),
            _pc("Fresh", {"Provoke": 1}),
        ],
        npcs=[_npc("Manipulator", {"Provoke": 3})],
        encounter=enc,
    )
    decision = decide_opponent_action(
        encounter=enc, snapshot=snap, opponent=enc.find_actor("Manipulator"), mental=True
    )
    assert decision is not None
    # No mental wound on anyone → falls through to highest-Provoke; both equal → seating order.
    assert decision.target == "PhysHurt"


def test_filled_consequence_counts_as_wounded_on_the_mental_track_too():
    # Consequences are CROSS-TRACK in Fate (a filled consequence is an aspect
    # invokable in any conflict). A PC with a filled consequence is "wounded" for a
    # MENTAL conflict even though consequences aren't tied to a stress track. Locks
    # the cross-track semantic of _is_wounded against a future per-track refactor.
    enc = _enc(
        [
            EncounterActor(name="Fresh", role="lead", side="player"),
            EncounterActor(name="Scarred", role="muscle", side="player"),
            EncounterActor(name="Manipulator", role="foe", side="opponent"),
        ]
    )
    snap = GameSnapshot(
        genre_slug="fate_test",
        characters=[
            _pc("Fresh", {"Provoke": 5}),  # higher Provoke — would win threat ranking
            _pc("Scarred", {"Provoke": 1}, sheet=_wounded_consequence_sheet({"Provoke": 1})),
        ],
        npcs=[_npc("Manipulator", {"Provoke": 3})],
        encounter=enc,
    )
    decision = decide_opponent_action(
        encounter=enc, snapshot=snap, opponent=enc.find_actor("Manipulator"), mental=True
    )
    assert decision is not None
    # Cross-track wound priority wins over Fresh's higher Provoke threat rating.
    assert decision.target == "Scarred"


# (b) no wound → targets the PC whose fate_commits entry attacked this opponent.
def test_targets_pc_who_attacked_this_opponent():
    from sidequest.game.encounter import FateSealedCommit

    enc = _enc(
        [
            EncounterActor(name="Aggressor", role="lead", side="player"),
            EncounterActor(name="Bystander", role="muscle", side="player"),
            EncounterActor(name="Thug", role="foe", side="opponent"),
        ]
    )
    # Bystander has the higher Fight; without the commit, threat-targeting would pick them.
    snap = GameSnapshot(
        genre_slug="fate_test",
        characters=[_pc("Aggressor", {"Fight": 1}), _pc("Bystander", {"Fight": 5})],
        npcs=[_npc("Thug", {"Fight": 2})],
        encounter=enc,
    )
    enc.fate_commits.append(
        FateSealedCommit(actor="Aggressor", action="attack", skill="Fight", target="Thug")
    )
    decision = decide_opponent_action(
        encounter=enc, snapshot=snap, opponent=enc.find_actor("Thug"), mental=False
    )
    assert decision is not None
    assert decision.target == "Aggressor"


def test_attack_commit_at_other_opponent_does_not_retaliate():
    from sidequest.game.encounter import FateSealedCommit

    enc = _enc(
        [
            EncounterActor(name="Aggressor", role="lead", side="player"),
            EncounterActor(name="Strong", role="muscle", side="player"),
            EncounterActor(name="Thug", role="foe", side="opponent"),
            EncounterActor(name="OtherFoe", role="foe2", side="opponent"),
        ]
    )
    snap = GameSnapshot(
        genre_slug="fate_test",
        characters=[_pc("Aggressor", {"Fight": 1}), _pc("Strong", {"Fight": 5})],
        npcs=[_npc("Thug", {"Fight": 2}), _npc("OtherFoe", {"Fight": 2})],
        encounter=enc,
    )
    # Aggressor attacked OtherFoe, not Thug → Thug falls through to highest-threat (Strong).
    enc.fate_commits.append(
        FateSealedCommit(actor="Aggressor", action="attack", skill="Fight", target="OtherFoe")
    )
    decision = decide_opponent_action(
        encounter=enc, snapshot=snap, opponent=enc.find_actor("Thug"), mental=False
    )
    assert decision is not None
    assert decision.target == "Strong"


# (c) neither → targets the highest-"Fight" PC; seating-order tiebreak on equal ratings.
def test_targets_highest_fight_pc():
    enc = _enc(
        [
            EncounterActor(name="Weak", role="lead", side="player"),
            EncounterActor(name="Deadly", role="muscle", side="player"),
            EncounterActor(name="Thug", role="foe", side="opponent"),
        ]
    )
    snap = GameSnapshot(
        genre_slug="fate_test",
        characters=[_pc("Weak", {"Fight": 1}), _pc("Deadly", {"Fight": 4})],
        npcs=[_npc("Thug", {"Fight": 2})],
        encounter=enc,
    )
    decision = decide_opponent_action(
        encounter=enc, snapshot=snap, opponent=enc.find_actor("Thug"), mental=False
    )
    assert decision is not None
    assert decision.target == "Deadly"


def test_highest_threat_seating_order_tiebreak():
    enc = _enc(
        [
            EncounterActor(name="First", role="lead", side="player"),
            EncounterActor(name="Second", role="muscle", side="player"),
            EncounterActor(name="Thug", role="foe", side="opponent"),
        ]
    )
    snap = GameSnapshot(
        genre_slug="fate_test",
        characters=[_pc("First", {"Fight": 3}), _pc("Second", {"Fight": 3})],
        npcs=[_npc("Thug", {"Fight": 2})],
        encounter=enc,
    )
    decision = decide_opponent_action(
        encounter=enc, snapshot=snap, opponent=enc.find_actor("Thug"), mental=False
    )
    assert decision is not None
    assert decision.target == "First"


def test_physical_threat_ranks_on_fight_alone_not_max_of_attack_skills():
    # Step-3 threat ranking must use the SINGLE canonical skill (Fight), NOT
    # max(Fight, Shoot). Sniper is a high-Shoot specialist; Brawler is the better
    # fighter. Both unwounded, neither retaliating → opponent must pick Brawler.
    enc = _enc(
        [
            EncounterActor(name="Sniper", role="lead", side="player"),
            EncounterActor(name="Brawler", role="muscle", side="player"),
            EncounterActor(name="Thug", role="foe", side="opponent"),
        ]
    )
    snap = GameSnapshot(
        genre_slug="fate_test",
        characters=[
            _pc("Sniper", {"Fight": 1, "Shoot": 5}),
            _pc("Brawler", {"Fight": 3, "Shoot": 0}),
        ],
        npcs=[_npc("Thug", {"Fight": 2})],
        encounter=enc,
    )
    decision = decide_opponent_action(
        encounter=enc, snapshot=snap, opponent=enc.find_actor("Thug"), mental=False
    )
    assert decision is not None
    # max(Fight,Shoot) would pick Sniper (5); Fight-alone correctly picks Brawler (3).
    assert decision.target == "Brawler"


def test_mental_threat_uses_provoke():
    enc = _enc(
        [
            EncounterActor(name="Stoic", role="lead", side="player"),
            EncounterActor(name="Loud", role="muscle", side="player"),
            EncounterActor(name="Manipulator", role="foe", side="opponent"),
        ]
    )
    # Loud has higher Provoke; Stoic has higher Fight. Mental conflict → Provoke decides.
    snap = GameSnapshot(
        genre_slug="fate_test",
        characters=[
            _pc("Stoic", {"Fight": 5, "Provoke": 1}),
            _pc("Loud", {"Fight": 1, "Provoke": 4}),
        ],
        npcs=[_npc("Manipulator", {"Provoke": 3})],
        encounter=enc,
    )
    decision = decide_opponent_action(
        encounter=enc, snapshot=snap, opponent=enc.find_actor("Manipulator"), mental=True
    )
    assert decision is not None
    assert decision.target == "Loud"


# (d) returns None when every player-side actor is withdrawn.
def test_returns_none_when_all_players_withdrawn():
    enc = _enc(
        [
            EncounterActor(name="Fallen", role="lead", side="player", withdrawn=True),
            EncounterActor(name="Thug", role="foe", side="opponent"),
        ]
    )
    snap = GameSnapshot(
        genre_slug="fate_test",
        characters=[_pc("Fallen", {"Fight": 3})],
        npcs=[_npc("Thug", {"Fight": 2})],
        encounter=enc,
    )
    decision = decide_opponent_action(
        encounter=enc, snapshot=snap, opponent=enc.find_actor("Thug"), mental=False
    )
    assert decision is None


def test_returns_none_when_no_player_side_actors():
    enc = _enc([EncounterActor(name="Thug", role="foe", side="opponent")])
    snap = GameSnapshot(
        genre_slug="fate_test", characters=[], npcs=[_npc("Thug", {"Fight": 2})], encounter=enc
    )
    decision = decide_opponent_action(
        encounter=enc, snapshot=snap, opponent=enc.find_actor("Thug"), mental=False
    )
    assert decision is None


def test_withdrawn_wounded_pc_is_skipped_for_a_live_one():
    enc = _enc(
        [
            EncounterActor(name="DownAndOut", role="lead", side="player", withdrawn=True),
            EncounterActor(name="Standing", role="muscle", side="player"),
            EncounterActor(name="Thug", role="foe", side="opponent"),
        ]
    )
    snap = GameSnapshot(
        genre_slug="fate_test",
        characters=[
            _pc("DownAndOut", {"Fight": 9}, sheet=_wounded_consequence_sheet({"Fight": 9})),
            _pc("Standing", {"Fight": 1}),
        ],
        npcs=[_npc("Thug", {"Fight": 2})],
        encounter=enc,
    )
    decision = decide_opponent_action(
        encounter=enc, snapshot=snap, opponent=enc.find_actor("Thug"), mental=False
    )
    assert decision is not None
    assert decision.target == "Standing"


# (e) opponent skill selection.
def test_opponent_picks_highest_rated_attack_skill():
    enc = _enc(
        [
            EncounterActor(name="Hero", role="lead", side="player"),
            EncounterActor(name="Gunner", role="foe", side="opponent"),
        ]
    )
    snap = GameSnapshot(
        genre_slug="fate_test",
        characters=[_pc("Hero", {"Fight": 2})],
        npcs=[_npc("Gunner", {"Fight": 2, "Shoot": 5})],
        encounter=enc,
    )
    decision = decide_opponent_action(
        encounter=enc, snapshot=snap, opponent=enc.find_actor("Gunner"), mental=False
    )
    assert decision is not None
    assert decision.action == "attack"
    assert decision.skill == "Shoot"


def test_physical_default_skill_order_tiebreak_picks_fight():
    enc = _enc(
        [
            EncounterActor(name="Hero", role="lead", side="player"),
            EncounterActor(name="Brawler", role="foe", side="opponent"),
        ]
    )
    snap = GameSnapshot(
        genre_slug="fate_test",
        characters=[_pc("Hero", {"Fight": 2})],
        npcs=[_npc("Brawler", {"Fight": 3, "Shoot": 3})],
        encounter=enc,
    )
    decision = decide_opponent_action(
        encounter=enc, snapshot=snap, opponent=enc.find_actor("Brawler"), mental=False
    )
    assert decision is not None
    # Equal ratings → declared list order ["Fight", "Shoot"] → Fight.
    assert decision.skill == "Fight"


def test_mental_attack_uses_provoke():
    enc = _enc(
        [
            EncounterActor(name="Hero", role="lead", side="player"),
            EncounterActor(name="Manipulator", role="foe", side="opponent"),
        ]
    )
    snap = GameSnapshot(
        genre_slug="fate_test",
        characters=[_pc("Hero", {"Provoke": 2})],
        npcs=[_npc("Manipulator", {"Provoke": 4})],
        encounter=enc,
    )
    decision = decide_opponent_action(
        encounter=enc, snapshot=snap, opponent=enc.find_actor("Manipulator"), mental=True
    )
    assert decision is not None
    assert decision.skill == "Provoke"


def test_unskilled_opponent_falls_back_to_canonical_skill_not_none():
    enc = _enc(
        [
            EncounterActor(name="Hero", role="lead", side="player"),
            EncounterActor(name="Pacifist", role="foe", side="opponent"),
        ]
    )
    # Pacifist has NO attack skills rated → unskilled swing at Fight (physical canonical).
    snap = GameSnapshot(
        genre_slug="fate_test",
        characters=[_pc("Hero", {"Fight": 2})],
        npcs=[_npc("Pacifist", {"Notice": 4, "Will": 3})],
        encounter=enc,
    )
    decision = decide_opponent_action(
        encounter=enc, snapshot=snap, opponent=enc.find_actor("Pacifist"), mental=False
    )
    assert decision is not None
    assert decision.skill == "Fight"
    assert decision.target == "Hero"


def test_unskilled_opponent_mental_falls_back_to_provoke():
    enc = _enc(
        [
            EncounterActor(name="Hero", role="lead", side="player"),
            EncounterActor(name="Quiet", role="foe", side="opponent"),
        ]
    )
    snap = GameSnapshot(
        genre_slug="fate_test",
        characters=[_pc("Hero", {"Provoke": 2})],
        npcs=[_npc("Quiet", {"Notice": 4})],
        encounter=enc,
    )
    decision = decide_opponent_action(
        encounter=enc, snapshot=snap, opponent=enc.find_actor("Quiet"), mental=True
    )
    assert decision is not None
    assert decision.skill == "Provoke"


def test_opponent_without_fate_sheet_raises():
    enc = _enc(
        [
            EncounterActor(name="Hero", role="lead", side="player"),
            EncounterActor(name="Ghost", role="foe", side="opponent"),
        ]
    )
    # Ghost is seated but has no CreatureCore in the snapshot → impossible seated state.
    snap = GameSnapshot(
        genre_slug="fate_test", characters=[_pc("Hero", {"Fight": 2})], npcs=[], encounter=enc
    )
    with pytest.raises(ValueError):
        decide_opponent_action(
            encounter=enc, snapshot=snap, opponent=enc.find_actor("Ghost"), mental=False
        )


def test_decision_is_frozen():
    decision = OpponentDecision(action="attack", skill="Fight", target="Hero")
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.skill = "Shoot"  # type: ignore[misc]
