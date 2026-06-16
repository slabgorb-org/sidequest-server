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

from ._core import SPAN_ROUTES, SpanRoute

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
