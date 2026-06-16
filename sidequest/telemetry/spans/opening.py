"""OTEL span constants for the canned-openings pipeline.

Five spans:

- ``opening.resolved``              — chargen-complete, post candidate selection
- ``opening.directive_rendered``    — after the directive string is built
- ``opening.played``                — first-turn consumption
- ``opening.no_match``              — defensive: validator-8 bypass
- ``npc.authored_loaded``           — world materialization, per AuthoredNpc

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
SPAN_NPC_AUTHORED_LOADED = "npc.authored_loaded"

FLAT_ONLY_SPANS.update(
    {
        SPAN_OPENING_RESOLVED,
        SPAN_OPENING_DIRECTIVE_RENDERED,
        SPAN_OPENING_PLAYED,
        SPAN_OPENING_NO_MATCH,
        SPAN_NPC_AUTHORED_LOADED,
    }
)
