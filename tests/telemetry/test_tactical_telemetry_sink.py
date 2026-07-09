"""Task 6 (ADR-096 v2, Track C2) — tactical.* spans must MIRROR into the
turn_telemetry sink (``publish_event``), not merely open a Jaeger span.

The GM panel is the lie detector (CLAUDE.md OTEL Observability Principle): a
grid adjudication that reaches only Jaeger reads as DEAD in saves. Every tactical
span must flow through ``publish_event`` the way movement spans do (the
2026-06-22 root cause behind 8 "why is movement DEAD" saves). Model:
``tests/telemetry/test_movement_telemetry_sink.py``.

RED until ``sidequest/telemetry/spans/tactical.py`` exists AND is wired into the
``sidequest.telemetry.spans`` package via ``from .tactical import *`` — the
module in isolation is not enough (Verify Wiring, Not Just Existence).

NOTE (plan-doc bug corrected): the mirror helper skips a NonRecordingSpan
(``hasattr(span, "attributes")`` is False under the SDK default no-op tracer),
so these tests install a real recording ``TracerProvider`` via ``capture_spans``.
The plan's Task-6 snippet omitted that fixture and would never fire the mirror.
"""

from __future__ import annotations

import pytest
import sidequest.telemetry.spans.tactical as tac
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import sidequest.telemetry.spans as spans_module


@pytest.fixture
def capture_spans(monkeypatch):
    """Install a recording tracer so ``Span.open`` yields a span with
    ``attributes`` — the mirror helper needs a recording span to fire."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    local = provider.get_tracer("test-tactical-telemetry-sink")
    monkeypatch.setattr(spans_module, "tracer", lambda: local)
    return exporter


def test_move_validated_mirrors_to_sink(monkeypatch, capture_spans):
    published: list[tuple] = []
    monkeypatch.setattr(
        tac, "publish_event", lambda et, fields, **kw: published.append((et, fields, kw))
    )
    with tac.tactical_move_validated_span(
        actor="Rux", cells_spent=2, cells_budget=6, from_cell=(1, 1), to_cell=(3, 1)
    ):
        pass
    assert published, "tactical.move.validated did not mirror into the turn_telemetry sink"
    event_type, fields, kw = published[0]
    assert event_type == "state_transition"
    assert kw["component"] == "tactical"
    assert fields["op"] == "tactical.move.validated"
    assert fields["cells_spent"] == 2
    assert fields["cells_budget"] == 6


def test_move_denied_mirrors_and_carries_reason(monkeypatch, capture_spans):
    published: list[tuple] = []
    monkeypatch.setattr(
        tac, "publish_event", lambda et, fields, **kw: published.append((et, fields, kw))
    )
    with tac.tactical_move_denied_span(
        actor="Rux",
        cells_spent=5,
        cells_budget=1,
        reason="that move is 5 cells; you can move 1 cell",
    ):
        pass
    assert published, "tactical.move.denied did not mirror to the sink"
    _, fields, _ = published[0]
    assert fields["op"] == "tactical.move.denied"
    assert "5 cells" in fields["reason"]


def test_aoe_cells_mirrors_with_count(monkeypatch, capture_spans):
    published: list[tuple] = []
    monkeypatch.setattr(
        tac, "publish_event", lambda et, fields, **kw: published.append((et, fields, kw))
    )
    with tac.tactical_aoe_cells_span(actor="Rux", template="burst", cell_count=5, radius=1):
        pass
    assert published, "tactical.aoe.cells did not mirror to the sink"
    _, fields, _ = published[0]
    assert fields["op"] == "tactical.aoe.cells"
    assert fields["cell_count"] == 5


def test_enforcement_skipped_mirrors_with_reason(monkeypatch, capture_spans):
    """The no-grid skip is a DELIBERATE boundary, not a silent fallback — the GM
    panel must see it fire with its reason so 'the gate did nothing' is legible."""
    published: list[tuple] = []
    monkeypatch.setattr(
        tac, "publish_event", lambda et, fields, **kw: published.append((et, fields, kw))
    )
    with tac.tactical_enforcement_skipped_span(actor="Rux", reason="no_grid"):
        pass
    assert published, "tactical.enforcement.skipped did not mirror to the sink"
    _, fields, _ = published[0]
    assert fields["op"] == "tactical.enforcement.skipped"
    assert fields["reason"] == "no_grid"


def test_positions_seated_mirrors_with_count_and_room(monkeypatch, capture_spans):
    published: list[tuple] = []
    monkeypatch.setattr(
        tac, "publish_event", lambda et, fields, **kw: published.append((et, fields, kw))
    )
    with tac.tactical_positions_seated_span(seated_count=3, room_id="beneath_sunden.r2"):
        pass
    assert published, "tactical.positions.seated did not mirror to the sink"
    _, fields, _ = published[0]
    assert fields["op"] == "tactical.positions.seated"
    assert fields["seated_count"] == 3
    assert fields["room_id"] == "beneath_sunden.r2"


def test_every_tactical_span_is_routed():
    """Each tactical span constant must have a SPAN_ROUTES entry, or
    tests/telemetry/test_routing_completeness.py fails (the translator would emit
    only agent_span_close and the GM-panel typed tabs miss the subsystem)."""
    from sidequest.telemetry.spans import SPAN_ROUTES

    for name in (
        tac.SPAN_TACTICAL_MOVE_VALIDATED,
        tac.SPAN_TACTICAL_MOVE_DENIED,
        tac.SPAN_TACTICAL_AOE_CELLS,
        tac.SPAN_TACTICAL_ENFORCEMENT_SKIPPED,
        tac.SPAN_TACTICAL_POSITIONS_SEATED,
    ):
        assert name in SPAN_ROUTES, f"{name} missing a SPAN_ROUTES entry"


def test_tactical_spans_wired_into_package_namespace():
    """WIRING (not just existence): the constants must be reachable from the
    ``sidequest.telemetry.spans`` PACKAGE — proving ``from .tactical import *``
    was added to spans/__init__.py, which is also how the routing-completeness
    lint discovers them. A module that exists but isn't re-exported is half-wired."""
    assert hasattr(spans_module, "SPAN_TACTICAL_MOVE_DENIED")
    assert hasattr(spans_module, "SPAN_TACTICAL_POSITIONS_SEATED")
    assert spans_module.SPAN_TACTICAL_MOVE_DENIED == "tactical.move.denied"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v", "-n0"]))
