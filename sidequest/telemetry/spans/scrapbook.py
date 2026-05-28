"""Scrapbook subsystem spans (Story 45-10).

Two spans flag scrapbook coverage gaps on save resume — Playtest 3
regression (Orin's 29-round session covered only 10 rounds; the other 19
were silently invisible to the recap injection subsystem). Per CLAUDE.md
OTEL Observability Principle, every backend fix that touches a subsystem
MUST add OTEL watcher events so the GM panel can verify the fix is
working — these spans are that verification.

- ``scrapbook.coverage_evaluated`` fires on every save-resume, including
  the no-op path where ``max_round=0`` or ``gap_count=0``. Sebastien
  (mechanical-first player, watches the GM panel) needs the negative
  confirmation that the detector ran. Without it, "scrapbook checked"
  is unobservable.

- ``scrapbook.coverage_gap_detected`` fires only when ``gap_count > 0``
  and carries the ``gap_rounds`` list verbatim so the GM panel can
  render which rounds went uncovered.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import trace

from ._core import SPAN_ROUTES, SpanRoute
from .span import Span

SPAN_SCRAPBOOK_COVERAGE_EVALUATED = "scrapbook.coverage_evaluated"
SPAN_ROUTES[SPAN_SCRAPBOOK_COVERAGE_EVALUATED] = SpanRoute(
    event_type="state_transition",
    component="scrapbook",
    extract=lambda span: {
        "field": "scrapbook",
        "op": "coverage_evaluated",
        "max_round": (span.attributes or {}).get("max_round", 0),
        "covered_count": (span.attributes or {}).get("covered_count", 0),
        "gap_count": (span.attributes or {}).get("gap_count", 0),
        "coverage_ratio": (span.attributes or {}).get("coverage_ratio", 1.0),
        "genre": (span.attributes or {}).get("genre", ""),
        "world": (span.attributes or {}).get("world", ""),
        "slug": (span.attributes or {}).get("slug", ""),
    },
)

SPAN_SCRAPBOOK_COVERAGE_GAP_DETECTED = "scrapbook.coverage_gap_detected"
SPAN_ROUTES[SPAN_SCRAPBOOK_COVERAGE_GAP_DETECTED] = SpanRoute(
    event_type="state_transition",
    component="scrapbook",
    extract=lambda span: {
        "field": "scrapbook",
        "op": "coverage_gap_detected",
        "max_round": (span.attributes or {}).get("max_round", 0),
        "covered_count": (span.attributes or {}).get("covered_count", 0),
        "gap_count": (span.attributes or {}).get("gap_count", 0),
        # Mirrors the SPAN_SCRAPBOOK_COVERAGE_EVALUATED default (1.0) for
        # symmetry — gap_detected only fires when gap_count > 0 so the
        # default is unreachable in practice; pick the same safe value.
        "coverage_ratio": (span.attributes or {}).get("coverage_ratio", 1.0),
        # gap_rounds is the load-bearing payload — the GM panel renders the
        # missing-round list. OTEL serialises sequence attributes; tests
        # accept any string repr that names the boundary rounds.
        "gap_rounds": (span.attributes or {}).get("gap_rounds", ""),
        "genre": (span.attributes or {}).get("genre", ""),
        "world": (span.attributes or {}).get("world", ""),
        "slug": (span.attributes or {}).get("slug", ""),
    },
)


# ---------------------------------------------------------------------------
# Story 65-6 — NPC portrait resolution on scene invocation.
#
# When an NPC is invoked in a turn it lands as a ScrapbookEntryNpcRef. The
# emitter attaches a world-scoped portrait_url IFF the NPC matches a
# portrait_manifest entry for the current world. Both outcomes are observable
# so the GM/dev panel can confirm the lookup ran (and Claude isn't silently
# dropping authored art) — a portrait that should resolve but doesn't is a
# slug/path skew (the classic 404), and the not-found span proves the lookup
# happened rather than being skipped. Missing portraits are EXPECTED for
# ad-hoc NPCs; this is observability, not an error.
# ---------------------------------------------------------------------------

SPAN_SCRAPBOOK_NPC_PORTRAIT_RESOLVED = "scrapbook.npc_portrait_resolved"
SPAN_ROUTES[SPAN_SCRAPBOOK_NPC_PORTRAIT_RESOLVED] = SpanRoute(
    event_type="state_transition",
    component="scrapbook",
    extract=lambda span: {
        "field": "scrapbook",
        "op": "npc_portrait_resolved",
        "npc_name": (span.attributes or {}).get("npc_name", ""),
        "genre": (span.attributes or {}).get("genre", ""),
        "world": (span.attributes or {}).get("world", ""),
        "slug": (span.attributes or {}).get("slug", ""),
    },
)

SPAN_SCRAPBOOK_NPC_PORTRAIT_NOT_FOUND = "scrapbook.npc_portrait_not_found"
SPAN_ROUTES[SPAN_SCRAPBOOK_NPC_PORTRAIT_NOT_FOUND] = SpanRoute(
    event_type="state_transition",
    component="scrapbook",
    extract=lambda span: {
        "field": "scrapbook",
        "op": "npc_portrait_not_found",
        "npc_name": (span.attributes or {}).get("npc_name", ""),
        "genre": (span.attributes or {}).get("genre", ""),
        "world": (span.attributes or {}).get("world", ""),
        "slug": (span.attributes or {}).get("slug", ""),
    },
)


def _portrait_attrs(*, npc_name: str, genre: str, world: str, slug: str) -> dict[str, str]:
    return {
        "npc_name": npc_name,
        "genre": genre,
        "world": world,
        "slug": slug,
    }


@contextmanager
def scrapbook_npc_portrait_resolved_span(
    *,
    npc_name: str,
    genre: str,
    world: str,
    slug: str,
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    """INFO — fired when an invoked NPC matches a portrait_manifest entry and a
    world-scoped portrait_url is attached to its scrapbook ref (Story 65-6)."""
    with Span.open(
        SPAN_SCRAPBOOK_NPC_PORTRAIT_RESOLVED,
        _portrait_attrs(npc_name=npc_name, genre=genre, world=world, slug=slug),
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def scrapbook_npc_portrait_not_found_span(
    *,
    npc_name: str,
    genre: str,
    world: str,
    slug: str,
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    """INFO — fired when an invoked NPC has no matching portrait_manifest entry
    for the current world, so its scrapbook ref carries no portrait_url. Missing
    portraits are EXPECTED for ad-hoc NPCs; the span proves the lookup ran."""
    with Span.open(
        SPAN_SCRAPBOOK_NPC_PORTRAIT_NOT_FOUND,
        _portrait_attrs(npc_name=npc_name, genre=genre, world=world, slug=slug),
        tracer_override=_tracer,
    ) as span:
        yield span
