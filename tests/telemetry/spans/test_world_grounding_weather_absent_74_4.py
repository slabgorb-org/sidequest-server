"""Story 74-4 AC2 — ``world_grounding.weather_absent`` OTEL span (helper + route).

Companion to the existing ``weather_proposed`` / ``weather_used`` spans
(``sidequest/telemetry/spans/world_grounding.py``). Today, when the narrator
asks for weather but the session has none wired, NOTHING fires — the GM panel
cannot distinguish "this world has no weather **by design**" from "the weather
subsystem broke". Per the **OTEL Observability Principle**, that decision needs
its own state_transition span so the lie detector stays honest.

This file is the helper + routing RED gate (mirrors
``test_world_grounding_spans.py``). The production-seam wiring proof lives in
``tests/agents/tools/test_grounding_weather_absent_wiring_74_4.py``.
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)


@pytest.fixture
def exporter(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    """Per-test in-memory exporter installed on the shared spans module.

    Mirrors ``test_world_grounding_spans.py``: ``Span.open`` reads the tracer
    callable from ``sidequest.telemetry.spans.tracer`` at span-open time, so we
    monkeypatch that function to scope emission to this test.
    """
    from sidequest.telemetry import spans as spans_module

    exp = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    test_tracer = provider.get_tracer("test")
    monkeypatch.setattr(spans_module, "tracer", lambda: test_tracer)
    return exp


# --------------------------------------------------------------------------- #
# Constant + route registration
# --------------------------------------------------------------------------- #


def test_weather_absent_constant_has_canonical_name() -> None:
    """The watcher translator and the GM dashboard key off this literal string;
    pin it so a rename forces a synchronized dashboard update."""
    from sidequest.telemetry.spans import SPAN_WORLD_GROUNDING_WEATHER_ABSENT

    assert SPAN_WORLD_GROUNDING_WEATHER_ABSENT == "world_grounding.weather_absent"


def test_weather_absent_span_is_routed_as_state_transition() -> None:
    """The new constant must be ROUTED (typed event), not flat-only — a
    flat-only span never reaches the GM panel's world-grounding tab, which
    would defeat the entire point of the absence signal. Also satisfies the
    routing-completeness invariant (every SPAN_* must be in exactly one set).
    """
    from sidequest.telemetry.spans import (
        FLAT_ONLY_SPANS,
        SPAN_ROUTES,
        SPAN_WORLD_GROUNDING_WEATHER_ABSENT,
    )

    assert SPAN_WORLD_GROUNDING_WEATHER_ABSENT in SPAN_ROUTES, (
        "weather_absent missing from SPAN_ROUTES"
    )
    assert SPAN_WORLD_GROUNDING_WEATHER_ABSENT not in FLAT_ONLY_SPANS, (
        "weather_absent must not be flat-only — the GM dashboard needs the typed event"
    )
    route = SPAN_ROUTES[SPAN_WORLD_GROUNDING_WEATHER_ABSENT]
    assert route.component == "world_grounding"
    assert route.event_type == "state_transition"


# --------------------------------------------------------------------------- #
# Helper
# --------------------------------------------------------------------------- #


def test_weather_absent_helper_records_world_and_perspective(
    exporter: InMemorySpanExporter,
) -> None:
    """The absence helper fires a span carrying the world id and perspective.

    There is no WeatherState to record (that is the whole point), so the span
    is identity-only: which world, whose viewpoint. ``world_id`` is always
    present — a session has a world by definition.
    """
    from sidequest.telemetry.spans import emit_weather_absent_span

    emit_weather_absent_span(world_id="glenross", perspective_pc="Alex")

    [span] = exporter.get_finished_spans()
    assert span.name == "world_grounding.weather_absent"
    attrs = span.attributes or {}
    assert attrs["world_id"] == "glenross"
    assert attrs["perspective_pc"] == "Alex"


def test_weather_absent_helper_encodes_absent_perspective_explicitly(
    exporter: InMemorySpanExporter,
) -> None:
    """Single-player sessions pass ``perspective_pc=None``. Per CLAUDE.md "no
    silent fallbacks" the helper MUST encode that as the empty string, never
    omit the attribute, so the dashboard's column-presence check is reliable.
    """
    from sidequest.telemetry.spans import emit_weather_absent_span

    emit_weather_absent_span(world_id="glenross", perspective_pc=None)

    [span] = exporter.get_finished_spans()
    attrs = span.attributes or {}
    assert attrs["perspective_pc"] == ""


# --------------------------------------------------------------------------- #
# Route extractor — columns the watcher translator surfaces to the UI
# --------------------------------------------------------------------------- #


def test_weather_absent_route_extract_exposes_columns(
    exporter: InMemorySpanExporter,
) -> None:
    """The route extractor must surface ``field``/``op``/``world_id`` so the
    dashboard renders the absence row in the same world-grounding tab as
    proposed/used, marked as ``op="absent"``."""
    from sidequest.telemetry.spans import SPAN_ROUTES, emit_weather_absent_span

    emit_weather_absent_span(world_id="glenross", perspective_pc="Alex")

    [span] = exporter.get_finished_spans()
    fields = SPAN_ROUTES["world_grounding.weather_absent"].extract(span)
    assert fields["field"] == "weather"
    assert fields["op"] == "absent"
    assert fields["world_id"] == "glenross"
