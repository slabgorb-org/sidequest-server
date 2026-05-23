"""Seed trope engine OTEL spans — draw + expire (Epic 22, Story 22-3).

Sibling of :mod:`sidequest.telemetry.spans.trope`. The macro-trope engine
fires :data:`SPAN_TROPE_ACTIVATE` / :data:`SPAN_TROPE_RESOLVE`; the
short-arc seed engine fires :data:`SPAN_SEED_DRAWN` per drawn seed (on
session bootstrap) and :data:`SPAN_SEED_EXPIRED` per migration from
``snapshot.active_seeds`` into ``snapshot.seed_ghosts``.

Both spans stay in :data:`FLAT_ONLY_SPANS` for now — typed Subsystems-tab
routing for the GM panel is 22-4's territory.
"""

from __future__ import annotations

from ._core import FLAT_ONLY_SPANS

SPAN_SEED_DRAWN = "seed.drawn"
SPAN_SEED_EXPIRED = "seed.expired"

FLAT_ONLY_SPANS.update(
    {
        SPAN_SEED_DRAWN,
        SPAN_SEED_EXPIRED,
    }
)

__all__ = [
    "SPAN_SEED_DRAWN",
    "SPAN_SEED_EXPIRED",
]
