"""Premise/Bloc OTEL span routes are registered (Plan 2, Task 2)."""

from __future__ import annotations


def test_premise_span_constants_exported_and_routed():
    from sidequest.telemetry.spans import (
        SPAN_BLOC_DEFIANCE_RAISED,
        SPAN_BLOC_TIPPED,
        SPAN_PREMISE_BELIEF_DRAINED,
        SPAN_PREMISE_COLLAPSED,
    )
    from sidequest.telemetry.spans._core import SPAN_ROUTES

    for name in (
        SPAN_PREMISE_BELIEF_DRAINED,
        SPAN_BLOC_DEFIANCE_RAISED,
        SPAN_PREMISE_COLLAPSED,
        SPAN_BLOC_TIPPED,
    ):
        assert name in SPAN_ROUTES, f"{name} not registered in SPAN_ROUTES"

    # Terminal-event routes carry act_id so the GM panel can attribute a
    # collapse/tip to the act that caused it (parity with the delta routes).
    class _EmptySpan:
        attributes: dict = {}

    assert "act_id" in SPAN_ROUTES[SPAN_PREMISE_COLLAPSED].extract(_EmptySpan())
    assert "act_id" in SPAN_ROUTES[SPAN_BLOC_TIPPED].extract(_EmptySpan())


def test_belief_drained_route_extracts_fields():
    from sidequest.telemetry.spans import SPAN_PREMISE_BELIEF_DRAINED
    from sidequest.telemetry.spans._core import SPAN_ROUTES

    route = SPAN_ROUTES[SPAN_PREMISE_BELIEF_DRAINED]
    assert route.component == "premise"

    class _FakeSpan:
        attributes = {
            "premise_id": "the_wizards_humbug",
            "act_id": "expose_the_humbug",
            "delta": -35,
            "new_reserve": 55,
            "witnesses": "Dorothy,Toto",
            "turn": 3,
        }

    fields = route.extract(_FakeSpan())
    assert fields["premise_id"] == "the_wizards_humbug"
    assert fields["new_reserve"] == 55
