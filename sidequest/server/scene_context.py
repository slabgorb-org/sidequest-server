"""Per-connection scene context: world (on the cartography) vs site (inside a
site's graph).

Replaces ``map_emit._descent_phase``'s beneath_sunden-only surface|deep binary
(Track B, Task 7 — story 164-4): ANY world may declare sites, and exactly one
map projection owns each connection's turn — the cartography MAP_UPDATE for
the world scene, the SITE_MAP frame for a site scene.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from sidequest.game.sites.registry import SiteRegistry

if TYPE_CHECKING:
    from sidequest.game.session import GameSnapshot
    from sidequest.genre.models.world import CartographyConfig
    from sidequest.server.session_state import _SessionData

__all__ = ["SceneContext", "cartography_for", "resolve_scene_context"]


@dataclass(frozen=True)
class SceneContext:
    """Which map frame owns this connection's turn.

    Frozen: a resolved scene is a read-only per-turn snapshot handed across
    the map-emit layers — mutating it would desync arbitration mid-turn
    (same invariant as ``SiteDescriptor``).
    """

    kind: Literal["world", "site"]
    site_id: str | None


def cartography_for(sd: _SessionData) -> CartographyConfig | None:
    """The bound world's cartography (``None`` for an unbound/duck-typed
    session or a world without one)."""
    world = getattr(getattr(sd, "genre_pack", None), "worlds", {}).get(
        getattr(sd, "world_slug", "") or ""
    )
    return getattr(world, "cartography", None)


def resolve_scene_context(
    *, sd: _SessionData, snapshot: GameSnapshot, player_id: str
) -> SceneContext:
    """A connection is in a site scene iff its PC's region is a node in some
    site's graph.

    Owner-namespaced nodes (``gilded_boar:r2``) resolve via the registry
    namespace alone — no store IO. The legacy un-namespaced frontier ids
    (``entrance``/``expNNN.rN``, the pre-namespacing Sünden deep — story
    164-3 migration) resolve via frontier-site store membership. Everything
    else — cartography regions, unseated connections, site-less worlds,
    nodes of an undeclared namespace — is the world scene.
    """
    # Lazy import: map_emit imports this module at its top; the PC-region
    # resolver stays single-sourced in map_emit (OP1 semantics live there).
    from sidequest.server.websocket_handlers.map_emit import _resolve_connection_pc_region

    _pc_name, pc_region = _resolve_connection_pc_region(snapshot, player_id)
    if not pc_region:
        return SceneContext(kind="world", site_id=None)

    registry = SiteRegistry.from_cartography(cartography_for(sd))
    owner = registry.site_owning_node(pc_region)
    if owner is not None:
        return SceneContext(kind="site", site_id=owner.site_id)

    if ":" not in pc_region:
        # Legacy un-namespaced deep ids predate site namespacing; their
        # membership lives only in the frontier site's stored graph. A
        # namespaced id of an UNDECLARED site never reaches here — an unknown
        # namespace must not fabricate a site scene.
        repo = getattr(sd, "dungeon_repository", None)
        if repo is not None:
            for site in registry.frontier_sites():
                graph = repo.load_map(entrance_id=site.entrance_node_id, site_id=site.site_id)
                if pc_region in graph.nodes:
                    return SceneContext(kind="site", site_id=site.site_id)

    return SceneContext(kind="world", site_id=None)
