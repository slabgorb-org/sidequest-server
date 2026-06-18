"""Confrontation classification-result span after decompose.

sq-playtest 2026-06-07 (standoff seat seam, five_points-2 turn 5): an armed
brace matching the standoff def's own intent_verbs yielded
``confrontation=None`` with ZERO telemetry on the decline — silence
indistinguishable from "feature doesn't exist". The pass now emits
``intent_router.confrontation_classified`` whenever the action lexically
matches an authored intent_verb OR a confrontation dispatch was emitted;
``emitted=0`` with non-empty ``verb_hits`` is the unrouted shape (plus a
DEBUG log — Story 126-6 downgraded it from WARNING; a correct suppression
is not a warning, and the span already carries the GM-panel signal). Quiet
turns (no hit, no dispatch) stay span-free.

Twin of ``test_intent_router_witnessed_act_classified.py`` — same harness.
"""

from __future__ import annotations

import logging

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.session import GameSnapshot
from sidequest.protocol.dispatch import (
    DispatchPackage,
    PlayerDispatch,
    SubsystemDispatch,
)
from sidequest.server.intent_router_pass import execute_intent_router_pre_narrator_pass


class _FakeConfrontationDef:
    def __init__(self, confrontation_type: str, intent_verbs: list[str]):
        self.confrontation_type = confrontation_type
        self.category = "pre_combat"
        self.intent_verbs = intent_verbs


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


def _snapshot() -> GameSnapshot:
    snap = GameSnapshot(world_slug="five_points")
    snap.genre_slug = "spaghetti_western"
    return snap


def _standoff_pack() -> _FakePack:
    return _FakePack(
        [
            _FakeConfrontationDef(
                "standoff",
                ["draw", "stare", "threaten", "intimidate", "square", "confront"],
            )
        ]
    )


def _package_with_confrontation() -> DispatchPackage:
    return DispatchPackage(
        turn_id="t1",
        per_player=[
            PlayerDispatch(
                player_id="Zanzibar",
                raw_action="I square up to the deacon",
                dispatch=[
                    SubsystemDispatch(
                        subsystem="confrontation",
                        params={"type": "standoff", "opponent": "the deacon"},
                        idempotency_key="cf-1",
                        visibility={"visible_to": "all"},
                        confidence=0.9,
                    )
                ],
            )
        ],
        cross_player=[],
        confidence_global=0.9,
    )


def _empty_package() -> DispatchPackage:
    return DispatchPackage(turn_id="t2", per_player=[], cross_player=[], confidence_global=0.5)


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


def _classified_spans(exporter):
    return [
        s
        for s in exporter.get_finished_spans()
        if s.name == "intent_router.confrontation_classified"
    ]


@pytest.mark.asyncio
async def test_span_fires_with_emitted_type_when_router_dispatches(otel_capture):
    router = _StubRouter(_package_with_confrontation())
    await execute_intent_router_pre_narrator_pass(
        intent_router=router,
        snapshot=_snapshot(),
        pack=_standoff_pack(),
        action="I square up to the deacon and wait",
        player_name="Zanzibar",
    )
    spans = _classified_spans(otel_capture)
    assert len(spans) == 1
    attrs = spans[0].attributes or {}
    assert attrs["emitted"] == 1
    assert attrs["types"] == "standoff"
    assert "standoff:square" in attrs["verb_hits"]
    assert attrs["genre_slug"] == "spaghetti_western"


@pytest.mark.asyncio
async def test_verb_hit_with_no_dispatch_logs_at_debug_not_warning(otel_capture, caplog):
    """The turn-5 decline: action matches authored verbs, router emits no
    confrontation dispatch → span fires with emitted=0 + verb_hits, and the
    unrouted-verb log names the verbs at DEBUG (Story 126-6: a correct
    suppression is not a WARNING — the span carries the GM-panel signal)."""
    router = _StubRouter(_empty_package())
    with caplog.at_level(logging.DEBUG):
        await execute_intent_router_pre_narrator_pass(
            intent_router=router,
            snapshot=_snapshot(),
            pack=_standoff_pack(),
            action="I intimidate the heavy man in broadcloth, hand on the Colt.",
            player_name="Zanzibar",
        )
    spans = _classified_spans(otel_capture)
    assert len(spans) == 1
    attrs = spans[0].attributes or {}
    assert attrs["emitted"] == 0
    assert "standoff:intimidate" in attrs["verb_hits"]
    unrouted = [r for r in caplog.records if "confrontation_verb_unrouted" in r.getMessage()]
    assert unrouted, "the decline must still log, naming the unrouted verbs"
    assert all(r.levelno == logging.DEBUG for r in unrouted), (
        "a correct suppression must log at DEBUG, not WARNING (Story 126-6)"
    )


@pytest.mark.asyncio
async def test_quiet_turn_emits_no_span(otel_capture):
    """No verb hit, no dispatch — the span stays silent (no per-turn noise)."""
    router = _StubRouter(_empty_package())
    await execute_intent_router_pre_narrator_pass(
        intent_router=router,
        snapshot=_snapshot(),
        pack=_standoff_pack(),
        action="I order a whiskey and watch the room.",
        player_name="Zanzibar",
    )
    assert _classified_spans(otel_capture) == []


@pytest.mark.asyncio
async def test_verb_match_is_word_boundary(otel_capture):
    """'withdraw' must not hit the authored verb 'draw'."""
    router = _StubRouter(_empty_package())
    await execute_intent_router_pre_narrator_pass(
        intent_router=router,
        snapshot=_snapshot(),
        pack=_standoff_pack(),
        action="I withdraw to the boarding house and sleep.",
        player_name="Zanzibar",
    )
    assert _classified_spans(otel_capture) == []
