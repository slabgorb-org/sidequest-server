"""Story 71-40 (RED) — per-turn latency percentile harness.

AC1 (router decompose p50/p95) and AC3 (solo-turn p95) share one measurement
harness: given a list of captured latency values, compute p50/p95 by REUSING
the canonical ``sidequest.telemetry.validator._percentile`` helper (context-story
-71-40.md: "do not write a second percentile helper").

The harness is a pure function over the values it is handed — it does NOT filter
the input. That is load-bearing for AC1's edge case: a run where the router
retried (or a turn that hit the tool-loop ceiling) produces a retry-/loop-
inflated tail, and "do not silently drop intent_router.failed attempts — they
are part of the wall-clock cost the player feels." The tail MUST move p95.

These tests fail RED because ``sidequest/telemetry/latency_report.py`` does not
exist yet. Dev (GREEN) creates it, delegating the math to ``_percentile``.
"""

from __future__ import annotations

import pytest

# The canonical percentile helper the harness must reuse (AC1/AC3 guardrail).
from sidequest.telemetry.validator import _percentile


def test_latency_percentiles_empty_is_zero_not_crash() -> None:
    """Edge case: a turn set with no captured latencies yields 0.0/0.0 and a
    zero count — never a ``ZeroDivisionError`` or ``IndexError``. Mirrors
    ``_percentile([], pct) == 0.0``."""
    from sidequest.telemetry.latency_report import latency_percentiles

    result = latency_percentiles([])
    assert result.p50 == 0.0
    assert result.p95 == 0.0
    assert result.count == 0


def test_latency_percentiles_matches_canonical_percentile() -> None:
    """The harness must DELEGATE to ``validator._percentile`` — same numbers,
    no second implementation. Behavioral reuse proof (not a source grep): the
    harness output equals ``_percentile`` called directly on the same input."""
    from sidequest.telemetry.latency_report import latency_percentiles

    values = [120.0, 350.0, 410.0, 1800.0, 240.0, 6100.0, 900.0, 300.0, 280.0, 510.0]
    result = latency_percentiles(values)

    assert result.p50 == _percentile(values, 50), (
        "harness p50 must equal validator._percentile(values, 50) — the story "
        "forbids a second percentile helper"
    )
    assert result.p95 == _percentile(values, 95), (
        "harness p95 must equal validator._percentile(values, 95)"
    )
    assert result.count == len(values)


def test_latency_percentiles_tail_is_represented_not_dropped() -> None:
    """AC1 edge case: a retry-/loop-inflated tail turn (the 4-12s outliers the
    playtest measured) MUST pull p95 up. The harness does not filter outliers —
    they ARE the player-felt cost.

    Non-vacuous: the same fast body WITH the slow tail must yield a strictly
    higher p95 than WITHOUT it. A harness that silently dropped the tail (or
    clamped to the body) would fail this."""
    from sidequest.telemetry.latency_report import latency_percentiles

    fast_body = [300.0] * 19  # 19 healthy sub-budget turns
    with_tail = fast_body + [9000.0]  # one router-retry / loop-exceeded stall

    p95_body = latency_percentiles(fast_body).p95
    p95_with_tail = latency_percentiles(with_tail).p95

    assert p95_with_tail > p95_body, (
        "the inflated tail turn must raise p95 — dropping it would hide the "
        f"player-facing stall (body p95={p95_body}, with-tail p95={p95_with_tail})"
    )
    assert p95_with_tail == _percentile(with_tail, 95)


def test_latency_percentiles_p95_at_least_p50() -> None:
    """Sanity invariant across any non-degenerate spread: p95 >= p50."""
    from sidequest.telemetry.latency_report import latency_percentiles

    values = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
    result = latency_percentiles(values)
    assert result.p95 >= result.p50
    assert result.p50 == pytest.approx(_percentile(values, 50))
