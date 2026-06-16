"""OTEL spans for dogfight shot resolution (ADR-077).

Tests for ``dogfight_shot_attempted_span`` and ``dogfight_shot_damage_span``.
These are the lie-detector spans that prove dice fired and weren't improvised.
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.telemetry.spans import FLAT_ONLY_SPANS, SPAN_ROUTES
from sidequest.telemetry.spans.dogfight import (
    SPAN_DOGFIGHT_SHOT_ATTEMPTED,
    SPAN_DOGFIGHT_SHOT_DAMAGE,
    dogfight_shot_attempted_span,
    dogfight_shot_damage_span,
)


@pytest.fixture
def exporter(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    """Install a per-test in-memory exporter. Mirrors the pattern in
    ``tests/telemetry/spans/test_location_spans.py`` — monkeypatching
    ``sidequest.telemetry.spans.tracer`` keeps the global tracer provider
    untouched."""
    from sidequest.telemetry import spans as spans_module

    exp = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    test_tracer = provider.get_tracer("test")
    monkeypatch.setattr(spans_module, "tracer", lambda: test_tracer)
    return exp


def test_both_shot_spans_are_routed() -> None:
    """Both new shot spans are routed as state_transition under component=dogfight.

    Membership in ``FLAT_ONLY_SPANS`` would mean the GM panel never sees a typed
    event for the span — wrong for the lie-detector surface. This guards against
    a future edit silently mis-bucketing the dashboard events.
    """
    for span_name in (SPAN_DOGFIGHT_SHOT_ATTEMPTED, SPAN_DOGFIGHT_SHOT_DAMAGE):
        assert span_name in SPAN_ROUTES, f"{span_name} missing from SPAN_ROUTES"
        assert span_name not in FLAT_ONLY_SPANS
        route = SPAN_ROUTES[span_name]
        assert route.component == "dogfight"
        assert route.event_type == "state_transition"


def test_shot_attempted_span_carries_roll(exporter: InMemorySpanExporter) -> None:
    with dogfight_shot_attempted_span(
        shooter="Red Baron",
        target="player",
        d20_total=18,
        target_ac=16,
        hit=True,
        geometry_modifier=4,
        source="npc",
    ):
        pass

    [span] = exporter.get_finished_spans()
    assert span.name == "dogfight.shot_attempted"
    attrs = span.attributes or {}
    assert attrs["d20_total"] == 18
    assert attrs["hit"] is True
    assert attrs["source"] == "npc"


def test_shot_damage_span_carries_ablation(exporter: InMemorySpanExporter) -> None:
    with dogfight_shot_damage_span(
        shooter="Red Baron",
        target="player",
        dice="1d4",
        armor_piercing=20,
        armor_negated=5,
        applied=3,
        target_hp_after=5,
    ):
        pass

    [span] = exporter.get_finished_spans()
    assert span.name == "dogfight.shot_damage"
    attrs = span.attributes or {}
    assert attrs["applied"] == 3
    assert attrs["target_hp_after"] == 5
