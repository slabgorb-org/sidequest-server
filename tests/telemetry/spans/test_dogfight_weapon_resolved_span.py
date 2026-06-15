"""Story 114-15 — the ``dogfight.weapon_resolved`` OTEL span (AC5).

The dogfight resolves its ship weapon from the genre-tier ``ship_weapons``
collection; this span is the GM-panel lie-detector that proves the dogfight used a
REAL ship weapon (with its armor_piercing) rather than Claude improvising one.
Mirrors ``tests/telemetry/spans/test_dogfight_shot_spans.py``.
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.telemetry.spans import FLAT_ONLY_SPANS, SPAN_ROUTES
from sidequest.telemetry.spans.dogfight import (
    SPAN_DOGFIGHT_WEAPON_RESOLVED,
    dogfight_weapon_resolved_span,
)


@pytest.fixture
def exporter(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    from sidequest.telemetry import spans as spans_module

    exp = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    test_tracer = provider.get_tracer("test")
    monkeypatch.setattr(spans_module, "tracer", lambda: test_tracer)
    return exp


def test_weapon_resolved_span_is_routed() -> None:
    """The span is routed as a typed state_transition under component=dogfight.

    Membership in ``FLAT_ONLY_SPANS`` would mean the GM panel never sees a typed
    event for it — wrong for a lie-detector surface.
    """
    assert SPAN_DOGFIGHT_WEAPON_RESOLVED == "dogfight.weapon_resolved"
    assert SPAN_DOGFIGHT_WEAPON_RESOLVED in SPAN_ROUTES, "span missing from SPAN_ROUTES"
    assert SPAN_DOGFIGHT_WEAPON_RESOLVED not in FLAT_ONLY_SPANS
    route = SPAN_ROUTES[SPAN_DOGFIGHT_WEAPON_RESOLVED]
    assert route.component == "dogfight"
    assert route.event_type == "state_transition"


def test_weapon_resolved_span_carries_source_id_and_ap(exporter: InMemorySpanExporter) -> None:
    with dogfight_weapon_resolved_span(
        source="ship_weapons",
        weapon_id="multifocal_laser",
        armor_piercing=20,
        dice="1d4",
    ):
        pass

    [span] = exporter.get_finished_spans()
    assert span.name == "dogfight.weapon_resolved"
    attrs = span.attributes or {}
    assert attrs.get("source") == "ship_weapons"
    assert attrs.get("weapon_id") == "multifocal_laser"
    assert attrs.get("armor_piercing") == 20


def test_weapon_resolved_route_extract_projects_panel_fields() -> None:
    """The SPAN_ROUTES extractor must surface source/weapon_id/armor_piercing to the
    GM panel — assert the projection, not just registration (a route that drops the
    AP field would render a hollow panel row)."""
    route = SPAN_ROUTES[SPAN_DOGFIGHT_WEAPON_RESOLVED]

    class _FakeSpan:
        attributes = {
            "source": "ship_weapons",
            "weapon_id": "multifocal_laser",
            "armor_piercing": 20,
        }

    fields = route.extract(_FakeSpan())
    assert fields["source"] == "ship_weapons"
    assert fields["weapon_id"] == "multifocal_laser"
    assert fields["armor_piercing"] == 20
