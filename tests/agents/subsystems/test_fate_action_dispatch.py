from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from sidequest.agents.subsystems import SubsystemOutput, get_registered
from sidequest.agents.subsystems.fate_action import run_fate_action_dispatch
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.fate_sheet import Aspect, FateSheet
from sidequest.game.session import GameSnapshot, Npc
from sidequest.protocol.dispatch import SubsystemDispatch, VisibilityTag


class _FixedRng:
    """A deterministic stand-in for ``random.Random`` — the Fate engine only
    calls ``.choice(...)`` for the 4dF roll. Returns the fixed value so every
    die is neutral (0): Hero(Fight 4) vs Thug(0) lands +4 shifts, taking out the
    fully-depleted Thug every run. Mirrors the F1d test double in
    ``tests/server/dispatch/test_fate_dispatch_routing.py``."""

    def __init__(self, value: int = 0) -> None:
        self._value = value

    def choice(self, seq):
        return self._value


def _pc(name: str, skills: dict[str, int]) -> Character:
    core = CreatureCore(
        name=name, description="d", personality="p", fate_sheet=FateSheet(skills=skills)
    )
    return Character(core=core, char_class="Agent", race="Human", backstory="b")


def _depleted_thug() -> Npc:
    sheet = FateSheet(skills={"Athletics": 0})
    for b in sheet.stress["physical"].boxes:
        b.checked = True
    for c in sheet.consequences:
        c.aspect = Aspect(text="old wound", kind="consequence", free_invokes=0)
    return Npc(core=CreatureCore(name="Thug", description="d", personality="p", fate_sheet=sheet))


def _solo_combat() -> tuple[GameSnapshot, StructuredEncounter]:
    enc = StructuredEncounter(
        encounter_type="duel",
        category="combat",
        player_metric=EncounterMetric(name="p", threshold=10),
        opponent_metric=EncounterMetric(name="o", threshold=10),
        actors=[
            EncounterActor(name="Hero", role="lead", side="player"),
            EncounterActor(name="Thug", role="foe", side="opponent"),
        ],
    )
    snap = GameSnapshot(
        genre_slug="fate_test", characters=[_pc("Hero", {"Fight": 4})], encounter=enc
    )
    snap.npcs.append(_depleted_thug())
    return snap, enc


def _fate_pack():
    return SimpleNamespace(rules=SimpleNamespace(ruleset="fate"))


def _dispatch(action: str, **params) -> SubsystemDispatch:
    return SubsystemDispatch(
        subsystem="fate_action",
        params={"action": action, **params},
        idempotency_key="fate_action_t1",
        visibility=VisibilityTag(visible_to="all"),
        confidence=0.95,
    )


def test_handler_builds_payload_routes_and_emits_classified_span(otel_capture):
    exporter = otel_capture
    snap, enc = _solo_combat()
    out = asyncio.run(
        run_fate_action_dispatch(
            _dispatch("attack", skill="Fight", target="Thug"),
            snapshot=snap,
            pack=_fate_pack(),
            player_name="Hero",
            rng=_FixedRng(0),  # neutral 4dF → the +4-skill attack lands deterministically
        )
    )
    assert isinstance(out, SubsystemOutput)
    # Routed to dispatch_fate_action → solo barrier closed → exchange resolved.
    assert enc.find_actor("Thug").withdrawn is True
    assert enc.resolved is True
    names = [s.name for s in exporter.get_finished_spans()]
    assert "fate.action.classified" in names
    assert "fate.exchange.resolved" in names


def test_invalid_action_fails_loud():
    snap, _ = _solo_combat()
    with pytest.raises(ValueError):
        asyncio.run(
            run_fate_action_dispatch(
                _dispatch("parry", skill="Fight", target="Thug"),  # not one of the four
                snapshot=snap,
                pack=_fate_pack(),
                player_name="Hero",
            )
        )


def test_fate_action_is_a_registered_subsystem():
    # Runtime registry membership (not a source grep) — the bank can resolve it.
    assert "fate_action" in get_registered()


def test_non_fate_ruleset_returns_error_not_silent_success():
    snap, enc = _solo_combat()
    out = asyncio.run(
        run_fate_action_dispatch(
            _dispatch("attack", skill="Fight", target="Thug"),
            snapshot=snap,
            pack=SimpleNamespace(rules=SimpleNamespace(ruleset="native")),  # NOT fate
            player_name="Hero",
        )
    )
    assert (
        out.data.get("error") == "fate_dispatch_error"
    )  # dispatch_fate_action raised, caught loud
    assert enc.resolved is False  # nothing engaged
