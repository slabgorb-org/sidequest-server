"""Story 65-11 — lore-page Map section: a server-rendered SVG node-link graph.

The lore reference page (``GET /reference/lore/{pack}/{world}``, ADR-135 public
projection) gains a **Map** section: an inline SVG graph built from the world's
``cartography.yaml``. Regions are nodes, each region's ``adjacent`` list is an
edge, and npc-binding location entities (``entities[].binding.kind == "npc"``)
are portrait pins gated on R2 presence the same way the 65-9 Cast section gates
portraits.

Cartography has **no coordinates** (every world is ``navigation_mode: region``
with only an adjacency list), so the layout is computed deterministically from
the graph: a breadth-first walk seeded at ``starting_region`` with neighbours
visited in sorted-id order, laid out in depth layers. The same cartography
always renders byte-identical SVG, independent of YAML key order.

This module owns only the new code (the loader, the deterministic layout, and
the SVG/HTML emission). The portrait gate (``load_r2_manifest_keys`` +
``portrait_image_key``), the slug rule (``slugify_player_name``), the asset-URL
seam (``resolve_asset_url``), and the section/TOC append in ``assemble_lore_page``
are reused, not rebuilt.
"""

from __future__ import annotations

from collections import deque
from html import escape
from pathlib import Path

import yaml
from pydantic import ValidationError

from sidequest.genre.models.world import CartographyConfig, Region
from sidequest.server.asset_urls import resolve_asset_url
from sidequest.server.reference_presenters import portrait_image_key
from sidequest.server.utils import slugify_player_name
from sidequest.telemetry.spans.reference import (
    reference_map_dangling_edge_span,
    reference_map_pin_not_found_span,
    reference_map_pin_resolved_span,
    reference_map_rendered_span,
)

# Layout geometry. Pure constants — no coordinates exist in cartography, so the
# graph is laid out in depth layers (column = BFS depth, row = order within the
# layer). Values are arbitrary but fixed, which keeps the SVG deterministic.
_MARGIN = 48
_COL_W = 220
_ROW_H = 132
_NODE_R = 10
_PIN = 36  # portrait pin box edge


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


def _layout_order(cart: CartographyConfig) -> tuple[list[str], dict[str, int]]:
    """Deterministic node order + depth, BFS-seeded at ``starting_region``.

    Neighbours are visited in sorted-id order, so the ordering is independent of
    YAML key order. Regions not reachable from the start (or all regions, when
    ``starting_region`` is unset/invalid) are appended in sorted-id order.
    """
    regions = cart.regions
    order: list[str] = []
    depth: dict[str, int] = {}
    seen: set[str] = set()
    queue: deque[str] = deque()

    start = cart.starting_region
    if start in regions:
        seen.add(start)
        depth[start] = 0
        queue.append(start)

    while queue:
        cur = queue.popleft()
        order.append(cur)
        for nb in sorted(regions[cur].adjacent):
            if nb in regions and nb not in seen:
                seen.add(nb)
                depth[nb] = depth[cur] + 1
                queue.append(nb)

    for rid in sorted(regions):
        if rid not in seen:
            seen.add(rid)
            depth[rid] = 0
            order.append(rid)

    return order, depth


def _positions(order: list[str], depth: dict[str, int]) -> dict[str, tuple[int, int]]:
    """Place each node at (x = depth column, y = row within its depth layer)."""
    pos: dict[str, tuple[int, int]] = {}
    rows: dict[int, int] = {}
    for rid in order:
        d = depth[rid]
        row = rows.get(d, 0)
        pos[rid] = (_MARGIN + d * _COL_W, _MARGIN + row * _ROW_H)
        rows[d] = row + 1
    return pos


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


def present_lore_map(
    cart: CartographyConfig,
    *,
    pack: str,
    world: str,
    portrait_on_r2_slugs: frozenset[str],
) -> str:
    """Render the Map ``<section>`` (heading + inline SVG), or "" if no regions.

    Emits the OTEL decision spans the GM/dev panel reads: one ``map_rendered``
    summary, a per-pin ``map_pin_resolved`` / ``map_pin_not_found``, and a
    ``map_dangling_edge`` WARN per dropped adjacency.
    """
    regions = cart.regions
    if not regions:
        return ""

    order, depth = _layout_order(cart)
    pos = _positions(order, depth)
    edges, dangling = _edges_and_dangling(cart)

    for source, missing in dangling:
        with reference_map_dangling_edge_span(source_region=source, dangling_region=missing):
            pass

    width = _MARGIN * 2 + (max(depth.values()) if depth else 0) * _COL_W + _PIN
    rows_per_depth: dict[int, int] = {}
    for rid in order:
        rows_per_depth[depth[rid]] = rows_per_depth.get(depth[rid], 0) + 1
    height = _MARGIN * 2 + (max(rows_per_depth.values()) - 1) * _ROW_H + _PIN

    parts: list[str] = []
    parts.append('<section id="map" class="ref-map">')
    parts.append("<h2>Map</h2>")
    parts.append(
        f'<svg class="ref-map__svg" viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg" role="img" aria-label="World region map">'
    )

    # Edges first so nodes paint over them (plain <g> wrapper — no class needed).
    parts.append("<g>")
    for a, b in edges:
        ax, ay = pos[a]
        bx, by = pos[b]
        parts.append(
            f'<line class="ref-map__edge" data-edge="{escape(f"{a}--{b}")}" '
            f'x1="{ax}" y1="{ay}" x2="{bx}" y2="{by}" />'
        )
    parts.append("</g>")

    # Nodes (in deterministic BFS order) with their npc pins.
    npc_pin_count = 0
    resolved_pin_count = 0
    parts.append("<g>")
    for rid in order:
        x, y = pos[rid]
        region = regions[rid]
        parts.append(
            f'<g class="ref-map__node" data-region-id="{escape(rid)}" '
            f'transform="translate({x},{y})">'
        )
        parts.append(f'<circle r="{_NODE_R}" />')
        parts.append(f'<text x="{_NODE_R + 6}" y="4">{escape(region.name)}</text>')

        pin_y = _NODE_R + 4
        for slug, label in _npc_pins(region):
            npc_pin_count += 1
            parts.append(f'<g class="ref-map__pin" data-npc-slug="{escape(slug)}">')
            if slug in portrait_on_r2_slugs:
                resolved_pin_count += 1
                src = resolve_asset_url(portrait_image_key(pack, world, slug))
                with reference_map_pin_resolved_span(slug=slug, region=rid):
                    pass
                parts.append(
                    f'<foreignObject x="{-_PIN // 2}" y="{pin_y}" width="{_PIN}" height="{_PIN}">'
                    f'<img xmlns="http://www.w3.org/1999/xhtml" '
                    f'src="{escape(src)}" alt="{escape(label)}" loading="lazy" />'
                    f"</foreignObject>"
                )
            else:
                with reference_map_pin_not_found_span(slug=slug, region=rid):
                    pass
                parts.append(f'<circle cy="{pin_y + _PIN // 2}" r="{_NODE_R - 3}" />')
            parts.append("</g>")
            pin_y += _PIN + 4
        parts.append("</g>")
    parts.append("</g>")
    parts.append("</svg>")
    parts.append("</section>")

    with reference_map_rendered_span(
        node_count=len(order),
        edge_count=len(edges),
        npc_pin_count=npc_pin_count,
        resolved_pin_count=resolved_pin_count,
    ):
        pass

    return "".join(parts)
