"""Green Room spans (ADR-156) — the single-gate NPC materializer's OTEL
surface. The GM panel is the lie detector: without these spans there is no
way to tell whether ``admit()`` actually arbitrated a precedence conflict or
Claude just improvised a name that happened to match."""

from __future__ import annotations

from ._core import SPAN_ROUTES, SpanRoute

# Story 166-1 (ADR-156 §4): one identity group resolved to a canonical Npc —
# either freshly admitted or merged onto a pre-existing snapshot entry.
# ``canonical_tier`` is the winning candidate's LADDER rank (lower = higher
# precedence); ``candidates_seen``/``candidates_dropped`` let the GM panel
# verify a multi-feeder collision actually collapsed to one seat.
SPAN_GREEN_ROOM_MATERIALIZED = "green_room.materialized"
SPAN_ROUTES[SPAN_GREEN_ROOM_MATERIALIZED] = SpanRoute(
    event_type="state_transition",
    component="green_room",
    extract=lambda span: {
        "field": "green_room",
        "op": "materialized",
        "identity_key": (span.attributes or {}).get("identity_key", ""),
        "canonical_tier": (span.attributes or {}).get("canonical_tier", 0),
        "canonical_source": (span.attributes or {}).get("canonical_source", ""),
        "candidates_seen": (span.attributes or {}).get("candidates_seen", 0),
        "candidates_dropped": (span.attributes or {}).get("candidates_dropped", 0),
        "alias_count": (span.attributes or {}).get("alias_count", 0),
    },
)

# Story 166-1: fires only when an identity group has more than one candidate
# — the origin-precedence ladder had to arbitrate. ``winning_tier`` is the
# canonical's LADDER rank; ``losing_tiers`` is the sorted set of ranks that
# lost the seat. The GM panel reads this to confirm the ladder (not arrival
# order) decided who stands on stage.
SPAN_GREEN_ROOM_PRECEDENCE_CONFLICT = "green_room.precedence_conflict"
SPAN_ROUTES[SPAN_GREEN_ROOM_PRECEDENCE_CONFLICT] = SpanRoute(
    event_type="state_transition",
    component="green_room",
    extract=lambda span: {
        "field": "green_room",
        "op": "precedence_conflict",
        "identity_key": (span.attributes or {}).get("identity_key", ""),
        "winning_tier": (span.attributes or {}).get("winning_tier", 0),
        "losing_tiers": (span.attributes or {}).get("losing_tiers", ""),
    },
)

# Story 166-1: a prose/stage name was recorded against an identity via
# ``attach_alias`` — fires on a NEW attachment only (dedup misses are silent
# by design, same as the alias ledger elsewhere in the codebase).
SPAN_GREEN_ROOM_ALIAS_ATTACHED = "green_room.alias_attached"
SPAN_ROUTES[SPAN_GREEN_ROOM_ALIAS_ATTACHED] = SpanRoute(
    event_type="state_transition",
    component="green_room",
    extract=lambda span: {
        "field": "green_room",
        "op": "alias_attached",
        "identity_key": (span.attributes or {}).get("identity_key", ""),
        "alias": (span.attributes or {}).get("alias", ""),
        "from_source": (span.attributes or {}).get("from_source", ""),
    },
)

# Story 166-1: reserved for the narrator-mint feeder (ADR-156 §6, later
# task) — fires when a candidate reaching the gate at NARRATOR_INVENTED tier
# actually mints a new identity rather than attaching as an alias onto an
# existing one. Registered now so the routing-completeness lint and the GM
# panel's typed tab exist ahead of the feeder wiring.
SPAN_GREEN_ROOM_MINT = "green_room.mint"
SPAN_ROUTES[SPAN_GREEN_ROOM_MINT] = SpanRoute(
    event_type="state_transition",
    component="green_room",
    extract=lambda span: {
        "field": "green_room",
        "op": "mint",
        "identity_key": (span.attributes or {}).get("identity_key", ""),
        "prose_name": (span.attributes or {}).get("prose_name", ""),
        "source": (span.attributes or {}).get("source", ""),
    },
)
