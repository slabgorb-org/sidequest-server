from __future__ import annotations

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.fate_sheet import Aspect, FateSheet
from sidequest.game.session import GameSnapshot
from sidequest.server.dispatch.fate_conflict import (
    FateConflictError,
    fate_barrier_closed,
    fate_turn_order,
    fate_waiting_actors,
    seal_fate_commit,
)


def _pc(name: str, skills: dict[str, int]) -> Character:
    core = CreatureCore(
        name=name, description="d", personality="p", fate_sheet=FateSheet(skills=skills)
    )
    return Character(core=core, char_class="Agent", race="Human", backstory="b")


def _enc(actors: list[EncounterActor], *, category: str = "combat") -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="duel",
        category=category,
        player_metric=EncounterMetric(name="p", threshold=10),
        opponent_metric=EncounterMetric(name="o", threshold=10),
        actors=actors,
    )


def test_barrier_waits_on_pcs_then_closes():
    enc = _enc(
        [
            EncounterActor(name="Vesska", role="lead", side="player"),
            EncounterActor(name="Brakka", role="muscle", side="player"),
            EncounterActor(name="Thug", role="foe", side="opponent"),
        ]
    )
    snap = GameSnapshot(
        genre_slug="fate_test",
        characters=[_pc("Vesska", {"Fight": 3}), _pc("Brakka", {"Fight": 2})],
        encounter=enc,
    )
    assert set(fate_waiting_actors(encounter=enc, snapshot=snap)) == {"Vesska", "Brakka"}
    assert fate_barrier_closed(encounter=enc, snapshot=snap) is False

    seal_fate_commit(
        encounter=enc, actor=enc.find_actor("Vesska"), action="attack", skill="Fight",
        target="Thug", ladder_total=5,
    )
    assert fate_waiting_actors(encounter=enc, snapshot=snap) == ["Brakka"]
    assert fate_barrier_closed(encounter=enc, snapshot=snap) is False

    seal_fate_commit(
        encounter=enc, actor=enc.find_actor("Brakka"), action="overcome", skill="Athletics",
        difficulty=1, ladder_total=2,
    )
    assert fate_barrier_closed(encounter=enc, snapshot=snap) is True


def test_double_commit_fails_loud():
    enc = _enc([EncounterActor(name="Vesska", role="lead", side="player")])
    GameSnapshot(genre_slug="fate_test", characters=[_pc("Vesska", {"Fight": 3})], encounter=enc)
    seal_fate_commit(encounter=enc, actor=enc.find_actor("Vesska"), action="overcome", skill="Athletics", difficulty=2, ladder_total=4)
    with pytest.raises(FateConflictError):
        seal_fate_commit(encounter=enc, actor=enc.find_actor("Vesska"), action="attack", skill="Fight", target="x", ladder_total=4)


def test_turn_order_uses_notice_for_physical_empathy_for_mental():
    enc_actors = [
        EncounterActor(name="Quick", role="a", side="player"),
        EncounterActor(name="Slow", role="b", side="opponent"),
    ]
    physical = _enc(enc_actors, category="combat")
    snap = GameSnapshot(
        genre_slug="fate_test",
        characters=[_pc("Quick", {"Notice": 4, "Empathy": 1})],
        npcs=[],
        encounter=physical,
    )
    # Seat the opponent as an NPC so find_creature_core resolves its sheet.
    from sidequest.game.session import Npc

    snap.npcs.append(
        Npc(core=CreatureCore(name="Slow", description="d", personality="p", fate_sheet=FateSheet(skills={"Notice": 2, "Empathy": 5})))
    )
    assert fate_turn_order(encounter=physical, snapshot=snap, mental=False) == ["Quick", "Slow"]
    # Mental conflict flips it: Slow's Empathy 5 beats Quick's Empathy 1.
    assert fate_turn_order(encounter=physical, snapshot=snap, mental=True) == ["Slow", "Quick"]


def test_absorb_uses_one_stress_box_when_it_covers_the_hit():
    from sidequest.game.ruleset import get_ruleset_module
    from sidequest.server.dispatch.fate_conflict import absorb_shifts

    module = get_ruleset_module("fate")
    sheet = FateSheet()  # physical boxes [1,2]; consequences open
    survived = absorb_shifts(
        module=module, sheet=sheet, track="physical", shifts=2, actor="Hero", source="Thug"
    )
    assert survived is True
    assert sheet.stress["physical"].boxes[1].checked is True  # the value-2 box
    assert all(c.aspect is None for c in sheet.consequences)  # no consequence needed


def test_absorb_combines_one_box_plus_consequences():
    from sidequest.game.ruleset import get_ruleset_module
    from sidequest.server.dispatch.fate_conflict import absorb_shifts

    module = get_ruleset_module("fate")
    sheet = FateSheet()  # best single box = 2; consequences 2/4/6/8
    survived = absorb_shifts(
        module=module, sheet=sheet, track="physical", shifts=5, actor="Hero", source="Thug"
    )
    assert survived is True
    # 5 shifts: largest box (2) + mild consequence (2) leaves 1 → moderate (4) covers it.
    assert sheet.stress["physical"].boxes[1].checked is True
    filled = [c.level for c in sheet.consequences if c.aspect is not None]
    assert filled == ["mild", "moderate"]
    # The consequence text is a real, descriptive default naming the source.
    mild = next(c for c in sheet.consequences if c.level == "mild")
    assert "Thug" in mild.aspect.text


def test_absorb_returns_false_when_capacity_exhausted():
    from sidequest.game.ruleset import get_ruleset_module
    from sidequest.server.dispatch.fate_conflict import absorb_shifts

    module = get_ruleset_module("fate")
    sheet = FateSheet()
    # Pre-deplete: check every stress box, fill every consequence slot.
    for b in sheet.stress["physical"].boxes:
        b.checked = True
    for c in sheet.consequences:
        c.aspect = Aspect(text="old wound", kind="consequence", free_invokes=0)
    survived = absorb_shifts(
        module=module, sheet=sheet, track="physical", shifts=1, actor="Hero", source="Thug"
    )
    assert survived is False  # nothing left to absorb with → taken out


def test_absorb_rejects_nonpositive_shifts():
    from sidequest.game.ruleset import get_ruleset_module
    from sidequest.server.dispatch.fate_conflict import FateConflictError, absorb_shifts

    module = get_ruleset_module("fate")
    with pytest.raises(FateConflictError, match="shifts >= 1"):
        absorb_shifts(module=module, sheet=FateSheet(), track="physical", shifts=0, actor="Hero", source="Thug")


def test_absorb_rejects_unknown_track():
    from sidequest.game.ruleset import get_ruleset_module
    from sidequest.server.dispatch.fate_conflict import FateConflictError, absorb_shifts

    module = get_ruleset_module("fate")
    with pytest.raises(FateConflictError, match="stress track"):
        absorb_shifts(module=module, sheet=FateSheet(), track="corruption", shifts=1, actor="Hero", source="Thug")  # type: ignore[arg-type]
