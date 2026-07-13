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
# verify a multi-feeder collision actually collapsed to one seat. Deliberately
# NOT emitted for a group that folds into a batch-mate's seat admitted earlier
# in the same admit() call — that identity never materialized; the fold
# surfaces as green_room.precedence_conflict instead (task-1 rework).
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

# Story 166-1: the origin-precedence ladder had to arbitrate. Two shapes:
# (a) intra-group — an identity group holds more than one candidate;
# ``identity_key`` is the group's key, ``losing_tiers`` the sorted set of
# ranks that lost the seat. (b) cross-group fold (task-1 rework) — a group's
# sole candidate resolved onto a batch-mate's seat admitted earlier in the
# same admit() call; ``identity_key``/``winning_tier`` are the WINNER's (the
# seat that absorbed the fold), ``losing_tiers`` the folded group's rank. In
# both shapes the GM panel reads this to confirm the ladder (not arrival
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

# Story 166-10 / ADR-156 §6: the DISPLAY half of the coal→diamond promotion —
# the seated Other was given the stage name the narrator's prose used
# (``StructuredEncounter.promote_actor``). ``alias_attached`` proves the IDENTITY
# learned the name; this proves the name reached the surface the player is
# actually looking at. Without both, a panel still reading "the Scrapborn" under
# narration that says "Ihnsch of the Rusted Works" is indistinguishable from the
# narrator simply improvising a name with no engine behind it.
#
# ``seat_id`` is the actor's UNCHANGED entity id — the promotion adds a label, it
# never repoints the id (a seat left under a prose alias is an opponent the
# engine cannot resolve). Carrying both on the span is what lets the GM panel see
# that the two stayed distinct.
SPAN_GREEN_ROOM_ACTOR_PROMOTED = "green_room.actor_promoted"
SPAN_ROUTES[SPAN_GREEN_ROOM_ACTOR_PROMOTED] = SpanRoute(
    event_type="state_transition",
    component="green_room",
    extract=lambda span: {
        "field": "green_room",
        "op": "actor_promoted",
        "seat_id": (span.attributes or {}).get("seat_id", ""),
        "display_name": (span.attributes or {}).get("display_name", ""),
        "side": (span.attributes or {}).get("side", ""),
    },
)

# Story 166-1 / ADR-156 §6 (Task 5, live): fires when a candidate reaching
# the gate at NARRATOR_INVENTED tier actually mints a new identity rather
# than attaching as an alias onto an existing one — emitted from both
# narrator-mint feeders (`narration_apply._apply_npc_mentions`'s novel-name
# branch, `session_helpers._auto_mint_prose_only_npcs`) at the genuine-mint
# path, after `_attach_before_mint` declines to attach.
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
