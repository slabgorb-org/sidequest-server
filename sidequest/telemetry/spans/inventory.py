"""Inventory spans — narrator-extracted item mutations."""

from __future__ import annotations

import json as _json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

from ._core import FLAT_ONLY_SPANS, SPAN_ROUTES, SpanRoute
from .span import Span

# Port-artifact constant — extractor agent not reimplemented.
SPAN_INVENTORY_EXTRACTION = "inventory.extraction"
FLAT_ONLY_SPANS.add(SPAN_INVENTORY_EXTRACTION)

SPAN_INVENTORY_NARRATOR_EXTRACTED = "inventory.narrator_extracted"
SPAN_ROUTES[SPAN_INVENTORY_NARRATOR_EXTRACTED] = SpanRoute(
    event_type="state_transition",
    component="inventory",
    extract=lambda span: {
        "field": "inventory",
        "op": "narrator_extracted",
        "gained": (span.attributes or {}).get("gained_json", "[]"),
        "lost": (span.attributes or {}).get("lost_json", "[]"),
        "discarded": (span.attributes or {}).get("discarded_json", "[]"),
        "consumed": (span.attributes or {}).get("consumed_json", "[]"),
        "gained_count": (span.attributes or {}).get("gained_count", 0),
        "lost_count": (span.attributes or {}).get("lost_count", 0),
        "discarded_count": (span.attributes or {}).get("discarded_count", 0),
        "consumed_count": (span.attributes or {}).get("consumed_count", 0),
        "player_name": (span.attributes or {}).get("player_name", ""),
        "turn_number": (span.attributes or {}).get("turn_number", 0),
    },
)


@contextmanager
def inventory_narrator_extracted_span(
    *,
    gained: list[str],
    lost: list[str],
    player_name: str,
    turn_number: int,
    discarded: list[str] | None = None,
    consumed: list[str] | None = None,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Lists are JSON-encoded — OTEL silently drops list attribute values.

    ``discarded``: state transitioned out of Carried (still in inventory).
    ``consumed``: removed because used up (one-shot consumables).
    """
    discarded_list = list(discarded or [])
    consumed_list = list(consumed or [])
    attributes: dict[str, Any] = {
        "gained_json": _json.dumps(list(gained)),
        "lost_json": _json.dumps(list(lost)),
        "discarded_json": _json.dumps(discarded_list),
        "consumed_json": _json.dumps(consumed_list),
        "gained_count": len(gained),
        "lost_count": len(lost),
        "discarded_count": len(discarded_list),
        "consumed_count": len(consumed_list),
        "player_name": player_name,
        "turn_number": turn_number,
        **attrs,
    }
    with Span.open(
        SPAN_INVENTORY_NARRATOR_EXTRACTED,
        attributes,
        tracer_override=_tracer,
    ) as span:
        yield span


# ---------------------------------------------------------------------------
# Story 71-15 (ADR-055) — movement-consumed resource depletion.
# Fires once per item burned on a room-graph transition (torch model). The
# GM panel reads it as proof the dungeon clock is ablating resources rather
# than the narrator improvising an inexhaustible light source.
# ---------------------------------------------------------------------------

SPAN_ITEM_RESOURCE_DEPLETED = "item.resource_depleted"
SPAN_ROUTES[SPAN_ITEM_RESOURCE_DEPLETED] = SpanRoute(
    event_type="state_transition",
    component="inventory",
    extract=lambda span: {
        "field": "inventory",
        "op": "resource_depleted",
        "item": (span.attributes or {}).get("item"),
        "before": (span.attributes or {}).get("before"),
        "after": (span.attributes or {}).get("after"),
        "exhausted": (span.attributes or {}).get("exhausted", False),
        "actor": (span.attributes or {}).get("actor", ""),
    },
)


@contextmanager
def item_resource_depleted_span(
    *,
    item: str,
    before: int,
    after: int,
    exhausted: bool,
    actor: str = "",
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """One span per movement-consumed item burned on a room-graph transition.

    ``before`` / ``after`` are the ``uses_remaining`` count either side of the
    decrement; ``exhausted`` is True when ``after == 0``.
    """
    with Span.open(
        SPAN_ITEM_RESOURCE_DEPLETED,
        {
            "item": item,
            "before": before,
            "after": after,
            "exhausted": exhausted,
            "actor": actor,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span
