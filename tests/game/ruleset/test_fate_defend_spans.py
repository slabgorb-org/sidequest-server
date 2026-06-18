"""OTEL for the DEFEND barrier (spec 2026-06-18 §9, story 126-8): a ``role``
attribute on fate.action_resolved and the new fate.defend_phase span — the
GM-panel lie detector that the barrier actually fired (and a player defense came
from the client, not narrator improvisation).

Run serially: ``uv run pytest tests/game/ruleset/test_fate_defend_spans.py -n0 -q``
(the parallel runner has a known span-count deadlock on telemetry files).

RED: fate_defend_phase_span / SPAN_ROUTES["fate.defend_phase"] do not exist, and
fate_action_resolved_span has no ``role`` param/attribute yet (plan Task 3).
"""

from __future__ import annotations

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.telemetry.spans._core import SPAN_ROUTES
from sidequest.telemetry.spans.fate import (
    fate_action_resolved_span,
    fate_defend_phase_span,
)


def _exporter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


def test_action_resolved_defaults_role_action():
    # No role passed → the span must still carry role="action" (the default),
    # which only exists once the param is added — proves the param, not **attrs.
    exporter, tracer = _exporter()
    fate_action_resolved_span(
        actor="Rux",
        skill_rating=2,
        dice=(0, 0, 0, 0),
        ladder_total=2,
        opposition=0,
        opposition_kind="active",
        shifts=2,
        tier="Succeed",
        source="player_thrown",
        _tracer=tracer,
    )
    span = exporter.get_finished_spans()[0]
    assert span.attributes["role"] == "action"
    assert span.attributes["source"] == "player_thrown"


def test_action_resolved_role_defense():
    exporter, tracer = _exporter()
    fate_action_resolved_span(
        actor="Rux",
        skill_rating=3,
        dice=(1, 0, 0, 0),
        ladder_total=4,
        opposition=0,
        opposition_kind="active",
        shifts=0,
        tier="Tie",
        role="defense",
        source="player_thrown",
        _tracer=tracer,
    )
    span = exporter.get_finished_spans()[0]
    assert span.attributes["role"] == "defense"
    assert span.attributes["source"] == "player_thrown"


def test_defend_phase_route_registered():
    route = SPAN_ROUTES["fate.defend_phase"]
    assert route.component == "fate"
    assert route.event_type == "state_transition"


def test_defend_phase_emitter_fires_named_span():
    exporter, tracer = _exporter()
    fate_defend_phase_span(
        defender="Rux",
        attacker="Bandit",
        request_id="d1",
        responded=True,
        conceded=False,
        _tracer=tracer,
    )
    spans = exporter.get_finished_spans()
    assert [s.name for s in spans] == ["fate.defend_phase"]
    assert spans[0].attributes["responded"] is True
    assert spans[0].attributes["defender"] == "Rux"
    assert spans[0].attributes["attacker"] == "Bandit"
    assert spans[0].attributes["request_id"] == "d1"


def test_defend_phase_request_time_unanswered():
    # At request time the barrier is open: responded=False (the GM panel can see
    # we asked Rux to defend and are waiting).
    exporter, tracer = _exporter()
    fate_defend_phase_span(
        defender="Rux",
        attacker="Bandit",
        request_id="d1",
        responded=False,
        _tracer=tracer,
    )
    span = exporter.get_finished_spans()[0]
    assert span.attributes["responded"] is False
    assert span.attributes["conceded"] is False
