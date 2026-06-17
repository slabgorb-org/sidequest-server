"""Story 108-6 (RED) — dying-window OTEL spans + the #846 dark-span fold.

The GM panel is the lie-detector: the WWN dying window must emit three
slug-namespaced spans so the panel can prove the engine drove the clock rather
than the narrator improvising a countdown:

  - ``{slug}.dying_window.opened``  — the window opened (vs instant terminal)
  - ``{slug}.dying_window.tick``    — one engine-owned round elapsed
  - ``{slug}.dying_window.resolved``— window exit (stabilized | died)

Plus the folded #846 fix: ``mortal_injury_declared_span`` already FORWARDS
``superseded_by_terminal`` via ``**attrs``, but ``_mortal_injury_declared_extract``
DROPS it from the GM-panel projection — a known-dark span. The supersede
decision must reach the panel.

Span pattern mirrors test_142_wn_lethality_spans.py: local InMemorySpanExporter,
pass ``_tracer=`` so spans land locally. Route assertions hit the SPAN_ROUTES
registry directly (the GM-panel projection contract).
"""

from __future__ import annotations

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


def _exporter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


def _one(exporter, name):
    spans = [s for s in exporter.get_finished_spans() if s.name == name]
    assert len(spans) == 1, (
        f"expected exactly one {name}; got {[s.name for s in exporter.get_finished_spans()]}"
    )
    return dict(spans[0].attributes or {})


def test_dying_window_routes_registered_for_lethality_slugs():
    from sidequest.telemetry.spans.wn import SPAN_ROUTES

    for slug in ("wwn", "cwn", "awn"):
        for event in ("dying_window.opened", "dying_window.tick", "dying_window.resolved"):
            assert f"{slug}.{event}" in SPAN_ROUTES, f"missing route {slug}.{event}"


def test_opened_span_carries_reason_and_deadline():
    from sidequest.telemetry.spans.wn import dying_window_opened_span

    exporter, tracer = _exporter()
    dying_window_opened_span(
        ruleset="wwn",
        actor="Rux",
        created_turn=4,
        mortal_injury_rounds=6,
        deadline_round=10,
        reason="no_live_hostile",
        _tracer=tracer,
    )
    attrs = _one(exporter, "wwn.dying_window.opened")
    assert attrs["actor"] == "Rux"
    assert attrs["deadline_round"] == 10
    assert attrs["reason"] == "no_live_hostile"


def test_tick_span_carries_derived_rounds_and_stabilization_flag():
    from sidequest.telemetry.spans.wn import dying_window_tick_span

    exporter, tracer = _exporter()
    dying_window_tick_span(
        ruleset="wwn",
        actor="Rux",
        rounds_elapsed=2,
        difficulty=10,
        action_was_stabilization=True,
        roll=14,
        success=True,
        _tracer=tracer,
    )
    attrs = _one(exporter, "wwn.dying_window.tick")
    assert attrs["rounds_elapsed"] == 2
    assert attrs["difficulty"] == 10
    assert attrs["action_was_stabilization"] is True


def test_resolved_span_carries_outcome():
    from sidequest.telemetry.spans.wn import dying_window_resolved_span

    exporter, tracer = _exporter()
    dying_window_resolved_span(
        ruleset="wwn",
        actor="Rux",
        outcome="died",
        final_rounds_elapsed=6,
        resulting_status="terminal-dead",
        _tracer=tracer,
    )
    attrs = _one(exporter, "wwn.dying_window.resolved")
    assert attrs["outcome"] == "died"
    assert attrs["final_rounds_elapsed"] == 6


def test_mortal_injury_extract_surfaces_superseded_by_terminal():
    """#846 fold: the supersede decision must reach the GM-panel projection."""
    from sidequest.telemetry.spans.wn import SPAN_ROUTES

    route = SPAN_ROUTES["wwn.mortal_injury.declared"]

    class _Span:
        attributes = {"actor": "Rux", "rounds_to_die": 6, "superseded_by_terminal": True}

    projected = route.extract(_Span())
    assert projected["superseded_by_terminal"] is True
