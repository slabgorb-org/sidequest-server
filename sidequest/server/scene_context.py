"""Per-connection scene context: world (on cartography) vs site (inside a
site's graph) — Track B, Task 7 (story 164-4).

Replaces ``map_emit._descent_phase``'s beneath_sunden-hardcoded
``surface|deep`` binary so ANY site — a bounded tavern/vault with
``{site_id}:``-namespaced nodes, or the legacy Sünden frontier with bare
``entrance``/``expNNN.rN`` ids — can project its interior map. A connection
is in a site scene iff its PC's region is a node in some site's graph:

  - owner-namespaced node ids resolve purely through
    ``SiteRegistry.site_owning_node`` — no store probe;
  - the legacy frontier site resolves via a store membership check (B1
    Sünden; this branch dies with the B4 namespacing follow-up);
  - everything else (cartography region, unseated connection, missing
    ``pc_regions`` entry, no repo) is the world scene — never a crash.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from sidequest.game.sites import SiteRegistry

if TYPE_CHECKING:
    from sidequest.game.session import GameSnapshot


@dataclass(frozen=True)
class SceneContext:
    """Which map projection owns this connection's turn.

    ``("world", None)`` — the PC stands on the authored cartography; the
    cartography MAP_UPDATE owns the map. ``("site", site_id)`` — the PC is
    inside that site's graph; the SITE_MAP emit owns the map.
    """

    kind: Literal["world", "site"]
    site_id: str | None


def _cartography_for(sd: Any) -> Any:
    world = getattr(getattr(sd, "genre_pack", None), "worlds", {}).get(
        getattr(sd, "world_slug", "") or ""
    )
    return getattr(world, "cartography", None)


def site_registry_for(sd: Any) -> SiteRegistry:
    """The active world's ``SiteRegistry`` (inert for a siteless world)."""
    return SiteRegistry.from_cartography(_cartography_for(sd))


def resolve_scene_context(*, sd: Any, snapshot: GameSnapshot, player_id: str) -> SceneContext:
    """Resolve THIS connection's scene: world vs ``site:<site_id>``.

    Per-connection (§Q-map): reads the connection's own PC region via the
    same seat→perspective accessor the map emits use, so a split party (one
    PC on the surface, one in the deep) resolves a different scene each.
    """
    # Lazy import: map_emit imports this module (and sidequest.dungeon
    # depends on game models) — same lazy-import precedent as the rest of
    # the map-emit seams.
    from sidequest.dungeon.seed_bootstrap import ENTRANCE_ID
    from sidequest.server.websocket_handlers.map_emit import _resolve_connection_pc_region

    _pc_name, pc_region = _resolve_connection_pc_region(snapshot, player_id)
    if not pc_region:
        return SceneContext(kind="world", site_id=None)

    registry = site_registry_for(sd)
    owner = registry.site_owning_node(pc_region)
    if owner is not None:
        return SceneContext(kind="site", site_id=owner.site_id)

    # Legacy frontier (B1 Sünden): bare node ids can't be owner-resolved, so
    # membership comes from the store. Bounded sites never reach this loop —
    # their nodes are namespaced and resolved above.
    repo = getattr(sd, "dungeon_repository", None)
    if repo is not None:
        for site in registry.frontier_sites():
            graph = repo.load_map(entrance_id=ENTRANCE_ID, site_id=site.site_id)
            if pc_region in graph.nodes:
                return SceneContext(kind="site", site_id=site.site_id)

    return SceneContext(kind="world", site_id=None)
