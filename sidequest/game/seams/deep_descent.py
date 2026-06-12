"""The deep_descent seam resolver.

Extracts the 59-12 surface→deep bind (``sidequest/agents/subsystems/movement.py``)
behind the seam registry: bind THIS PC onto the procedural dungeon entrance node,
or raise ``SeamCrossingError`` (fail loud — never a silent stay-put).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sidequest.dungeon.seed_bootstrap import ENTRANCE_ID
from sidequest.game.seams.base import SeamCrossingError, SeamCrossingResult
from sidequest.game.session import WorldStatePatch
from sidequest.telemetry.spans import movement_resolved_span

if TYPE_CHECKING:
    from sidequest.dungeon.persistence import DungeonStore
    from sidequest.game.session import GameSnapshot
    from sidequest.genre.models.world import Route

logger = logging.getLogger(__name__)


def resolve_deep_descent(
    *,
    snapshot: GameSnapshot,
    player_name: str,
    route: Route,
    resolved_via: str,
    dungeon_store: DungeonStore | None = None,
    direction: str = "deeper",
    exit_descriptor: str = "",
    **_context: Any,
) -> SeamCrossingResult:
    """Bind THIS PC onto the dungeon entrance node, or raise."""
    from_region = snapshot.region_for(perspective=player_name) or ""
    if dungeon_store is None:
        raise SeamCrossingError(
            reason="no_dungeon_store",
            surface=(
                "The way down exists, but the deep beneath it has not "
                "been opened — this is a wiring fault, not a closed door."
            ),
        )
    graph = dungeon_store.load_map(entrance_id=ENTRANCE_ID)
    entrance_id = graph.entrance_id
    if entrance_id not in graph.nodes:
        raise SeamCrossingError(
            reason="no_dungeon_entrance",
            surface="The descent into the depths has not yet formed.",
        )
    snapshot.apply_world_patch(WorldStatePatch(pc_region={player_name: entrance_id}))
    with movement_resolved_span(
        pc_name=player_name,
        from_region=from_region,
        to_region=entrance_id,
    ) as span:
        span.set_attribute("intent.direction", direction)
        span.set_attribute("intent.exit_descriptor", exit_descriptor)
        span.set_attribute("resolved_via", resolved_via)
        span.set_attribute("seam_kind", str(route.to_id or ""))
        span.set_attribute("seam_route_name", route.name)
        span.set_attribute("candidate_exits", [entrance_id])
        span.set_attribute("edge_kind", "surface_descent")
        span.set_attribute("target_pre_materialized", True)
        span.set_attribute("materialize_triggered", True)
        span.set_attribute("party_split_after", snapshot.region_for() is None)
    logger.debug(
        "seam.crossing kind=%s pc=%s from=%s to=%s via=%s",
        route.to_id,
        player_name,
        from_region,
        entrance_id,
        resolved_via,
    )
    return SeamCrossingResult(to_region=entrance_id)
