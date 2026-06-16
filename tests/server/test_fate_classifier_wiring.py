"""Wiring net for the freeform Fate action channel (ADR-144 F2a).

Neither assertion is a source grep (server CLAUDE.md "No Source-Text Wiring
Tests"):
  1. Runtime registry — ``fate_action`` resolves to its handler in the live bank
     registry (get_registered()).
  2. End-to-end — a fate_action SubsystemDispatch driven through the REAL
     run_dispatch_bank (with the real fate ruleset from get_ruleset_module)
     reaches dispatch_fate_action → run_fate_exchange: the exchange resolves and
     both fate.action.classified and fate.exchange.resolved fire.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from sidequest.agents.subsystems import get_registered, run_dispatch_bank
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.fate_sheet import Aspect, FateSheet
from sidequest.game.session import GameSnapshot, Npc
from sidequest.protocol.dispatch import (
    DispatchPackage,
    PlayerDispatch,
    SubsystemDispatch,
    VisibilityTag,
)


class _FixedRng:
    """Deterministic ``random.Random`` stand-in (Fate engine calls ``.choice``
    only). Neutral 4dF (0) → Hero(Fight 4) lands +4 shifts on the depleted Thug
    every run. Injected through the real bank's context-kwarg filtering."""

    def __init__(self, value: int = 0) -> None:
        self._value = value

    def choice(self, seq):
        return self._value


def _pc(name: str, skills: dict[str, int]) -> Character:
    return Character(
        core=CreatureCore(
            name=name, description="d", personality="p", fate_sheet=FateSheet(skills=skills)
        ),
        char_class="Agent",
        race="Human",
        backstory="b",
    )


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


def test_fate_action_is_registered_in_the_live_bank():
    assert "fate_action" in get_registered()


def test_freeform_fate_action_engages_the_exchange_through_the_bank(otel_capture):
    exporter = otel_capture
    snap, enc = _solo_combat()
    pack = SimpleNamespace(rules=SimpleNamespace(ruleset="fate"))
    package = DispatchPackage(
        turn_id="t1",
        per_player=[
            PlayerDispatch(
                player_id="p1",
                raw_action="I lunge at the thug with my blade",
                dispatch=[
                    SubsystemDispatch(
                        subsystem="fate_action",
                        params={"action": "attack", "skill": "Fight", "target": "Thug"},
                        idempotency_key="fate_action_t1",
                        visibility=VisibilityTag(visible_to="all"),
                        confidence=0.95,
                    )
                ],
            )
        ],
        confidence_global=0.95,
    )

    result = asyncio.run(
        run_dispatch_bank(
            package,
            context={
                "snapshot": snap,
                "pack": pack,
                "player_name": "Hero",
                "rng": _FixedRng(0),  # deterministic 4dF so the attack lands through the real bank
            },
        )
    )

    # Bank engaged the engine (confidence 0.95 ≥ 0.6 default), not degraded.
    assert result.decisions[0]["decision"] == "engaged"
    assert enc.find_actor("Thug").withdrawn is True
    assert enc.resolved is True
    names = [s.name for s in exporter.get_finished_spans()]
    assert "fate.action.classified" in names
    assert "fate.exchange.resolved" in names
