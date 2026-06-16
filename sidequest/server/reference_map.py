"""Lore-page Map section — cartography graph *data* builders.

The lore reference page's **Map** section is built from the world's
``cartography.yaml``: regions are nodes, each region's ``adjacent`` list is an
edge, and npc-binding location entities (``entities[].binding.kind == "npc"``)
are portrait pins.

Story 100-12 (Phase 4 cutover) retired the server-side SVG emitter
(``present_lore_map``) and the depth-layer layout — node positions are now a
client concern (the SPA's d3-dag map). What survives here are the pure graph
*data* helpers ``reference_projection.py`` imports: the ``cartography.yaml``
loader, the de-duplicated edge / dangling-ref builder, and the npc-pin extractor.
Cartography carries **no coordinates** (every world is ``navigation_mode: region``
with only an adjacency list), so this module emits topology only — never markup.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from sidequest.genre.models.world import CartographyConfig, Region
from sidequest.server.utils import slugify_player_name


def load_cartography_config(world_dir: Path) -> CartographyConfig | None:
    """Load ``world_dir/cartography.yaml`` into a ``CartographyConfig``.

    Returns ``None`` when the world authors no cartography (the Map section is
    purely additive — a world without it renders unchanged). Fails **loud** on a
    malformed file (No Silent Fallbacks): a YAML syntax error or a shape pydantic
    rejects raises ``ValueError``, which the lore route converts to HTTP 500
    rather than serving a silently map-less page.
    """
    path = world_dir / "cartography.yaml"
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as fh:
        try:
            data = yaml.safe_load(fh)
        except yaml.YAMLError as exc:
            raise ValueError(f"cartography.yaml: malformed YAML: {exc}") from exc
    if data is None:
        return None
    try:
        return CartographyConfig.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"cartography.yaml: invalid shape: {exc}") from exc


def _edges_and_dangling(
    cart: CartographyConfig,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Return (de-duplicated valid edges sorted, dangling (source, missing) refs).

    An adjacency to a region id that is not in ``cartography.regions`` is dropped
    from the edge set and reported as dangling (caller emits a WARN span).
    Reciprocal adjacency (A lists B and B lists A) collapses to one edge via a
    sorted-endpoint key.
    """
    regions = cart.regions
    edge_set: set[tuple[str, str]] = set()
    dangling: list[tuple[str, str]] = []
    for rid in sorted(regions):
        for nb in regions[rid].adjacent:
            if nb not in regions:
                dangling.append((rid, nb))
                continue
            edge_set.add(tuple(sorted((rid, nb))))  # type: ignore[arg-type]
    return sorted(edge_set), dangling


def _npc_pins(region: Region) -> list[tuple[str, str]]:
    """The (slug, label) for each npc-binding entity in a region.

    Only ``binding.kind == "npc"`` entities pin (ADR-135 public-only: flavor_only
    and other binding kinds are not exposed on the public map). The portrait slug
    is ``slugify_player_name(entity.label)`` — the SAME rule the Cast section uses
    for ``portrait_manifest`` names, so a map pin and a Cast card resolve the same
    R2 portrait key by construction (see Dev Assessment AC5 note).
    """
    pins: list[tuple[str, str]] = []
    for ent in region.entities:
        if ent.binding is not None and ent.binding.kind == "npc":
            slug = slugify_player_name(ent.label)
            if slug:
                pins.append((slug, ent.label))
    return pins
