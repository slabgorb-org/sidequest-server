"""Story 82-9 (RED) — wire ``latency_percentiles`` to a real consumer (AC1).

71-40 shipped the ``latency_percentiles`` harness but left it with NO production
caller — it was exercised only by tests (Reviewer wiring finding: "the wiring
loop only closes when AC5 actually invokes the harness"). This story closes that
loop: a report builder consumes the captured per-turn decompose + turn latency
series by invoking ``latency_percentiles``, producing the structured numbers the
AC5 diagnosis artifact is written from.

The wiring requirement (CLAUDE.md "Verify Wiring, Not Just Existence") is proved
BEHAVIORALLY, not by grepping source: the report's percentiles must EQUAL
``latency_percentiles(series)`` for the same input — a builder that recomputed
percentiles its own way, or returned constants, fails these tests.

RED: ``build_latency_report`` / ``LatencyReport`` do not exist in
``sidequest/telemetry/latency_report.py`` yet. Dev (GREEN) adds the consumer,
delegating every percentile to the existing ``latency_percentiles`` harness.
"""

from __future__ import annotations

from sidequest.telemetry.latency_report import latency_percentiles

# The captured series the 2026-05-27 playtest measured — router decompose totals
# (4-12s tail over the ADR-113 <1.2s budget) and whole-turn wall-clock.
_DECOMPOSE = [410.0, 920.0, 1180.0, 4200.0, 760.0, 11800.0, 1020.0, 880.0, 1340.0, 690.0]
_TURN = [1900.0, 2400.0, 3100.0, 9800.0, 2200.0, 14200.0, 2600.0, 2050.0, 3300.0, 1980.0]


def test_build_latency_report_delegates_to_harness_for_decompose() -> None:
    """AC1: the report's decompose percentiles are EXACTLY what
    ``latency_percentiles`` returns for the same series — proof the consumer
    delegates to the harness rather than reimplementing the math (the story
    forbids a second percentile path)."""
    from sidequest.telemetry.latency_report import build_latency_report

    report = build_latency_report(decompose_latencies=_DECOMPOSE, turn_latencies=_TURN)
    expected = latency_percentiles(_DECOMPOSE)

    assert report.decompose.p50 == expected.p50, (
        "report decompose p50 must equal latency_percentiles(decompose).p50 — "
        f"got {report.decompose.p50} vs {expected.p50}"
    )
    assert report.decompose.p95 == expected.p95
    assert report.decompose.count == expected.count == len(_DECOMPOSE)


def test_build_latency_report_delegates_to_harness_for_turn() -> None:
    """AC1: the same delegation holds for the whole-turn series — the second
    percentile source AC5 correlates against the decompose split."""
    from sidequest.telemetry.latency_report import build_latency_report

    report = build_latency_report(decompose_latencies=_DECOMPOSE, turn_latencies=_TURN)
    expected = latency_percentiles(_TURN)

    assert report.turn.p50 == expected.p50
    assert report.turn.p95 == expected.p95
    assert report.turn.count == expected.count == len(_TURN)


def test_build_latency_report_empty_series_is_zero_not_crash() -> None:
    """Edge: an empty capture (a diagnosis run with no recorded turns) yields
    0.0/0.0/0 for both series — never a ``ZeroDivisionError`` — mirroring the
    harness's empty-sample contract."""
    from sidequest.telemetry.latency_report import build_latency_report

    report = build_latency_report(decompose_latencies=[], turn_latencies=[])
    assert report.decompose.p50 == 0.0
    assert report.decompose.p95 == 0.0
    assert report.decompose.count == 0
    assert report.turn.count == 0


def test_build_latency_report_renders_the_captured_numbers() -> None:
    """AC1/AC5: the report renders a human-readable artifact that actually
    contains the computed p95 numbers — the machine half of the AC5 diagnosis
    deliverable. Non-vacuous: a renderer that dropped the numbers (or emitted a
    template with no data) fails this."""
    from sidequest.telemetry.latency_report import build_latency_report

    report = build_latency_report(decompose_latencies=_DECOMPOSE, turn_latencies=_TURN)
    text = report.render()

    assert isinstance(text, str) and text.strip(), "render() must return non-empty text"
    decompose_p95 = latency_percentiles(_DECOMPOSE).p95
    # The rendered artifact must surface the decompose p95 the diagnosis turns on.
    assert str(int(decompose_p95)) in text or f"{decompose_p95}" in text, (
        f"rendered report must contain the decompose p95 ({decompose_p95}); got:\n{text}"
    )


def test_build_latency_report_tail_moves_decompose_p95() -> None:
    """AC1 diagnostic property: the retry-/loop-inflated tail the diagnosis
    exists to surface MUST pull the report's decompose p95 up — the consumer
    does not silently drop outliers (it inherits the harness's no-filter
    contract). A builder that clamped the tail would fail this."""
    from sidequest.telemetry.latency_report import build_latency_report

    fast_body = [600.0] * 19
    with_tail = fast_body + [11800.0]

    p95_body = build_latency_report(decompose_latencies=fast_body, turn_latencies=[]).decompose.p95
    p95_tail = build_latency_report(decompose_latencies=with_tail, turn_latencies=[]).decompose.p95

    assert p95_tail > p95_body, (
        "the inflated tail must raise the reported decompose p95 — dropping it "
        f"would hide the player-facing stall (body={p95_body}, with-tail={p95_tail})"
    )
