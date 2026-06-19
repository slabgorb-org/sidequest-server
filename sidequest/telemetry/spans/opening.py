"""OTEL span constants for the canned-openings pipeline.

Seven spans:

- ``opening.resolved``              — chargen-complete, post candidate selection
- ``opening.directive_rendered``    — after the directive string is built
- ``opening.played``                — first-turn consumption
- ``opening.no_match``              — defensive: validator-8 bypass
- ``opening.props_persisted``       — opening-scene props written to room_states (Story 126-18)
- ``npc.authored_loaded``           — world materialization, per AuthoredNpc
- ``npc.authored_load_skipped``     — preload skipped (resumed session), with reason

All flat-only — no typed-event route. The GM panel reads them via the
``agent_span_close`` fan-out (CLAUDE.md "OTEL Observability Principle").

See ``docs/superpowers/specs/2026-05-01-canned-openings-design.md`` §3.3.
"""

from __future__ import annotations

from ._core import FLAT_ONLY_SPANS

SPAN_OPENING_RESOLVED = "opening.resolved"
SPAN_OPENING_DIRECTIVE_RENDERED = "opening.directive_rendered"
SPAN_OPENING_PLAYED = "opening.played"
SPAN_OPENING_NO_MATCH = "opening.no_match"
SPAN_OPENING_PROPS_PERSISTED = "opening.props_persisted"
SPAN_NPC_AUTHORED_LOADED = "npc.authored_loaded"
SPAN_NPC_AUTHORED_LOAD_SKIPPED = "npc.authored_load_skipped"

FLAT_ONLY_SPANS.update(
    {
        SPAN_OPENING_RESOLVED,
        SPAN_OPENING_DIRECTIVE_RENDERED,
        SPAN_OPENING_PLAYED,
        SPAN_OPENING_NO_MATCH,
        SPAN_OPENING_PROPS_PERSISTED,
        SPAN_NPC_AUTHORED_LOADED,
        SPAN_NPC_AUTHORED_LOAD_SKIPPED,
    }
)
