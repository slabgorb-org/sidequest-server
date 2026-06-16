"""Premise/Bloc political-substrate spans (wry_whimsy, Plan 2).

Every belief/defiance change emits one of these so the GM panel is the
lie-detector for the political layer too (CLAUDE.md OTEL principle): a real
revolution writes these spans; narrated improvisation does not.
"""

from __future__ import annotations

from ._core import SPAN_ROUTES, SpanRoute

__all__ = [
    "SPAN_PREMISE_BELIEF_DRAINED",
    "SPAN_BLOC_DEFIANCE_RAISED",
    "SPAN_PREMISE_COLLAPSED",
    "SPAN_BLOC_TIPPED",
]

SPAN_PREMISE_BELIEF_DRAINED = "premise.belief_drained"
SPAN_BLOC_DEFIANCE_RAISED = "bloc.defiance_raised"
SPAN_PREMISE_COLLAPSED = "premise.collapsed"
SPAN_BLOC_TIPPED = "bloc.tipped"

SPAN_ROUTES[SPAN_PREMISE_BELIEF_DRAINED] = SpanRoute(
    event_type="state_transition",
    component="premise",
    extract=lambda span: {
        "field": "premise.belief_drained",
        "premise_id": (span.attributes or {}).get("premise_id", ""),
        "act_id": (span.attributes or {}).get("act_id", ""),
        "delta": (span.attributes or {}).get("delta", 0),
        "new_reserve": (span.attributes or {}).get("new_reserve", 0),
        "witnesses": (span.attributes or {}).get("witnesses", ""),
        "turn": (span.attributes or {}).get("turn", 0),
    },
)

SPAN_ROUTES[SPAN_BLOC_DEFIANCE_RAISED] = SpanRoute(
    event_type="state_transition",
    component="bloc",
    extract=lambda span: {
        "field": "bloc.defiance_raised",
        "bloc_id": (span.attributes or {}).get("bloc_id", ""),
        "act_id": (span.attributes or {}).get("act_id", ""),
        "source": (span.attributes or {}).get("source", ""),
        "delta": (span.attributes or {}).get("delta", 0),
        "new_defiance": (span.attributes or {}).get("new_defiance", 0),
        "turn": (span.attributes or {}).get("turn", 0),
    },
)

SPAN_ROUTES[SPAN_PREMISE_COLLAPSED] = SpanRoute(
    event_type="state_transition",
    component="premise",
    extract=lambda span: {
        "field": "premise.collapsed",
        "premise_id": (span.attributes or {}).get("premise_id", ""),
        "act_id": (span.attributes or {}).get("act_id", ""),
        "new_reserve": (span.attributes or {}).get("new_reserve", 0),
        "turn": (span.attributes or {}).get("turn", 0),
    },
)

SPAN_ROUTES[SPAN_BLOC_TIPPED] = SpanRoute(
    event_type="state_transition",
    component="bloc",
    extract=lambda span: {
        "field": "bloc.tipped",
        "bloc_id": (span.attributes or {}).get("bloc_id", ""),
        "act_id": (span.attributes or {}).get("act_id", ""),
        "new_defiance": (span.attributes or {}).get("new_defiance", 0),
        "turn": (span.attributes or {}).get("turn", 0),
    },
)
