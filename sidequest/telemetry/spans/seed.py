"""Seed trope engine OTEL spans — draw, expire, fire, promote (Epic 22).

Sibling of :mod:`sidequest.telemetry.spans.trope`. The macro-trope engine
fires :data:`SPAN_TROPE_ACTIVATE` / :data:`SPAN_TROPE_RESOLVE`; the
short-arc seed engine fires per-seed spans for each lifecycle transition.

Story 22-3 introduced :data:`SPAN_SEED_DRAWN` and :data:`SPAN_SEED_EXPIRED`
in ``FLAT_ONLY_SPANS``. Story 22-4 promotes them into ``SPAN_ROUTES``
under ``component='seeds'`` so the GM panel's typed Subsystems feed
surfaces seed lifecycle alongside trope events, and adds
:data:`SPAN_SEED_FIRED` (routed — per-seed narrator surfacing) and
:data:`SPAN_SEED_PROMOTED` (flat-only scaffold, no call site until 22-5).
"""

from __future__ import annotations

from ._core import FLAT_ONLY_SPANS, SPAN_ROUTES, SpanRoute

SPAN_SEED_DRAWN = "seed.drawn"
SPAN_SEED_EXPIRED = "seed.expired"
SPAN_SEED_FIRED = "seed.fired"
SPAN_SEED_PROMOTED = "seed.promoted"

SPAN_ROUTES[SPAN_SEED_DRAWN] = SpanRoute(
    event_type="state_transition",
    component="seeds",
    extract=lambda span: {
        "field": "active_seeds",
        "seed_id": (span.attributes or {}).get("seed_id", ""),
        "session_id": (span.attributes or {}).get("session_id", ""),
        "activated_at_turn": (span.attributes or {}).get("activated_at_turn", 0),
    },
)

SPAN_ROUTES[SPAN_SEED_EXPIRED] = SpanRoute(
    event_type="state_transition",
    component="seeds",
    extract=lambda span: {
        "field": "active_seeds",
        "seed_id": (span.attributes or {}).get("seed_id", ""),
        "expired_at_turn": (span.attributes or {}).get("expired_at_turn", 0),
    },
)

SPAN_ROUTES[SPAN_SEED_FIRED] = SpanRoute(
    event_type="state_transition",
    component="seeds",
    extract=lambda span: {
        "field": "active_seeds",
        "seed_id": (span.attributes or {}).get("seed_id", ""),
    },
)

# sq-playtest 2026-06-07 (77-7 forensics, split item c): a session whose seed
# source authored NO seeds armed the lull-escalation engine with an empty deck
# — fired=False reason=none_available every turn, invisible until forensics.
# Emitted ONCE at session bootstrap when ensure_initial_draw finds no seeds
# (No Silent Fallbacks: a loud configuration smell, not a silent no-op).
SPAN_SEED_DECK_EMPTY = "seed.deck_empty"
SPAN_ROUTES[SPAN_SEED_DECK_EMPTY] = SpanRoute(
    event_type="state_transition",
    component="seeds",
    extract=lambda span: {
        "field": "seed_deck",
        "op": "deck_empty",
        "session_slug": (span.attributes or {}).get("session_slug", ""),
        "genre_slug": (span.attributes or {}).get("genre_slug", ""),
        "world_slug": (span.attributes or {}).get("world_slug", ""),
        "severity": "warning",
    },
)

FLAT_ONLY_SPANS.add(SPAN_SEED_PROMOTED)

__all__ = [
    "SPAN_SEED_DECK_EMPTY",
    "SPAN_SEED_DRAWN",
    "SPAN_SEED_EXPIRED",
    "SPAN_SEED_FIRED",
    "SPAN_SEED_PROMOTED",
]
