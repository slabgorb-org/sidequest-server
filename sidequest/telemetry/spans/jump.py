"""OTEL spans for the campaign-scale inter-system jump subsystem (Story 98-5).

ADR-141 layers a galactic-graph **jump** (system → adjacent system) atop the
per-system orrery. Per the CLAUDE.md OTEL principle, every jump decision emits a
span so the GM panel can verify the crunch actually fired — fuel/transit/hazard
computed by the bound ruleset (ADR-117), not narrator prose claiming a hop.

Pattern mirrors sidequest/telemetry/spans/course.py.
"""

from __future__ import annotations

from ._core import FLAT_ONLY_SPANS
from .span import Span

SPAN_JUMP_ADJUDICATED = "jump.adjudicated"
SPAN_JUMP_DEFAULT_COST = "jump.default_cost"

FLAT_ONLY_SPANS.update(
    {
        SPAN_JUMP_ADJUDICATED,
        SPAN_JUMP_DEFAULT_COST,
    }
)


def emit_jump_adjudicated(
    *,
    from_region: str,
    to_region: str,
    fuel_spent: int,
    transit_days: int,
    hazard_roll: int,
) -> None:
    """Emit ``jump.adjudicated`` — the per-jump GM-panel record (AC5).

    Carries the five attributes the lie-detector needs: which systems the jump
    crossed, the fuel and transit it cost, and the rolled hazard check. Fired on
    every adjudicated jump regardless of whether the cost came from an authored
    route or the ruleset default."""
    with Span.open(
        SPAN_JUMP_ADJUDICATED,
        attrs={
            "from_region": from_region,
            "to_region": to_region,
            "fuel_spent": int(fuel_spent),
            "transit_days": int(transit_days),
            "hazard_roll": int(hazard_roll),
        },
    ):
        pass


def emit_jump_default_cost(
    *,
    from_region: str,
    to_region: str,
    fuel_spent: int,
    transit_days: int,
) -> None:
    """Emit ``jump.default_cost`` for an unrouted (bare-adjacency) jump (AC2).

    The explicit, logged record that an edge with no authored ``routes`` entry
    got a computed ruleset default — ``source=ruleset_default`` — not a silent
    zero (No Silent Fallbacks). Fired in addition to ``jump.adjudicated``."""
    with Span.open(
        SPAN_JUMP_DEFAULT_COST,
        attrs={
            "from_region": from_region,
            "to_region": to_region,
            "fuel_spent": int(fuel_spent),
            "transit_days": int(transit_days),
            "source": "ruleset_default",
        },
    ):
        pass
