"""Per-turn latency percentile harness (Story 71-40, AC1 + AC3).

The router-decompose p50/p95 (AC1) and the solo-turn p95 (AC3) share one
measurement primitive: given a list of captured latency values, compute p50/p95
by REUSING the canonical :func:`sidequest.telemetry.validator._percentile`
helper. The story forbids a second percentile implementation — this module is a
thin :class:`LatencyPercentiles` wrapper, not a reimplementation.

The harness is a pure function over the values it is handed — it does NOT filter
the input. That is load-bearing for AC1: a run where the router retried (or a
turn that hit the tool-loop ceiling) produces a retry-/loop-inflated tail, and
those slow turns ARE the wall-clock cost the player feels. The tail MUST move
p95; silently dropping outliers would hide the stall the diagnosis exists to
surface (CLAUDE.md "No Silent Fallbacks").
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sidequest.telemetry.validator import _percentile


@dataclass(frozen=True)
class LatencyPercentiles:
    """p50/p95 over a captured latency sample, plus the sample size.

    ``count`` is the number of values measured (0 for an empty sample, where
    both percentiles are 0.0 — mirrors ``_percentile([], pct) == 0.0``).
    """

    p50: float
    p95: float
    count: int


def latency_percentiles(values: Sequence[float]) -> LatencyPercentiles:
    """Compute p50/p95 over ``values`` by delegating to ``validator._percentile``.

    Does not filter the input: every value (including retry-/loop-inflated
    tails) contributes to the percentile, because those are the player-felt
    stalls the diagnosis must surface. An empty sample yields ``0.0/0.0`` with
    a zero count rather than raising.
    """
    sample = list(values)
    return LatencyPercentiles(
        p50=_percentile(sample, 50),
        p95=_percentile(sample, 95),
        count=len(sample),
    )


@dataclass(frozen=True)
class LatencyReport:
    """The AC5 diagnosis report: decompose + whole-turn percentiles over a run.

    Both fields are computed by :func:`latency_percentiles` over the captured
    series — the report does not re-derive percentiles, it consumes the harness
    (Story 82-9: close the 71-40 wiring loop, ``latency_percentiles`` had no
    production caller).
    """

    decompose: LatencyPercentiles
    turn: LatencyPercentiles

    def render(self) -> str:
        """Human-readable artifact carrying the captured numbers — the machine
        half of the AC5 diagnosis deliverable a maintainer reads off the run."""
        lines = [
            "Per-turn latency report (Story 71-40 AC5)",
            f"  router decompose: p50={self.decompose.p50:.1f}ms "
            f"p95={self.decompose.p95:.1f}ms (n={self.decompose.count})",
            f"  whole turn:       p50={self.turn.p50:.1f}ms "
            f"p95={self.turn.p95:.1f}ms (n={self.turn.count})",
        ]
        return "\n".join(lines)


def build_latency_report(
    *,
    decompose_latencies: Sequence[float],
    turn_latencies: Sequence[float],
) -> LatencyReport:
    """Build the AC5 report by running each captured series through the harness.

    This is the production consumer the 71-40 Reviewer wiring finding asked for:
    a representative run's ``intent_router.decompose`` totals and whole-turn
    wall-clock are fed in, and ``latency_percentiles`` produces the p50/p95 the
    diagnosis correlates (env-vs-code, iteration count). Inherits the harness's
    no-filter contract — the retry-/loop-inflated tail moves p95, it is not
    silently dropped.
    """
    return LatencyReport(
        decompose=latency_percentiles(decompose_latencies),
        turn=latency_percentiles(turn_latencies),
    )


__all__ = [
    "LatencyPercentiles",
    "LatencyReport",
    "build_latency_report",
    "latency_percentiles",
]
