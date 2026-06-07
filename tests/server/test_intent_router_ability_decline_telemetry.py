"""Ability/beat invocation decline telemetry after decompose.

sq-playtest 2026-06-07 (Reroute Power, perseus MP + five_points Size Up): a
signature ability declared verbatim ("I use Reroute Power — …") produced ZERO
ability/dispatch/gate spans — the ADR-123 dispatch bank has NO ability
subsystem, so the router cannot route the declaration anywhere, and nothing
recorded the decline. Opposite direction: a confrontation-only beat invoked by
name OUTSIDE any confrontation ("Size Up") was freehanded as prose with zero
gate telemetry. Both directions: silence indistinguishable from "feature
doesn't exist".

The pass now emits deterministic decline evidence (twin of the standoff-seam
``intent_router.confrontation_classified`` detector, PR #750):

* ``intent_router.ability_invocation_unrouted`` — a party character's ADR-097
  ability name appears word-boundary in the action text; no dispatch route
  exists for abilities (none does today), so the GM panel sees the declared
  ability that the engine could not mechanically engage.
* ``intent_router.beat_invoked_outside_confrontation`` — a MULTI-WORD beat
  label from the pack's confrontation defs appears in the action text while no
  confrontation is active and the router emitted no confrontation dispatch.
  (Single-word labels like "Shoot" are ordinary verbs, not invocations —
  excluded by design to keep the lie-detector quiet on normal prose.)

Same harness as test_intent_router_confrontation_classified.py.
"""

from __future__ import annotations

import logging

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.session import GameSnapshot
from sidequest.protocol.dispatch import DispatchPackage
from sidequest.protocol.models import AbilityDefinition, AbilitySource
from sidequest.server.intent_router_pass import execute_intent_router_pre_narrator_pass

ABILITY_SPAN = "intent_router.ability_invocation_unrouted"
BEAT_SPAN = "intent_router.beat_invoked_outside_confrontation"


class _FakeBeat:
    def __init__(self, beat_id: str, label: str):
        self.id = beat_id
        self.label = label


class _FakeConfrontationDef:
    def __init__(self, confrontation_type: str, beats: list[_FakeBeat]):
        self.confrontation_type = confrontation_type
        self.category = "combat"
        self.intent_verbs: list[str] = []
        self.beats = beats


class _FakeRules:
    def __init__(self, confrontations):
        self.confrontations = confrontations


class _FakePack:
    def __init__(self, confrontations):
        self.rules = _FakeRules(confrontations)
        self.witnessed_acts = None


class _StubRouter:
    def __init__(self, package: DispatchPackage):
        self._package = package

    async def decompose(self, *, action, state_summary):
        return self._package


def _empty_package() -> DispatchPackage:
    return DispatchPackage(turn_id="t1", per_player=[], cross_player=[], confidence_global=0.5)


def _engineer(name: str = "Chico") -> Character:
    return Character(
        core=CreatureCore(name=name, description="X.", personality="Y."),
        backstory="Z.",
        char_class="Engineer",
        race="human",
        abilities=[
            AbilityDefinition(
                name="Reroute Power",
                genre_description="Spec sheets are suggestions.",
                mechanical_effect="Once per ship combat, restore a track OR next broadside at advantage.",
                source=AbilitySource.Class,
            )
        ],
    )


def _snapshot(*, with_engineer: bool = True) -> GameSnapshot:
    snap = GameSnapshot(world_slug="perseus_cloud")
    snap.genre_slug = "space_opera"
    if with_engineer:
        snap.characters.append(_engineer())
    return snap


def _pack_with_beats() -> _FakePack:
    return _FakePack(
        [
            _FakeConfrontationDef(
                "standoff",
                [_FakeBeat("size_up", "Size Up"), _FakeBeat("shoot", "Shoot")],
            )
        ]
    )


@pytest.fixture
def otel_capture():
    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


def _spans(exporter, name: str):
    return [s for s in exporter.get_finished_spans() if s.name == name]


@pytest.mark.asyncio
async def test_ability_declaration_emits_unrouted_span(otel_capture, caplog):
    """The perseus repro: Reroute Power declared verbatim, no ability subsystem
    in the bank → the span + a WARNING must record the decline."""
    router = _StubRouter(_empty_package())
    with caplog.at_level(logging.WARNING):
        await execute_intent_router_pre_narrator_pass(
            intent_router=router,
            snapshot=_snapshot(),
            pack=_pack_with_beats(),
            action=(
                "I use Reroute Power — dump the bubble-shield overcharge straight "
                "into the gun capacitors and fire a full broadside."
            ),
            player_name="Chico",
        )
    spans = _spans(otel_capture, ABILITY_SPAN)
    assert len(spans) == 1
    attrs = spans[0].attributes or {}
    assert attrs["ability"] == "Reroute Power"
    assert attrs["character"] == "Chico"
    assert attrs["genre_slug"] == "space_opera"
    warned = [r for r in caplog.records if "ability_invocation_unrouted" in r.getMessage()]
    assert warned, "the decline must log a WARNING naming the unrouted ability"


@pytest.mark.asyncio
async def test_no_ability_name_no_span(otel_capture):
    """Ordinary prose mentioning no ability stays span-free."""
    router = _StubRouter(_empty_package())
    await execute_intent_router_pre_narrator_pass(
        intent_router=router,
        snapshot=_snapshot(),
        pack=_pack_with_beats(),
        action="I reroute the conversation toward the cargo manifest.",
        player_name="Chico",
    )
    assert _spans(otel_capture, ABILITY_SPAN) == []


@pytest.mark.asyncio
async def test_beat_invoked_outside_confrontation_emits_decline(otel_capture, caplog):
    """The five_points Size Up repro: a multi-word beat label invoked by name
    with no active confrontation and no confrontation dispatch emitted → the
    gate decline must be visible."""
    router = _StubRouter(_empty_package())
    with caplog.at_level(logging.WARNING):
        await execute_intent_router_pre_narrator_pass(
            intent_router=router,
            snapshot=_snapshot(),
            pack=_pack_with_beats(),
            action="I Size Up the man by the stove before saying a word.",
            player_name="Chico",
        )
    spans = _spans(otel_capture, BEAT_SPAN)
    assert len(spans) == 1
    attrs = spans[0].attributes or {}
    assert attrs["beat_id"] == "size_up"
    assert attrs["confrontation_type"] == "standoff"
    warned = [r for r in caplog.records if "beat_invoked_outside_confrontation" in r.getMessage()]
    assert warned


@pytest.mark.asyncio
async def test_single_word_beat_labels_are_not_invocations(otel_capture):
    """'I shoot the lock' is a verb, not a beat invocation — single-word
    labels never fire the beat decline span."""
    router = _StubRouter(_empty_package())
    await execute_intent_router_pre_narrator_pass(
        intent_router=router,
        snapshot=_snapshot(),
        pack=_pack_with_beats(),
        action="I shoot the lock off the cabinet.",
        player_name="Chico",
    )
    assert _spans(otel_capture, BEAT_SPAN) == []


def test_beat_commit_dice_path_emits_ability_decline(otel_capture, caplog):
    """The perseus repro path: in-confrontation actions ride
    ``DiceThrowPayload.player_action`` into a router-SUPPRESSED replay turn
    (story 91-2) — the router pass never sees them. The dice dispatcher must
    scan the typed text itself: Reroute Power declared alongside the broadside
    beat commit emits the unrouted span with ``in_confrontation=True``."""
    from unittest.mock import MagicMock

    from sidequest.game.creature_core import Inventory
    from sidequest.game.encounter import (
        EncounterActor,
        EncounterMetric,
        EncounterPhase,
        StructuredEncounter,
    )
    from sidequest.game.session import Npc
    from sidequest.genre.models.rules import (
        BeatDef,
        ConfrontationDef,
        MetricDef,
        ResolutionMode,
        RulesConfig,
    )
    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.server.dispatch.dice import dispatch_dice_throw

    broadside = BeatDef.model_validate(
        {
            "id": "broadside",
            "label": "Broadside",
            "kind": "strike",
            "base": 2,
            "stat_check": "Intellect",
            "narrator_hint": "All guns fire.",
        }
    )
    cdef = ConfrontationDef(
        type="ship_combat",
        label="Ship Combat",
        category="combat",
        resolution_mode=ResolutionMode.beat_selection,
        player_metric=MetricDef(name="momentum", starting=0, threshold=10),
        opponent_metric=MetricDef(name="momentum", starting=0, threshold=10),
        beats=[broadside],
    )
    pack = MagicMock()
    pack.rules = RulesConfig(confrontations=[cdef])
    pack.inventory = None
    pack.classes = []

    snap = _snapshot()
    opp_core = CreatureCore(
        name="Thari Cutter",
        description="A raider.",
        personality="hostile",
        inventory=Inventory(),
    )
    snap.npcs.append(Npc(core=opp_core))
    enc = StructuredEncounter(
        encounter_type="ship_combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        beat=0,
        structured_phase=EncounterPhase.Setup,
        actors=[
            EncounterActor(name="Chico", role="combatant", side="player"),
            EncounterActor(name="Thari Cutter", role="combatant", side="opponent"),
        ],
        resolved=False,
    )
    snap.encounter = enc

    with caplog.at_level(logging.WARNING):
        dispatch_dice_throw(
            payload=DiceThrowPayload(
                request_id="rp-1",
                throw_params=ThrowParams(
                    velocity=(0.0, 5.0, -2.0),
                    angular=(1.0, 1.0, 1.0),
                    position=(0.5, 0.5),
                ),
                face=[12],
                beat_id="broadside",
                player_action=(
                    "I use Reroute Power — dump the bubble-shield overcharge "
                    "into the gun capacitors and fire a full broadside."
                ),
            ),
            rolling_player_id="player-chico",
            character_name="Chico",
            character_stats={"Intellect": 12, "Reflex": 10, "Resolve": 10, "Cunning": 10},
            encounter=enc,
            pack=pack,
            genre_slug="space_opera",
            session_id="rp-dice-test",
            round_number=1,
            room_broadcast=[].append,
            snapshot=snap,
        )

    spans = _spans(otel_capture, ABILITY_SPAN)
    assert len(spans) == 1, (
        "the beat-commit dice path must scan player_action for ability "
        "declarations — the router pass never sees suppressed-replay turns"
    )
    attrs = spans[0].attributes or {}
    assert attrs["ability"] == "Reroute Power"
    assert attrs["character"] == "Chico"
    assert attrs["in_confrontation"] is True
    warned = [r for r in caplog.records if "ability_invocation_unrouted" in r.getMessage()]
    assert warned


@pytest.mark.asyncio
async def test_active_confrontation_suppresses_beat_decline(otel_capture):
    """During a live confrontation the beat picker owns beat invocations —
    the outside-confrontation decline must not fire."""
    from sidequest.game.encounter import EncounterMetric, StructuredEncounter

    snap = _snapshot()
    snap.encounter = StructuredEncounter(
        encounter_type="standoff",
        category="pre_combat",
        player_metric=EncounterMetric(name="nerve", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="nerve", current=0, starting=0, threshold=10),
    )
    router = _StubRouter(_empty_package())
    await execute_intent_router_pre_narrator_pass(
        intent_router=router,
        snapshot=snap,
        pack=_pack_with_beats(),
        action="I Size Up the gunman across the table.",
        player_name="Chico",
    )
    assert _spans(otel_capture, BEAT_SPAN) == []
