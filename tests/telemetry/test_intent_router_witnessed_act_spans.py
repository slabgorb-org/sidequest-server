"""witnessed_act intent-router span routes (Plan 2b, Task 1)."""

from __future__ import annotations


def test_witnessed_act_span_constants_exported_and_routed():
    from sidequest.telemetry.spans._core import SPAN_ROUTES
    from sidequest.telemetry.spans.intent_router import (
        SPAN_INTENT_ROUTER_WITNESSED_ACT_CLASSIFIED,
        SPAN_INTENT_ROUTER_WITNESSED_ACT_VOCABULARY,
    )

    assert SPAN_INTENT_ROUTER_WITNESSED_ACT_VOCABULARY in SPAN_ROUTES
    assert SPAN_INTENT_ROUTER_WITNESSED_ACT_CLASSIFIED in SPAN_ROUTES


def test_vocabulary_route_extracts_fields():
    from sidequest.telemetry.spans._core import SPAN_ROUTES
    from sidequest.telemetry.spans.intent_router import (
        SPAN_INTENT_ROUTER_WITNESSED_ACT_VOCABULARY,
    )

    route = SPAN_ROUTES[SPAN_INTENT_ROUTER_WITNESSED_ACT_VOCABULARY]
    assert route.component == "intent_router"

    class _FakeSpan:
        attributes = {"act_count": 5, "present_npc_count": 2, "genre_slug": "wry_whimsy"}

    fields = route.extract(_FakeSpan())
    assert fields["act_count"] == 5
    assert fields["present_npc_count"] == 2
    assert fields["genre_slug"] == "wry_whimsy"


def test_classified_route_extracts_fields():
    from sidequest.telemetry.spans._core import SPAN_ROUTES
    from sidequest.telemetry.spans.intent_router import (
        SPAN_INTENT_ROUTER_WITNESSED_ACT_CLASSIFIED,
    )

    route = SPAN_ROUTES[SPAN_INTENT_ROUTER_WITNESSED_ACT_CLASSIFIED]
    assert route.component == "intent_router"

    class _FakeSpan:
        attributes = {"emitted": 1, "act_ids": "expose_the_humbug", "genre_slug": "wry_whimsy"}

    fields = route.extract(_FakeSpan())
    assert fields["emitted"] == 1
    assert fields["act_ids"] == "expose_the_humbug"
