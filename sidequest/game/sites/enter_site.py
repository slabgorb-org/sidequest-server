"""enter_site seam resolver — bind THIS PC onto a site's entrance node."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sidequest.game.seams.base import SeamCrossingError, SeamCrossingResult
from sidequest.game.session import WorldStatePatch
from sidequest.telemetry.spans.site import site_enter_span

if TYPE_CHECKING:
    from sidequest.game.repository import DungeonRepository
    from sidequest.game.session import GameSnapshot
    from sidequest.game.sites import SiteDescriptor

logger = logging.getLogger(__name__)


def resolve_enter_site(
    *,
    snapshot: GameSnapshot,
    player_name: str,
    site: SiteDescriptor,
    dungeon_repository: DungeonRepository | None = None,
    resolved_via: str = "site_enter",
    direction: str = "",
    exit_descriptor: str = "",
    **_context: Any,
) -> SeamCrossingResult:
    """Bind THIS PC onto ``site.entrance_node_id``, or raise SeamCrossingError.

    The entrance node is expected to already exist — a frontier site (Sünden)
    was bootstrapped at connect; bounded materialization (Task 12) runs BEFORE
    this resolver. This resolver only binds + emits; it never generates. Both
    fault paths fail LOUD (recoverable) rather than stranding the PC in a
    phantom node.
    """
    from_region = snapshot.region_for(perspective=player_name) or ""
    if dungeon_repository is None:
        raise SeamCrossingError(
            reason="no_site_store",
            surface=(
                f"The way into {site.name} exists, but its interior has not "
                "been opened — a wiring fault, not a closed door."
            ),
        )
    graph = dungeon_repository.load_map(entrance_id=site.entrance_node_id, site_id=site.site_id)
    if site.entrance_node_id not in graph.nodes:
        raise SeamCrossingError(
            reason="no_site_entrance",
            surface=f"The interior of {site.name} has not yet formed.",
        )
    snapshot.apply_world_patch(WorldStatePatch(pc_region={player_name: site.entrance_node_id}))
    with site_enter_span(
        pc_name=player_name, site_id=site.site_id, from_region=from_region
    ) as span:
        span.set_attribute("to_region", site.entrance_node_id)
        span.set_attribute("resolved_via", resolved_via)
        span.set_attribute("extent", site.extent)
        span.set_attribute("archetype", site.archetype)
    logger.debug(
        "site.enter pc=%s site=%s from=%s to=%s via=%s",
        player_name,
        site.site_id,
        from_region,
        site.entrance_node_id,
        resolved_via,
    )
    return SeamCrossingResult(to_region=site.entrance_node_id)
