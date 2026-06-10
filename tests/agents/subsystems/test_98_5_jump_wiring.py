"""Story 98-5 — wiring test: inter-system jump adjudication is reached from a
production path and emits its OTEL span (RED).

CLAUDE.md mandate: every subsystem needs a wiring test that proves the engine is
reachable from production, not merely unit-testable in isolation. The epic's
verification spine (§7) requires S2 to assert "jump adjudication is reached from
``movement.py``, not just unit-testable."

These tests drive the production *glue* — ``orbital/jump.adjudicate_inter_system_jump``
— which the movement seam invokes to resolve a route, adjudicate through the bound
ruleset, and emit ``jump.adjudicated``. The glue is the refactor-stable seam: the
assertions are on the OTEL span and on the ruleset *registry* call, never a
source-text grep.

NOTE (test design): the spec leaves the exact production entry point for an
inter-system jump open (movement dispatch vs. the region-mode narration path vs. a
new handler). These tests pin the span + bound-ruleset contract at the glue seam;
Dev must additionally invoke this glue from the live movement path so it fires in
real play. See the TEA Delivery Finding for 98-5.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import sidequest.telemetry.spans as spans_module
from sidequest.genre.models.world import CartographyConfig, Region, Route


@pytest.fixture
def capture_spans(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    local = provider.get_tracer("test-98-5-jump-wiring")
    monkeypatch.setattr(spans_module, "tracer", lambda: local)
    return exporter


def _spans_named(exporter: InMemorySpanExporter, name: str):
    return [s for s in exporter.get_finished_spans() if s.name == name]


def _carto(routes: list[Route]) -> CartographyConfig:
    return CartographyConfig(
        regions={
            "yula": Region(name="Yula", summary="s", description="d", adjacent=["basilica"]),
            "basilica": Region(name="Basilica", summary="s", description="d", adjacent=["yula"]),
        },
        routes=routes,
    )


def _authored_route() -> Route:
    return Route(
        name="Yula–Basilica Lane",
        description="A charted spike lane.",
        from_id="yula",
        to_id="basilica",
        jump_fuel=2,
        transit_days=6,
        hazard="ion_shoals",
        drive_rating_min=1,
    )


def test_inter_system_jump_production_glue_fires_adjudicated_span(capture_spans):
    """Driving the production jump glue for a real adjacency emits
    ``jump.adjudicated`` with the from/to systems — the GM panel sees the jump
    actually fired, not just narrator prose claiming a hop."""
    from sidequest.orbital.jump import adjudicate_inter_system_jump
    from sidequest.telemetry.spans.jump import SPAN_JUMP_ADJUDICATED

    adjudicate_inter_system_jump(
        cartography=_carto([_authored_route()]),
        from_region="yula",
        to_region="basilica",
        ruleset="swn",
        drive_rating=2,
        rng=random.Random(1),
    )

    spans = _spans_named(capture_spans, SPAN_JUMP_ADJUDICATED)
    assert len(spans) == 1
    assert spans[0].attributes["from_region"] == "yula"
    assert spans[0].attributes["to_region"] == "basilica"


def test_inter_system_jump_unrouted_edge_also_fires_default_cost_span(capture_spans):
    """An unrouted (but adjacent) jump still fires ``jump.adjudicated`` AND the
    explicit ``jump.default_cost`` span — No Silent Fallbacks at the production
    seam, not just in the ruleset unit."""
    from sidequest.orbital.jump import adjudicate_inter_system_jump
    from sidequest.telemetry.spans.jump import SPAN_JUMP_ADJUDICATED, SPAN_JUMP_DEFAULT_COST

    adjudicate_inter_system_jump(
        cartography=_carto([]),  # adjacency present, no routes entry
        from_region="yula",
        to_region="basilica",
        ruleset="swn",
        drive_rating=1,
        rng=random.Random(2),
    )

    assert len(_spans_named(capture_spans, SPAN_JUMP_ADJUDICATED)) == 1
    assert len(_spans_named(capture_spans, SPAN_JUMP_DEFAULT_COST)) == 1


def test_inter_system_jump_routes_through_bound_ruleset_not_hardcoded(capture_spans, monkeypatch):
    """AC1 wiring: the glue adjudicates through ``get_ruleset_module(ruleset)`` —
    the bound module — not a hard-coded SWN import. Proven by swapping the
    registry for a spy and asserting the glue used it."""
    import sidequest.orbital.jump as jump_mod
    from sidequest.game.ruleset.resolution import JumpAdjudication

    @dataclass
    class _SpyModule:
        slug: str = "swn"
        calls: int = 0

        def adjudicate_jump(self, *, route, drive_rating, rng) -> JumpAdjudication:
            object.__setattr__(self, "calls", self.calls + 1)
            return JumpAdjudication(
                fuel_spent=2,
                transit_days=6,
                hazard="ion_shoals",
                hazard_roll=3,
                source="route",
            )

    spy = _SpyModule()
    monkeypatch.setattr(jump_mod, "get_ruleset_module", lambda slug: spy)

    jump_mod.adjudicate_inter_system_jump(
        cartography=_carto([_authored_route()]),
        from_region="yula",
        to_region="basilica",
        ruleset="swn",
        drive_rating=2,
        rng=random.Random(1),
    )

    assert spy.calls == 1, "glue must adjudicate via the bound ruleset module, not a hardcode"
