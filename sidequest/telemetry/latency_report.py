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


__all__ = ["LatencyPercentiles", "latency_percentiles"]
