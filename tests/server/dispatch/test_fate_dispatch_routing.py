from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.fate_sheet import Aspect, FateSheet
from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.session import GameSnapshot, Npc
from sidequest.protocol.fate import FateActionPayload
from sidequest.server.dispatch.fate_conflict import (
    FateConflictError,
    dispatch_fate_action,
)


class _FixedRng:
    def __init__(self, value: int = 0) -> None:
        self._value = value

    def choice(self, seq):
        return self._value


def _otel():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


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


def _solo_combat():
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


def test_fate_bound_ruleset_routes_to_the_exchange():
    snap, enc = _solo_combat()
    ruleset = get_ruleset_module("fate")  # real registry: a "fate"-slug pack resolves here
    payload = FateActionPayload(request_id="r1", action="attack", skill="Fight", target="Thug")
    exporter, tracer = _otel()

    result = dispatch_fate_action(
        payload=payload,
        actor_name="Hero",
        encounter=enc,
        ruleset=ruleset,
        snapshot=snap,
        rng=_FixedRng(0),
        _tracer=tracer,
    )

    # Solo barrier closes immediately → the exchange ran (routed to fate_conflict).
    assert result.commitment_pending is False
    assert result.exchange is not None
    names = [s.name for s in exporter.get_finished_spans()]
    assert "fate.action_resolved" in names  # the attacker's roll fired through dispatch
    assert "fate.exchange.resolved" in names
    assert enc.find_actor("Thug").withdrawn is True  # depleted target taken out
    assert enc.resolved is True


def test_non_fate_ruleset_is_rejected_loud():
    snap, enc = _solo_combat()
    native = get_ruleset_module("native")  # NOT a FateRulesetModule
    payload = FateActionPayload(request_id="r1", action="attack", skill="Fight", target="Thug")
    with pytest.raises(FateConflictError):
        dispatch_fate_action(
            payload=payload,
            actor_name="Hero",
            encounter=enc,
            ruleset=native,
            snapshot=snap,
            rng=_FixedRng(0),
        )


def test_unclosed_barrier_seals_and_pends():
    enc = StructuredEncounter(
        encounter_type="duel",
        category="combat",
        player_metric=EncounterMetric(name="p", threshold=10),
        opponent_metric=EncounterMetric(name="o", threshold=10),
        actors=[
            EncounterActor(name="Hero", role="lead", side="player"),
            EncounterActor(name="Ally", role="muscle", side="player"),
            EncounterActor(name="Thug", role="foe", side="opponent"),
        ],
    )
    snap = GameSnapshot(
        genre_slug="fate_test",
        characters=[_pc("Hero", {"Fight": 4}), _pc("Ally", {"Fight": 2})],
        encounter=enc,
    )
    snap.npcs.append(_depleted_thug())
    ruleset = get_ruleset_module("fate")
    payload = FateActionPayload(request_id="r1", action="attack", skill="Fight", target="Thug")
    exporter, tracer = _otel()

    result = dispatch_fate_action(
        payload=payload,
        actor_name="Hero",
        encounter=enc,
        ruleset=ruleset,
        snapshot=snap,
        rng=_FixedRng(0),
        _tracer=tracer,
    )

    assert result.commitment_pending is True  # Ally has not committed
    assert result.exchange is None
    assert enc.fate_commits[0].actor == "Hero"  # sealed, not resolved
    assert enc.find_actor("Thug").withdrawn is False
    names = [s.name for s in exporter.get_finished_spans()]
    assert "fate.action_resolved" in names  # the roll sealed even with the barrier open
    assert "fate.exchange.resolved" not in names  # barrier still open — exchange did not fire


def test_concede_routes_to_concession_not_the_ledger():
    enc = StructuredEncounter(
        encounter_type="duel",
        category="combat",
        player_metric=EncounterMetric(name="p", threshold=10),
        opponent_metric=EncounterMetric(name="o", threshold=10),
        actors=[EncounterActor(name="Hero", role="lead", side="player")],
    )
    hero = _pc("Hero", {"Fight": 2})
    hero.core.fate_sheet.fate_points = 1
    snap = GameSnapshot(genre_slug="fate_test", characters=[hero], encounter=enc)
    ruleset = get_ruleset_module("fate")
    payload = FateActionPayload(request_id="r1", action="concede", skill="")
    exporter, tracer = _otel()

    result = dispatch_fate_action(
        payload=payload,
        actor_name="Hero",
        encounter=enc,
        ruleset=ruleset,
        snapshot=snap,
        rng=_FixedRng(0),
        _tracer=tracer,
    )

    assert result.commitment_pending is False
    assert not enc.fate_commits  # concede never seals
    assert enc.find_actor("Hero").withdrawn is True
    assert hero.core.fate_sheet.fate_points == 2  # earned 1 (no consequences yet)
    assert "fate.conceded" in [s.name for s in exporter.get_finished_spans()]


def test_invoke_aspect_applies_bonus_and_emits_span():
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
    # Mirror the F1b economy idiom: an invokable aspect with one free invocation.
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
    hero = Character(core=core, char_class="Agent", race="Human", backstory="b")
    snap = GameSnapshot(genre_slug="fate_test", characters=[hero], encounter=enc)
    snap.npcs.append(_depleted_thug())
    ruleset = get_ruleset_module("fate")
    payload = FateActionPayload(
        request_id="r1",
        action="attack",
        skill="Fight",
        target="Thug",
        invoke_aspect="High Ground",
    )
    exporter, tracer = _otel()

    result = dispatch_fate_action(
        payload=payload,
        actor_name="Hero",
        encounter=enc,
        ruleset=ruleset,
        snapshot=snap,
        rng=_FixedRng(0),
        _tracer=tracer,
    )

    names = [s.name for s in exporter.get_finished_spans()]
    assert "fate.aspect.invoked" in names  # the pre-roll invoke fired
    # +2 sourced from the free invoke, not a fate point: free invoke consumed, points unchanged.
    assert hero.core.fate_sheet.aspects[0].free_invokes == 0
    assert hero.core.fate_sheet.fate_points == 2
    assert result.commitment_pending is False  # solo barrier → exchange resolved
