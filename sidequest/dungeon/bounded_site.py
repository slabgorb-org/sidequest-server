"""Bounded-site materialization — whole graph + grids in ONE transaction.

A ``bounded`` site (a tavern, a vault) is materialized WHOLE on first entry:
its entrance plus every room, in the single committed transaction ``materialize``
owns, deterministic from ``blake2b(campaign_seed, site_id)``. Unlike the frontier
megadungeon there is no lookahead worker — ``lookahead_breadth=0`` leaves no open
frontier edges, so the site never grows past its archetype's room budget.

Track B, plan task 11. Consumed by the movement dispatch (task 6/12): on
``enter_site`` for a ``bounded`` extent, the dispatcher calls
``ensure_bounded_site_materialized`` before resolving the crossing.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from sidequest.dungeon.materializer import MaterializationRequest, materialize
from sidequest.dungeon.persistence import FrontierEdge
from sidequest.dungeon.region_graph.model import RegionGraph, RegionNode
from sidequest.dungeon.seed_bootstrap import select_entrance_theme_id
from sidequest.game.seams.base import SeamCrossingError
from sidequest.game.sites.namespacing import site_entrance_id
from sidequest.telemetry.spans.site import (
    site_materialize_begin_span,
    site_materialize_commit_span,
    site_materialize_skip_span,
)

if TYPE_CHECKING:
    from sidequest.game.repository import DungeonRepository
    from sidequest.game.sites.models import SiteDescriptor
    from sidequest.genre.models.site_archetype import SiteArchetype

# The entrance push-off depth for a bounded site's first (and only) expansion —
# the same root depth the frontier bootstrap uses (seed_bootstrap._ENTRANCE_DEPTH).
_ENTRANCE_DEPTH = 0.0
# The campaign_seed column is a signed BIGINT and the frontier bootstrap draws
# secrets.randbits(63); the derived site seed is masked to the same 63-bit
# positive range so it never overflows the column.
_SEED_BITS = 63


def _derive_site_seed(*, base_seed: int, site_id: str) -> int:
    """A deterministic per-site seed folded from the session's base campaign
    seed and the site_id — so re-entry reproduces the same site with no random
    draw (blake2b, the canonical subseed pattern; never an XOR). Masked to 63
    bits to fit the signed BIGINT campaign_seed column."""
    digest = hashlib.blake2b(f"{base_seed}|{site_id}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") & ((1 << _SEED_BITS) - 1)


async def ensure_bounded_site_materialized(
    *,
    site: SiteDescriptor,
    archetype: SiteArchetype,
    dungeon_repository: DungeonRepository | None,
    snapshot: Any,
    pack: Any,
    bundle: Any,
    palette: Any,
) -> None:
    """Materialize ``site`` whole on first entry; a no-op on re-entry.

    Idempotent: returns early (emitting ``site.materialize.skip``) if the site's
    entrance node is already committed. Fail-loud: a missing dungeon store raises
    ``SeamCrossingError`` (No Silent Fallbacks) rather than opening a hollow site.
    """
    if dungeon_repository is None:
        # No Silent Fallbacks: a bounded site with no store cannot be opened —
        # surface it, never a quiet no-op that strands the player at the door.
        raise SeamCrossingError(reason="no_site_store", surface=f"{site.name} cannot be opened.")

    entrance = site_entrance_id(site.site_id)
    existing = dungeon_repository.load_map(entrance_id=entrance, site_id=site.site_id)
    if entrance in existing.nodes:
        with site_materialize_skip_span(site_id=site.site_id, archetype=site.archetype):
            pass
        return

    # Deterministic per-site seed: reuse a committed one, else fold the session
    # base seed with site_id and persist it (write-once) so re-entry is stable.
    seed = dungeon_repository.get_campaign_seed(site_id=site.site_id)
    if seed is None:
        base = dungeon_repository.get_campaign_seed() or 0
        seed = _derive_site_seed(base_seed=base, site_id=site.site_id)
        dungeon_repository.set_campaign_seed(seed, site_id=site.site_id)

    with site_materialize_begin_span(site_id=site.site_id, archetype=site.archetype) as span:
        span.set_attribute("seed", seed)
        span.set_attribute("room_count_max", archetype.room_count_max)

    # Seed graph: just the entrance node at expansion 0. The one committed
    # transaction inside materialize() seeds it (Expansion 0) then the whole
    # room burst (expansion 1) — a bounded burst = room_count_max with
    # lookahead_breadth=0, so no frontier edges survive.
    entrance_theme = select_entrance_theme_id(palette)
    seed_graph = RegionGraph(entrance_id=entrance)
    seed_graph.add_node(RegionNode(id=entrance, expansion_id=0, theme=entrance_theme))

    fe = FrontierEdge(
        frontier_edge_id=f"{site.site_id}:seed_fe1",
        from_region_id=entrance,
        heading="in",
        spawn_depth_score=_ENTRANCE_DEPTH,
    )
    request = MaterializationRequest.build(
        campaign_seed=seed,
        expansion_id=1,
        frontier_edge=fe,
        frontier=[fe],
        attach_region_ids=[entrance],
        heading="in",
        burst_magnitude=archetype.room_count_max,
        lookahead_breadth=0,
        site_id=site.site_id,
    )
    await materialize(
        request,
        graph=seed_graph,
        bundle=bundle,
        palette=palette,
        dungeon_repository=dungeon_repository,
        snapshot=snapshot,
        pack_tropes=pack,
        pack=pack,
    )

    committed = dungeon_repository.load_map(entrance_id=entrance, site_id=site.site_id)
    with site_materialize_commit_span(site_id=site.site_id, archetype=site.archetype) as span:
        span.set_attribute("node_count", len(committed.nodes))
