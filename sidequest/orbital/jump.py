"""Inter-system jump glue (Story 98-5, ADR-141 campaign scale).

The campaign-scale **jump** moves the party from one star system node to an
adjacent one on the cartography graph. This module is the production seam the
movement path reaches: it resolves the authored ``routes`` entry for an edge
(if any), adjudicates the cost through the *bound* ruleset (ADR-117), and emits
the GM-panel OTEL spans.

Pure-ish module: ``resolve_route_for_jump`` has no I/O;
``adjudicate_inter_system_jump`` only reads the ruleset registry and emits
spans. Deliberately separate from ``orbital/course.py`` — the intra-system
Hohmann course model (ADR-130) is a different movement scale and is never
invoked here (Story 98-5 AC4).
"""

from __future__ import annotations

import logging
import random

from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.ruleset.resolution import JumpAdjudication
from sidequest.genre.models.world import CartographyConfig, Route
from sidequest.telemetry.spans.jump import emit_jump_adjudicated, emit_jump_default_cost

logger = logging.getLogger(__name__)


def _endpoints_adjacent(carto: CartographyConfig, a: str, b: str) -> bool:
    """True iff ``a`` and ``b`` are mutually declared adjacency nodes.

    Both must be region keys and each must list the other in its ``adjacent``
    set. ``adjacent`` is the connectivity graph; a ``routes`` entry only
    *annotates* an existing edge — it never creates one (epic 98 §3)."""
    region_a = carto.regions.get(a)
    region_b = carto.regions.get(b)
    if region_a is None or region_b is None:
        return False
    return b in region_a.adjacent and a in region_b.adjacent


def resolve_route_for_jump(
    carto: CartographyConfig, from_region: str, to_region: str
) -> Route | None:
    """Return the authored ``routes`` entry annotating the ``from_region`` ↔
    ``to_region`` edge, or None when the adjacency is unrouted.

    Undirected match on endpoints. A bare (unrouted) adjacency is navigable —
    the caller falls to the ruleset default — so None is a normal result, not an
    error. A ``routes`` entry whose endpoints are NOT a real adjacency is a
    route-level anomaly: it is dropped with a WARNING and never promoted to
    connectivity (No Silent Fallbacks; epic 98 §3)."""
    match: Route | None = None
    for route in carto.routes:
        if route.from_id is None or route.to_id is None:
            continue  # an unanchored route annotates no specific edge
        if not _endpoints_adjacent(carto, route.from_id, route.to_id):
            logger.warning(
                "jump.route_anomaly route=%r endpoints %s<->%s are not a declared "
                "adjacency; dropping (a route annotates an edge, it does not create one)",
                route.name,
                route.from_id,
                route.to_id,
            )
            continue
        if {route.from_id, route.to_id} == {from_region, to_region}:
            match = route
    return match


def adjudicate_inter_system_jump(
    *,
    cartography: CartographyConfig,
    from_region: str,
    to_region: str,
    ruleset: str,
    drive_rating: int,
    rng: random.Random,
) -> JumpAdjudication:
    """Adjudicate one inter-system jump end-to-end and emit its spans.

    Resolves the authored route for the edge (if any), adjudicates the cost
    through the *bound* ruleset module (``get_ruleset_module(ruleset)`` — the
    ADR-117 seam, never a hard-coded SWN import), and fires ``jump.adjudicated``.
    An unrouted edge additionally fires the explicit ``jump.default_cost`` span.
    Returns the adjudication so the movement caller can apply fuel/transit."""
    route = resolve_route_for_jump(cartography, from_region, to_region)
    module = get_ruleset_module(ruleset)
    result = module.adjudicate_jump(route=route, drive_rating=drive_rating, rng=rng)

    emit_jump_adjudicated(
        from_region=from_region,
        to_region=to_region,
        fuel_spent=result.fuel_spent,
        transit_days=result.transit_days,
        hazard_roll=result.hazard_roll,
    )
    if result.source == "ruleset_default":
        emit_jump_default_cost(
            from_region=from_region,
            to_region=to_region,
            fuel_spent=result.fuel_spent,
            transit_days=result.transit_days,
        )
    return result
