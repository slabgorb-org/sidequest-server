"""Story 118-10 (ADR-144 F3d-pre) — the F2a subsystem mirrors the new wire fields.

``FateActionPayload`` gains ``invoke_mode`` and ``player_action`` (118-10). The
story requires both the explicit F1d ``FATE_ACTION`` channel AND the router-driven
F2a freeform channel to carry them. ``run_fate_action_dispatch`` builds the
payload from ``dispatch.params`` (subsystems/fate_action.py) — it currently omits
the two new fields, so a router that classifies a reroll-invoke or attaches a
flavor rider would silently lose them.

These are the WIRING tests for the story (CLAUDE.md: "Every Test Suite Needs a
Wiring Test"): they prove the new payload fields reach the engine through the REAL
production F2a engager (``run_fate_action_dispatch`` → ``dispatch_fate_action`` →
ruleset), observed on OTEL spans — not a protocol model checked in isolation.

RED today (F2a does not read the new params):
  * ``…f2a_threads_invoke_mode_reroll_to_the_span``
  * ``…f2a_player_action_emits_fate_flavor_rider_span``
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from sidequest.agents.subsystems.fate_action import run_fate_action_dispatch
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.fate_sheet import Aspect, FateSheet
from sidequest.game.session import GameSnapshot, Npc
from sidequest.protocol.dispatch import SubsystemDispatch, VisibilityTag

_FLAVOR_SPAN = "fate.action.flavor_rider"
_INVOKE_SPAN = "fate.aspect.invoked"
_RIDER = "I swing from the chandelier and fire"


class _FixedRng:
    def __init__(self, value: int = 0) -> None:
        self._value = value

    def choice(self, seq):
        return self._value


def _depleted_thug() -> Npc:
    sheet = FateSheet(skills={"Athletics": 0})
    for b in sheet.stress["physical"].boxes:
        b.checked = True
    for c in sheet.consequences:
        c.aspect = Aspect(text="old wound", kind="consequence", free_invokes=0)
    return Npc(core=CreatureCore(name="Thug", description="d", personality="p", fate_sheet=sheet))


def _hero_with_invokable_aspect() -> Character:
    core = CreatureCore(
        name="Hero",
        description="d",
        personality="p",
        fate_sheet=FateSheet(
            fate_points=2,
            skills={"Fight": 4},
            aspects=[Aspect(text="High Ground", kind="situation", free_invokes=1)],
        ),
    )
    return Character(core=core, char_class="Agent", race="Human", backstory="b")


def _solo_combat(hero: Character) -> tuple[GameSnapshot, StructuredEncounter]:
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
    snap = GameSnapshot(genre_slug="fate_test", characters=[hero], encounter=enc)
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


def test_f2a_threads_invoke_mode_reroll_to_the_span(otel_capture):
    """RED: a router-classified Fate action with ``params['invoke_mode']='reroll'``
    must reach ``invoke_aspect`` as ``mode='reroll'`` — observable on the
    ``fate.aspect.invoked`` span. ``run_fate_action_dispatch`` builds the payload
    without ``invoke_mode``, so the field defaults to 'bonus' and the span reports
    'bonus'. This proves the F2a freeform channel carries the reroll intent."""
    snap, _ = _solo_combat(_hero_with_invokable_aspect())
    asyncio.run(
        run_fate_action_dispatch(
            _dispatch(
                "attack",
                skill="Fight",
                target="Thug",
                invoke_aspect="High Ground",
                invoke_mode="reroll",
            ),
            snapshot=snap,
            pack=_fate_pack(),
            player_name="Hero",
            rng=_FixedRng(0),
        )
    )

    invoked = [s for s in otel_capture.get_finished_spans() if s.name == _INVOKE_SPAN]
    assert invoked, (
        f"the F2a-routed invoke did not emit {_INVOKE_SPAN!r}; spans: "
        f"{[s.name for s in otel_capture.get_finished_spans()]}"
    )
    mode = dict(invoked[0].attributes or {}).get("mode")
    assert mode == "reroll", (
        "run_fate_action_dispatch dropped params['invoke_mode']='reroll' when "
        f"building the FateActionPayload — invoked as {mode!r}. The F2a freeform "
        "channel must mirror F1d and carry invoke_mode (story 118-10)"
    )


def test_f2a_player_action_emits_fate_flavor_rider_span(otel_capture):
    """RED: a router-classified Fate action with ``params['player_action']`` (the
    freeform text the player typed) must emit ``fate.action.flavor_rider`` —
    proving F2a threads the rider into the narrator-context path. The field is not
    read from params today, so no span fires."""
    snap, _ = _solo_combat(_hero_with_invokable_aspect())
    asyncio.run(
        run_fate_action_dispatch(
            _dispatch("attack", skill="Fight", target="Thug", player_action=_RIDER),
            snapshot=snap,
            pack=_fate_pack(),
            player_name="Hero",
            rng=_FixedRng(0),
        )
    )

    rider = [s for s in otel_capture.get_finished_spans() if s.name == _FLAVOR_SPAN]
    assert rider, (
        f"a router-classified Fate action carrying params['player_action'] did not "
        f"emit {_FLAVOR_SPAN!r} — the F2a channel dropped the freeform rider before "
        "the engine saw it. spans: "
        f"{[s.name for s in otel_capture.get_finished_spans()]}"
    )
    assert dict(rider[0].attributes or {}).get("attached") is True
