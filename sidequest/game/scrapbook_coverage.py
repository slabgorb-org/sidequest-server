"""Scrapbook coverage gap detection on save resume (Story 45-10).

Playtest 3 evidence (2026-04-19): Orin's session covered 29 narrative
rounds but only 10 of those rounds had scrapbook entries. The other 19
were invisible to the recap injection subsystem; the narrator silently
invented continuity. Cause was benign (save predated scrapbook subsystem
being live); damage was real.

This module is the read-side hygiene the bug demanded — fires every
save-resume, computes the round-by-round coverage gap, and emits two
OTEL spans + a watcher event so the GM panel surfaces gaps loudly.
Read-only (no backfill); the context-story-45-10.md decision for
warn-only is intentional — backfill would manufacture diamond from
coal (ADR-014 inverse).

Post-45-11 invariant: ``narrative_log.round_number == turn_manager.interaction``,
and ``scrapbook_entries.turn_id`` is set from the same interaction
counter at emit time. So scrapbook coverage maps directly to round
number without an explicit join through narrative_log. The lockstep
write happens at the narration site —
``websocket_session_handler._execute_narration_turn`` writes
``narrative_log.round_number`` from ``turn_manager.interaction`` on
every committed turn — which is non-obvious from this module alone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sidequest.game.persistence import SqliteStore
from sidequest.game.session import GameSnapshot
from sidequest.telemetry.spans import (
    SPAN_SCRAPBOOK_COVERAGE_EVALUATED,
    SPAN_SCRAPBOOK_COVERAGE_GAP_DETECTED,
    Span,
)
from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScrapbookCoverageReport:
    """Read-only summary of scrapbook coverage vs narrative log.

    ``coverage_ratio`` is defined as ``1.0`` on empty narrative logs
    (``max_round == 0``) — the GM-panel chart axis must always be
    plottable, and a NaN/division-by-zero would break the dashboard.
    """

    max_round: int
    covered_count: int
    gap_count: int
    gap_rounds: tuple[int, ...] = field(default=())
    coverage_ratio: float = 1.0


def detect_scrapbook_coverage_gaps(
    *,
    store: SqliteStore,
    snapshot: GameSnapshot,
    slug: str = "",
) -> ScrapbookCoverageReport:
    """Detect scrapbook coverage gaps against the narrative log.

    Fires ``scrapbook.coverage_evaluated`` on every call (Sebastien's
    negative-confirmation requirement). Fires ``scrapbook.coverage_gap_detected``
    and publishes a ``scrapbook_coverage_gap`` watcher event with severity
    ``warning`` only when ``gap_count > 0``.

    Read-only. Threads through ``store._conn``; opens no new connections
    and writes nothing.

    :param store: SqliteStore for the slot being resumed. Reuses its
        connection — no new sqlite handles.
    :param snapshot: Loaded GameSnapshot. Read for span attribution
        (``genre``, ``world``); state is not mutated.
    :param slug: Optional slot slug for span attribution. Empty string on
        the legacy non-slug resume path.
    """
    max_round = store.max_narrative_round()

    # Pull every distinct round that has at least one scrapbook entry.
    # Filter to the valid range — rows with turn_id <= 0 or > max_round
    # are noise (test fixture artifacts or pre-lockstep stragglers) and
    # would distort coverage_count / gap_rounds.
    if max_round > 0:
        rows = store._conn.execute(
            "SELECT DISTINCT turn_id FROM scrapbook_entries WHERE turn_id >= 1 AND turn_id <= ?",
            (max_round,),
        ).fetchall()
        covered = {int(r[0]) for r in rows}
    else:
        covered = set()

    expected = set(range(1, max_round + 1)) if max_round > 0 else set()
    gap = sorted(expected - covered)
    covered_count = len(covered & expected)
    gap_count = len(gap)
    coverage_ratio = (covered_count / max_round) if max_round > 0 else 1.0

    report = ScrapbookCoverageReport(
        max_round=max_round,
        covered_count=covered_count,
        gap_count=gap_count,
        gap_rounds=tuple(gap),
        coverage_ratio=coverage_ratio,
    )

    genre = snapshot.genre_slug or ""
    world = snapshot.world_slug or ""
    base_attrs = {
        "max_round": max_round,
        "covered_count": covered_count,
        "gap_count": gap_count,
        "coverage_ratio": coverage_ratio,
        "genre": genre,
        "world": world,
        "slug": slug,
    }

    with Span.open(SPAN_SCRAPBOOK_COVERAGE_EVALUATED, base_attrs):
        pass

    if gap_count > 0:
        logger.warning(
            "scrapbook.coverage_gap_detected genre=%s world=%s slug=%s gap_count=%d gap_rounds=%s",
            genre,
            world,
            slug,
            gap_count,
            list(gap),
        )
        # OTEL span attributes don't natively support nested lists in
        # every exporter — serialize gap_rounds as a tuple of ints which
        # the SDK handles, and publish the verbatim list to the watcher
        # so the GM panel can render it without re-parsing.
        gap_attrs = {**base_attrs, "gap_rounds": tuple(gap)}
        with Span.open(SPAN_SCRAPBOOK_COVERAGE_GAP_DETECTED, gap_attrs):
            pass

        _watcher_publish(
            "scrapbook_coverage_gap",
            {
                "max_round": max_round,
                "covered_count": covered_count,
                "gap_count": gap_count,
                "coverage_ratio": coverage_ratio,
                "gap_rounds": list(gap),
                "genre": genre,
                "world": world,
                "slug": slug,
            },
            component="scrapbook",
            severity="warning",
        )

    return report
