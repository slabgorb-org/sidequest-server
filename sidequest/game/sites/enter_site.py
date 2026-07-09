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
    entrance_node = site.entrance_node_id
    if entrance_node not in graph.nodes:
        # Frontier-legacy fallback (Track B Task 6): the frontier site declares a
        # namespaced entrance ('frontier:entrance'), but the bootstrapped Sünden
        # store keyed its graph on the bare ENTRANCE_ID ('entrance'). Bind to the
        # graph's REAL entrance when the declared node is absent — a single LOUD
        # fallback, correct for the frontier-legacy case and harmless for bounded
        # sites (whose entrance IS the namespaced id, so this branch never fires).
        # Full node-id namespacing is a B4 follow-up; storage isolation is already
        # (session, site_id)-keyed, so the node id need not be namespaced for B1.
        if graph.entrance_id and graph.entrance_id in graph.nodes:
            entrance_node = graph.entrance_id
        else:
            raise SeamCrossingError(
                reason="no_site_entrance",
                surface=f"The interior of {site.name} has not yet formed.",
            )
    snapshot.apply_world_patch(WorldStatePatch(pc_region={player_name: entrance_node}))
    with site_enter_span(
        pc_name=player_name, site_id=site.site_id, from_region=from_region
    ) as span:
        span.set_attribute("to_region", entrance_node)
        span.set_attribute("resolved_via", resolved_via)
        span.set_attribute("extent", site.extent)
        span.set_attribute("archetype", site.archetype)
        # Coarse player intent (Story 164-3): what the player did to cross, the way
        # movement.resolved stamps it — so the GM panel sees the intent, not just
        # the outcome.
        span.set_attribute("intent.direction", direction)
        span.set_attribute("intent.exit_descriptor", exit_descriptor)
    logger.debug(
        "site.enter pc=%s site=%s from=%s to=%s via=%s",
        player_name,
        site.site_id,
        from_region,
        entrance_node,
        resolved_via,
    )
    return SeamCrossingResult(to_region=entrance_node)
