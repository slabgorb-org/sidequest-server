"""Story 98-5 — inter-system jump adjudication via the SWN ruleset seam (RED).

ADR-141 layers a campaign-scale **jump** onto the cartography graph: moving from
one star system to an adjacent one costs fuel / transit days / hazard, adjudicated
through the *bound ruleset* (ADR-117 space_opera→SWN), NOT a hard-coded formula in
movement.py. An ``adjacent`` edge with no authored ``routes`` entry is a navigable
edge with an **explicit, OTEL-logged** ruleset default (No Silent Fallbacks) — never
a silent zero or a dropped edge.

CONTENT-FREE: every test builds synthetic ``Route`` / ``Region`` / ``CartographyConfig``
objects and drives the seam directly (``feedback_no_content_coupled_tests``). Spans
are captured via an in-memory OTEL exporter monkeypatched onto ``spans.tracer``
(drive-and-assert, never a source-text grep).

These tests are RED: the jump seam (``adjudicate_jump``, ``JumpAdjudication``, the
``jump`` span module, ``orbital/jump.py``) and the typed ``Route`` jump fields do not
exist yet. Dev (GREEN) makes them pass.
"""

from __future__ import annotations

import logging
import random

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import sidequest.telemetry.spans as spans_module
from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.ruleset.native import NativeRulesetModule
from sidequest.genre.models.world import CartographyConfig, Region, Route

# ---------------------------------------------------------------------------
# Span capture (mirrors tests/agents/subsystems/test_movement_dispatch.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def capture_spans(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    local = provider.get_tracer("test-98-5-jump")
    monkeypatch.setattr(spans_module, "tracer", lambda: local)
    return exporter


def _spans_named(exporter: InMemorySpanExporter, name: str):
    return [s for s in exporter.get_finished_spans() if s.name == name]


# ---------------------------------------------------------------------------
# Synthetic cartography helpers
# ---------------------------------------------------------------------------


def _region(name: str, adjacent: list[str]) -> Region:
    return Region(
        name=name, summary=f"{name} summary", description=f"{name} desc", adjacent=adjacent
    )


def _yula_basilica_carto(routes: list[Route]) -> CartographyConfig:
    """A two-system graph: ``yula`` ↔ ``basilica`` adjacent, plus an isolated
    ``erebus`` adjacent to yula but unrouted. Routes are supplied per-test."""
    return CartographyConfig(
        regions={
            "yula": _region("Yula", ["basilica", "erebus"]),
            "basilica": _region("Basilica", ["yula"]),
            "erebus": _region("Erebus", ["yula"]),
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


# ---------------------------------------------------------------------------
# AC1 — jump adjudicated THROUGH the bound ruleset, reading routes fields
# ---------------------------------------------------------------------------


def test_swn_adjudicate_jump_reads_authored_route_via_bound_module():
    """AC1: with an authored ``routes`` entry, the adjudicated cost reflects the
    authored fields, and it is resolved through the *bound* ruleset module
    (``get_ruleset_module('swn')``) — proving it flows through the ADR-117 seam,
    not a movement.py hard-code."""
    swn = get_ruleset_module("swn")  # the space_opera→SWN binding, not a hardcode
    result = swn.adjudicate_jump(route=_authored_route(), drive_rating=2, rng=random.Random(1))

    assert result.fuel_spent == 2
    assert result.transit_days == 6
    assert result.hazard == "ion_shoals"
    assert result.source == "route"


def test_swn_underrated_drive_makes_strained_jump_costs_extra_fuel():
    """AC1 / drive_rating_min semantics (Dev decision, TEA finding #2): a ship
    whose drive rating is BELOW the route's ``drive_rating_min`` still makes the
    jump (a bare adjacency is always navigable) but burns one extra fuel load —
    a mechanical cost, never a block (No Silent Fallbacks)."""
    swn = get_ruleset_module("swn")
    route = _authored_route()  # jump_fuel=2, drive_rating_min=1
    unstrained = swn.adjudicate_jump(route=route, drive_rating=1, rng=random.Random(1))
    assert unstrained.fuel_spent == 2  # drive_rating == min: no penalty

    strained_route = Route(
        name="High-Threshold Lane",
        description="Needs a strong drive.",
        from_id="yula",
        to_id="basilica",
        jump_fuel=2,
        drive_rating_min=3,
    )
    strained = swn.adjudicate_jump(route=strained_route, drive_rating=1, rng=random.Random(1))
    assert strained.fuel_spent == 3  # 2 authored + 1 strain penalty
    assert strained.source == "route"  # still a routed jump, not a default


def test_native_ruleset_has_no_jump_adjudication():
    """AC1: the base seam default fails loud (mirrors ``ship_attack_params``) —
    a ruleset with no jump model raises rather than silently inventing a cost.
    The slug appears in the message so the GM/dev can see which module declined."""
    native = NativeRulesetModule()
    with pytest.raises(NotImplementedError) as exc:
        native.adjudicate_jump(route=_authored_route(), drive_rating=1, rng=random.Random(1))
    assert "native" in str(exc.value)


# ---------------------------------------------------------------------------
# AC2 — explicit, non-silent default for unrouted edges
# ---------------------------------------------------------------------------


def test_swn_adjudicate_jump_default_is_explicit_and_nonsilent():
    """AC2: an edge with no ``routes`` entry → the SWN drive model computes a
    *default* cost. No Silent Fallbacks: the default is a real positive cost
    (not a swallowed zero) and is labelled ``ruleset_default`` so the caller can
    emit the default-cost span."""
    swn = get_ruleset_module("swn")
    result = swn.adjudicate_jump(route=None, drive_rating=1, rng=random.Random(7))

    assert result.source == "ruleset_default"
    assert result.fuel_spent >= 1  # explicit positive default, never a silent 0
    assert result.transit_days >= 1
    assert isinstance(result.hazard_roll, int)


def test_resolve_route_for_jump_returns_authored_route_for_real_edge():
    """AC1/AC2: the route resolver returns the authored route for an adjacency it
    annotates (undirected match on endpoints)."""
    from sidequest.orbital.jump import resolve_route_for_jump

    carto = _yula_basilica_carto([_authored_route()])
    route = resolve_route_for_jump(carto, "yula", "basilica")
    assert route is not None
    assert route.jump_fuel == 2


def test_resolve_route_for_jump_none_for_unrouted_adjacency():
    """AC2: ``yula`` ↔ ``erebus`` is a real adjacency with no ``routes`` entry —
    the resolver returns ``None`` (the caller falls to the ruleset default). A
    bare adjacency is navigable, not an error."""
    from sidequest.orbital.jump import resolve_route_for_jump

    carto = _yula_basilica_carto([_authored_route()])
    assert resolve_route_for_jump(carto, "yula", "erebus") is None


def test_anomalous_route_endpoints_not_adjacent_dropped_with_warn(caplog):
    """AC2 edge case + python-review rule #4 (logging correctness): a ``routes``
    entry whose endpoints are not in any ``adjacent`` list is a route-level
    anomaly — it is dropped (NOT promoted to connectivity) and a WARNING is
    logged. It must never silently become a jumpable edge."""
    from sidequest.orbital.jump import resolve_route_for_jump

    ghost = Route(
        name="Ghost Lane",
        description="Authored against a non-adjacency.",
        from_id="yula",
        to_id="ceron",  # ceron is not in yula's adjacent list (not even a node)
    )
    carto = _yula_basilica_carto([ghost])

    with caplog.at_level(logging.WARNING):
        resolved = resolve_route_for_jump(carto, "yula", "basilica")

    # The malformed route does not make yula→basilica (or yula→ceron) jumpable.
    assert resolved is None
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "anomalous route must emit a WARNING, not be silently dropped"
    assert any("ceron" in r.getMessage() for r in warnings)


# ---------------------------------------------------------------------------
# AC3 — routes jump-mechanics schema finalized + documented; Black Door intact
# ---------------------------------------------------------------------------


def test_route_jump_fields_are_typed_not_extras_bag():
    """AC3: the additive jump fields are *typed* fields on ``Route`` (the schema
    C2 authors against), not loose keys in the ``extra='allow'`` bag. Reflection
    on ``model_fields`` is the legitimate type-check exception per CLAUDE.md."""
    for field in ("jump_fuel", "transit_days", "drive_rating_min", "hazard"):
        assert field in Route.model_fields, (
            f"{field!r} must be a declared Route field so C2 authors against a "
            "finalized schema, not an untyped extras key"
        )


def test_route_jump_fields_are_documented_for_c2():
    """AC3: each new jump field carries a ``Field(description=...)`` — the schema
    is documented *in code*, citable by the C2 (98-4) authors."""
    for field in ("jump_fuel", "transit_days", "drive_rating_min", "hazard"):
        desc = Route.model_fields[field].description
        assert desc, f"{field!r} needs a non-empty Field(description=...) for C2 to cite"


def test_route_jump_fields_are_optional_and_default_none():
    """AC3: the jump fields are *additive and optional* — an existing narrative
    route with none of them authored constructs cleanly with the fields at
    ``None`` (so unreached edges stay bare per Diamonds-and-Coal)."""
    bare = Route(name="Quiet Hop", description="No mechanics authored yet.")
    assert bare.jump_fuel is None
    assert bare.transit_days is None
    assert bare.drive_rating_min is None
    assert bare.hazard is None


def test_black_door_narrative_fields_preserved_and_distinct_from_hazard():
    """AC3: *The Black Door* (zephyr → ceron) keeps its narrative fields — and the
    narrative ``danger`` descriptor stays distinct from the new mechanical
    ``hazard`` field (so the Black Door's flavor never silently drives jump
    crunch). Jump-mechanics fields are absent until that edge is reached."""
    black_door = Route(
        name="The Black Door",
        description="A forbidden passage.",
        from_id="zephyr",
        to_id="ceron",
        distance="far",
        danger="lethal",
        difficulty="extreme",
    )
    assert black_door.from_id == "zephyr"
    assert black_door.to_id == "ceron"
    assert black_door.distance == "far"
    assert black_door.danger == "lethal"  # narrative descriptor, untouched
    assert black_door.difficulty == "extreme"
    assert black_door.hazard is None  # mechanical field is separate and unreached
    assert black_door.jump_fuel is None


# ---------------------------------------------------------------------------
# AC5 — OTEL span per jump (from/to region, fuel, transit days, hazard roll)
# AC2 — the default-cost span is explicit and labelled
# ---------------------------------------------------------------------------


def test_emit_jump_adjudicated_span_carries_all_five_attributes(capture_spans):
    """AC5: every jump emits ``jump.adjudicated`` with from/to region, fuel spent,
    transit days, and the hazard roll — the GM-panel lie-detector record."""
    from sidequest.telemetry.spans.jump import SPAN_JUMP_ADJUDICATED, emit_jump_adjudicated

    emit_jump_adjudicated(
        from_region="yula",
        to_region="basilica",
        fuel_spent=2,
        transit_days=6,
        hazard_roll=4,
    )

    spans = _spans_named(capture_spans, SPAN_JUMP_ADJUDICATED)
    assert len(spans) == 1
    attrs = spans[0].attributes
    assert attrs["from_region"] == "yula"
    assert attrs["to_region"] == "basilica"
    assert attrs["fuel_spent"] == 2
    assert attrs["transit_days"] == 6
    assert attrs["hazard_roll"] == 4


def test_emit_jump_default_cost_span_marks_ruleset_default(capture_spans):
    """AC2: the default-cost path emits its own ``jump.default_cost`` span labelled
    ``source=ruleset_default`` — the explicit, logged record that the unrouted
    edge got a computed default, not a silent zero."""
    from sidequest.telemetry.spans.jump import SPAN_JUMP_DEFAULT_COST, emit_jump_default_cost

    emit_jump_default_cost(
        from_region="yula",
        to_region="erebus",
        fuel_spent=1,
        transit_days=6,
    )

    spans = _spans_named(capture_spans, SPAN_JUMP_DEFAULT_COST)
    assert len(spans) == 1
    attrs = spans[0].attributes
    assert attrs["from_region"] == "yula"
    assert attrs["to_region"] == "erebus"
    assert attrs["fuel_spent"] == 1
    assert attrs["transit_days"] == 6
    assert attrs["source"] == "ruleset_default"


# ---------------------------------------------------------------------------
# AC4 — intra-system course model (ADR-130) stays untouched / separate
# ---------------------------------------------------------------------------


def test_adjudicate_jump_does_not_invoke_intra_system_course_model(monkeypatch):
    """AC4: the campaign-scale jump and the intra-system course (ADR-130) are two
    distinct movement scales and must not be conflated. Adjudicating a jump must
    NOT call ``compute_eta_and_dv`` — the jump uses the SWN drive model, never the
    orrery's Hohmann course math."""
    import sidequest.orbital.course as course_mod

    calls: list[int] = []
    monkeypatch.setattr(
        course_mod,
        "compute_eta_and_dv",
        lambda *a, **k: calls.append(1) or (0.0, 0.0),
    )

    swn = get_ruleset_module("swn")
    swn.adjudicate_jump(route=_authored_route(), drive_rating=2, rng=random.Random(1))
    swn.adjudicate_jump(route=None, drive_rating=1, rng=random.Random(1))

    assert calls == [], "jump adjudication must not touch the intra-system course model"
