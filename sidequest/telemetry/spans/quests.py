"""Quest-spine projection spans (ADR-137 / Story 77-8).

``quests.emitted`` confirms a QUESTS message was broadcast to the client — the
GM-panel lie-detector proving the engine projected the spine to the player
rather than the narrator improvising the campaign objective (CLAUDE.md OTEL
principle). The RELATIONSHIPS-snapshot sibling is ``relationships.emitted``.
"""

from __future__ import annotations

from ._core import SPAN_ROUTES, SpanRoute

__all__ = [
    "SPAN_QUESTS_EMITTED",
]

SPAN_QUESTS_EMITTED = "quests.emitted"

SPAN_ROUTES[SPAN_QUESTS_EMITTED] = SpanRoute(
    event_type="state_transition",
    component="quests",
    extract=lambda span: {
        "field": "quests.emitted",
        "quest_count": (span.attributes or {}).get("quest_count", 0),
        "anchor_count": (span.attributes or {}).get("anchor_count", 0),
        "has_stakes": bool((span.attributes or {}).get("has_stakes", False)),
        "changed": bool((span.attributes or {}).get("changed", False)),
    },
)
