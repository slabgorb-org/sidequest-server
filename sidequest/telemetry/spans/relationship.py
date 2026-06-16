"""Relationship-surface spans (ADR-136).

``relationship.beat_recorded`` confirms a disposition beat was appended to the
log (the GM-panel lie-detector: history is written, not improvised — CLAUDE.md
OTEL principle). ``relationships.emitted`` confirms a RELATIONSHIPS message was
broadcast.  ``relationships.seen_gate`` records how many authored NPCs were
filtered out because the PC has not yet encountered them (last_seen_turn == 0),
making the ADR-136 perception firewall verifiable from the GM panel.
"""

from __future__ import annotations

from ._core import SPAN_ROUTES, SpanRoute

__all__ = [
    "SPAN_RELATIONSHIP_BEAT_RECORDED",
    "SPAN_RELATIONSHIPS_EMITTED",
    "SPAN_RELATIONSHIPS_SEEN_GATE",
]

SPAN_RELATIONSHIP_BEAT_RECORDED = "relationship.beat_recorded"
SPAN_RELATIONSHIPS_EMITTED = "relationships.emitted"
SPAN_RELATIONSHIPS_SEEN_GATE = "relationships.seen_gate"

SPAN_ROUTES[SPAN_RELATIONSHIP_BEAT_RECORDED] = SpanRoute(
    event_type="state_transition",
    component="disposition",
    extract=lambda span: {
        "field": "relationship.beat_recorded",
        "npc_name": (span.attributes or {}).get("npc_name", ""),
        "delta": (span.attributes or {}).get("delta", 0),
        "reason": (span.attributes or {}).get("reason", ""),
        "turn": (span.attributes or {}).get("turn", 0),
        "log_size": (span.attributes or {}).get("log_size", 0),
    },
)

SPAN_ROUTES[SPAN_RELATIONSHIPS_EMITTED] = SpanRoute(
    event_type="state_transition",
    component="relationships",
    extract=lambda span: {
        "field": "relationships.emitted",
        "entry_count": (span.attributes or {}).get("entry_count", 0),
        "changed": bool((span.attributes or {}).get("changed", False)),
    },
)

SPAN_ROUTES[SPAN_RELATIONSHIPS_SEEN_GATE] = SpanRoute(
    event_type="state_transition",
    component="relationships",
    extract=lambda span: {
        "field": "relationships.seen_gate",
        "total": (span.attributes or {}).get("total", 0),
        "included": (span.attributes or {}).get("included", 0),
        "filtered": (span.attributes or {}).get("filtered", 0),
    },
)
