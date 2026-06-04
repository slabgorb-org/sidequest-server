"""RED — Story 85-3: the stakes wiring emits a ``confrontation.stakes_attached``
OTEL span so the GM panel can verify the field actually populated on the
CONFRONTATION channel (CLAUDE.md OTEL Observability Principle — the GM panel is
the lie detector; a subsystem that doesn't emit a span can't be distinguished
from one that's silently broken).

Architect contract (The White Queen, 2026-06-04): emit ONE new span per
``build_confrontation_payload`` call —
  span name: ``confrontation.stakes_attached``
  attributes: ``genre_slug``, ``confrontation_type`` (the encounter type),
              ``has_stakes`` (bool), ``stakes_len`` (int, 0 when absent).

Portrait resolution reuses the EXISTING scrapbook portrait spans, so no new
span is asserted for portraits here.

Mirrors the in-memory exporter fixture in
``test_confrontation_panel_projection_span.py``.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.genre.models.rules import BeatDef, ConfrontationDef, MetricDef
from sidequest.server.dispatch.confrontation import build_confrontation_payload

_SPAN_NAME = "confrontation.stakes_attached"
_STAKES = "Shake the cruisers or lose the cargo"


@pytest.fixture
def otel_capture() -> Iterator[InMemorySpanExporter]:
    """In-memory span exporter attached to the live TracerProvider."""
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


def _cdef() -> ConfrontationDef:
    return ConfrontationDef(
        type="chase",
        label="Highway Pursuit",
        category="movement",
        player_metric=MetricDef(name="separation", starting=0, threshold=10),
        opponent_metric=MetricDef(name="pursuit", starting=0, threshold=10),
        beats=[BeatDef(id="floor_it", label="Floor It", kind="push", base=2, stat_check="SPD")],
    )


def _enc() -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="chase",
        player_metric=EncounterMetric(name="separation", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="pursuit", current=0, starting=0, threshold=10),
        actors=[
            EncounterActor(name="Magpie", role="driver", side="player"),
            EncounterActor(name="Divvie Sergeant", role="pursuer", side="opponent"),
        ],
    )


def _stakes_spans(exporter: InMemorySpanExporter) -> list:
    return [s for s in exporter.get_finished_spans() if s.name == _SPAN_NAME]


def test_stakes_attached_span_fires_with_has_stakes_true(otel_capture: InMemorySpanExporter):
    build_confrontation_payload(
        encounter=_enc(),
        cdef=_cdef(),
        genre_slug="road_warrior",
        active_stakes=_STAKES,
    )
    spans = _stakes_spans(otel_capture)
    assert spans, (
        f"build_confrontation_payload must emit a {_SPAN_NAME!r} span so the GM panel can "
        f"confirm stakes attached to the confrontation channel"
    )
    attrs = dict(spans[-1].attributes or {})
    assert attrs.get("has_stakes") is True, f"has_stakes must be True; attrs={attrs!r}"
    assert attrs.get("stakes_len") == len(_STAKES), f"stakes_len wrong; attrs={attrs!r}"
    assert attrs.get("genre_slug") == "road_warrior"
    assert attrs.get("confrontation_type") == "chase"


def test_stakes_attached_span_fires_with_has_stakes_false_when_absent(
    otel_capture: InMemorySpanExporter,
):
    # The span fires on EVERY confrontation build — has_stakes=False is the
    # signal that a confrontation ran with no active stakes (not that the wiring
    # broke). Without this the GM panel can't tell "no stakes set" from "emit
    # dropped".
    build_confrontation_payload(encounter=_enc(), cdef=_cdef(), genre_slug="road_warrior")
    spans = _stakes_spans(otel_capture)
    assert spans, f"{_SPAN_NAME!r} span must fire even when there are no stakes"
    attrs = dict(spans[-1].attributes or {})
    assert attrs.get("has_stakes") is False, f"has_stakes must be False; attrs={attrs!r}"
    assert attrs.get("stakes_len") == 0, f"stakes_len must be 0 when absent; attrs={attrs!r}"
