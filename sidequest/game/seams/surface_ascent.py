"""The surface_ascent seam resolver — the reverse of deep_descent (Story 105-3).

When a PC stands on the dungeon entrance node (the static→procedural threshold,
seen from below) and intends to leave, bind THIS PC back to the surface
cartography region that OWNS the deep crossing — the seam route's ``from_id`` —
via the same per-PC patch path the descent uses, or raise ``SeamCrossingError``
(fail loud — never a silent stay-put).

The seam is ONE bidirectional route at the threshold: ``seam_kind`` is unchanged
(``deep_descent``, the route's ``to_id``); ``resolved_via`` ("surface_ascent")
is the direction discriminator. The GM panel reads one seam, two directions.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sidequest.game.seams.base import SeamCrossingError, SeamCrossingResult
from sidequest.game.session import WorldStatePatch
from sidequest.telemetry.spans import movement_resolved_span

if TYPE_CHECKING:
    from sidequest.game.session import GameSnapshot
    from sidequest.genre.models.world import CartographyConfig, Route

logger = logging.getLogger(__name__)


def resolve_surface_ascent(
    *,
    snapshot: GameSnapshot,
    player_name: str,
    route: Route,
    resolved_via: str = "surface_ascent",
    direction: str = "",
    exit_descriptor: str = "",
    cartography: CartographyConfig | None = None,
    **_context: Any,
) -> SeamCrossingResult:
    """Bind THIS PC back to the surface owner region (``route.from_id``), or raise."""
    from_region = snapshot.region_for(perspective=player_name) or ""
    surface_id = route.from_id
    if not surface_id:
        raise SeamCrossingError(
            reason="no_surface_owner",
            surface=(
                "There is a way up from here, but the surface it returns to "
                "has not been mapped — this is a wiring fault, not a sealed shaft."
            ),
        )
    # Symmetric to the descent's ``entrance_id in graph.nodes`` guard
    # (deep_descent.py): never bind the PC to a surface region that isn't on the
    # map. A registered-kind route whose ``from_id`` names an unmapped region is
    # a wiring fault — fail loud (the caller routes this to ``movement.unresolved``),
    # never strand the PC in a phantom region. When no cartography is supplied we
    # cannot verify membership, so we fail loud rather than bind blindly.
    regions = getattr(cartography, "regions", None) or {}
    if surface_id not in regions:
        raise SeamCrossingError(
            reason="dangling_surface_owner",
            surface=(
                "There is a way up from here, but it returns to a place that "
                "isn't on the map — this is a wiring fault, not a sealed shaft."
            ),
        )
    snapshot.apply_world_patch(WorldStatePatch(pc_region={player_name: surface_id}))
    with movement_resolved_span(
        pc_name=player_name,
        from_region=from_region,
        to_region=surface_id,
    ) as span:
        span.set_attribute("intent.direction", direction)
        span.set_attribute("intent.exit_descriptor", exit_descriptor)
        span.set_attribute("resolved_via", resolved_via)
        # Same bidirectional route as the descent — kind is unchanged.
        span.set_attribute("seam_kind", str(route.to_id or ""))
        span.set_attribute("seam_route_name", route.name)
        span.set_attribute("candidate_exits", [surface_id])
        span.set_attribute("edge_kind", "surface_ascent")
        span.set_attribute("target_pre_materialized", True)
        span.set_attribute("materialize_triggered", True)
        span.set_attribute("party_split_after", snapshot.region_for() is None)
    logger.debug(
        "seam.crossing kind=%s pc=%s from=%s to=%s via=%s (ascent)",
        route.to_id,
        player_name,
        from_region,
        surface_id,
        resolved_via,
    )
    return SeamCrossingResult(to_region=surface_id)
