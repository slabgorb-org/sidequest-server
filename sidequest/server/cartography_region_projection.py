"""Cartography → RegionProjection builder (the frozen-Location-panel fix).

Procedural region-mode worlds (the beneath_sünden megadungeon) already
project the party's current region into a "YOU ARE HERE" narrator-prompt
section + MOVEMENT RULE, which is what makes the narrator emit a
``current_region`` patch and keeps the Location panel tracking the prose.
Cartography region-mode worlds (e.g. ``tea_and_murder/glenross``) had the
identical authored region graph in ``cartography.yaml`` but got NO
projection — ``dungeon.region_projection.applies_to`` is hard-gated to the
megadungeon — so the narrator was never handed the region ids and never
emitted ``current_region``. The panel froze on the chargen starting region.

This module is the reuse-first bridge: it builds the SAME
``RegionProjection`` shape from a world's cartography so it can ride the
SAME render. The dungeon-only fields (theme/register/motifs/depth_score)
are inert and ``is_dungeon`` stays False, which gates off the megadungeon
lethality directive in ``register_region_section``.

The projection is the move vocabulary: ``exits`` are the region's
``adjacent`` ids, the EXACT values the narrator must place in a
``current_region`` patch. A blank or unknown ``current_region`` returns
None rather than inventing a projection (No Silent Fallbacks).
"""

from __future__ import annotations

from sidequest.dungeon.region_projection import RegionExit, RegionProjection
from sidequest.genre.models.world import NavigationMode

# Cartography adjacency has no edge-kind taxonomy (unlike the dungeon graph's
# corridor/stairs/shaft). A neutral "path" keeps the render's
# "{kind} → {to_region_id}" line grammatical without inventing geography.
_CARTOGRAPHY_EXIT_KIND = "path"


def project_cartography_region(
    world_obj: object | None,
    current_region: str,
) -> RegionProjection | None:
    """Project ``current_region`` from a cartography region-mode world.

    Returns None (no projection, never a fabricated one) when:
      - ``world_obj`` is None or has no cartography
      - the world's navigation mode is not ``region``
      - ``current_region`` is blank or absent from the cartography regions

    None is the correct "this turn has no cartography projection" signal —
    the caller emits an observable span, never a silent skip.
    """
    if world_obj is None:
        return None
    cart = getattr(world_obj, "cartography", None)
    if cart is None:
        return None
    if getattr(cart, "navigation_mode", None) != NavigationMode.region:
        return None
    regions = getattr(cart, "regions", None) or {}
    if not current_region or current_region not in regions:
        return None

    region = regions[current_region]
    exits = [
        RegionExit(to_region_id=adjacent_id, kind=_CARTOGRAPHY_EXIT_KIND)
        for adjacent_id in region.adjacent
    ]
    return RegionProjection(
        region_id=current_region,
        # Cartography has no procedural theme palette — the human-facing
        # label is the region name, the flavor is its authored summary.
        theme_id="",
        theme_display=region.name,
        register="",
        flavor=region.summary,
        motifs=[],
        depth_score=None,
        exits=exits,
        is_dungeon=False,
    )
