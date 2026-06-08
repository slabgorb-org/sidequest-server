"""Public-projected JSON for the reference lore page (React-rendered surface).

The server keeps the *projection* job — deciding what public data exists — and
emits JSON. React renders it. This module is the serializer; it holds no HTTP and
no HTML. The map section emits graph *topology only* (regions, edges, npc pins,
dangling refs); node positions are a client concern (d3-dag), so nothing here
computes coordinates. The pure graph helpers in ``reference_map.py``
(``_edges_and_dangling``, ``_npc_pins``) are reused as data builders; only the
SVG emission stays behind in that module.
"""

from __future__ import annotations

from sidequest.genre.models.world import CartographyConfig
from sidequest.server.asset_urls import resolve_asset_url
from sidequest.server.reference_map import _edges_and_dangling, _npc_pins
from sidequest.server.reference_presenters import portrait_image_key
from sidequest.telemetry.spans.reference import (
    reference_map_dangling_edge_span,
    reference_map_pin_not_found_span,
    reference_map_pin_resolved_span,
    reference_map_rendered_span,
)


def build_lore_map_section(
    cart: CartographyConfig,
    *,
    pack: str,
    world: str,
    portrait_on_r2_slugs: frozenset[str],
) -> dict:
    """Project ``cartography`` into the public ``map`` section dict.

    Fires the reference map spans at projection time (the decision point): one
    ``map_rendered`` census, a per-pin ``pin_resolved`` / ``pin_not_found``, and a
    ``dangling_edge`` WARN per dropped adjacency.
    """
    edges, dangling = _edges_and_dangling(cart)
    for source, missing in dangling:
        with reference_map_dangling_edge_span(source_region=source, dangling_region=missing):
            pass

    npc_pin_count = 0
    resolved_pin_count = 0
    regions: list[dict] = []
    for rid in sorted(cart.regions):
        region = cart.regions[rid]
        pins: list[dict] = []
        for slug, label in _npc_pins(region):
            npc_pin_count += 1
            if slug in portrait_on_r2_slugs:
                resolved_pin_count += 1
                with reference_map_pin_resolved_span(slug=slug, region=rid):
                    pass
                portrait_url: str | None = resolve_asset_url(portrait_image_key(pack, world, slug))
            else:
                with reference_map_pin_not_found_span(slug=slug, region=rid):
                    pass
                portrait_url = None
            pins.append({"slug": slug, "label": label, "portrait_url": portrait_url})
        regions.append(
            {"id": rid, "name": region.name, "adjacent": list(region.adjacent), "pins": pins}
        )

    with reference_map_rendered_span(
        node_count=len(regions),
        edge_count=len(edges),
        npc_pin_count=npc_pin_count,
        resolved_pin_count=resolved_pin_count,
    ):
        pass

    return {
        "id": "map",
        "label": "Map",
        "starting_region": cart.starting_region,
        "regions": regions,
        "edges": [list(e) for e in edges],
        "dangling": [list(d) for d in dangling],
    }
