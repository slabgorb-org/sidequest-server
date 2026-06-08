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

from pathlib import Path

import yaml

from sidequest.genre.models.world import CartographyConfig
from sidequest.server.asset_urls import resolve_asset_url
from sidequest.server.reference_map import _edges_and_dangling, _npc_pins, load_cartography_config
from sidequest.server.reference_presenters import portrait_image_key
from sidequest.server.reference_renderer import (
    EXCLUDED_FILES,
    LORE_WORLD_FILES,
    _gate_cast_slugs_on_manifest,
    _humanize_label,
    _is_devnote,
)
from sidequest.server.reference_slug import slugify
from sidequest.server.reference_visibility import Visibility, classify
from sidequest.telemetry.spans.reference import (
    reference_devnote_suppressed_span,
    reference_map_dangling_edge_span,
    reference_map_pin_not_found_span,
    reference_map_pin_resolved_span,
    reference_map_rendered_span,
    reference_unknown_field_span,
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


def _project_node(
    value: object,
    *,
    file_stem: str,
    key_path: tuple[str, ...],
    pack: str,
    world: str,
) -> dict | None:
    """Project a parsed-YAML node into a public-only node-tree dict, or ``None``.

    The data-shaping analog of ``render_node`` / ``_render_dict`` / ``_render_list``
    in ``reference_renderer.py``. The firewall is identical: ``classify()`` decides
    per ``key_path``, KEEPER children drop silently, UNKNOWN children drop and fire a
    WARN span, and leading-underscore keys + ``_is_devnote`` values/items are
    suppressed (with a devnote span). ``None`` is returned when nothing public
    survives so callers can omit empty containers.
    """
    if isinstance(value, dict):
        entries: list[dict] = []
        for key, child in value.items():
            child_path = key_path + (str(key),)

            # Renderer-layer suppressions (Story 63-9 parity): private keys and
            # dev-note marker values never reach the player-facing surface.
            if str(key).startswith("_") or _is_devnote(child):
                with reference_devnote_suppressed_span(
                    pack=pack, world=world, file_stem=file_stem, key_path=child_path
                ):
                    pass
                continue

            # Visibility firewall — classify() is the single gate.
            vis = classify(file_stem, child_path)
            if vis is Visibility.KEEPER:
                continue
            if vis is Visibility.UNKNOWN:
                with reference_unknown_field_span(
                    pack=pack, world=world, file_stem=file_stem, key_path=child_path
                ):
                    pass
                continue

            child_node = _project_node(
                child, file_stem=file_stem, key_path=child_path, pack=pack, world=world
            )
            if child_node is not None:
                entries.append(
                    {"key": str(key), "label": _humanize_label(key), "node": child_node}
                )
        if not entries:
            return None
        return {"type": "dict", "entries": entries}

    if isinstance(value, list):
        # List-of-dict items use the ('*',) wildcard segment so classify()
        # patterns like ('confrontations','*','beats','*','narrator_hint') resolve.
        items: list[dict] = []
        for item in value:
            if not isinstance(item, (dict, list)) and _is_devnote(item):
                with reference_devnote_suppressed_span(
                    pack=pack, world=world, file_stem=file_stem, key_path=key_path
                ):
                    pass
                continue
            item_path = key_path + ("*",) if isinstance(item, dict) else key_path
            item_node = _project_node(
                item, file_stem=file_stem, key_path=item_path, pack=pack, world=world
            )
            if item_node is not None:
                items.append(item_node)
        if not items:
            return None
        return {"type": "list", "items": items}

    return {"type": "scalar", "value": value}


def build_generic_yaml_section(
    data: object, *, file_stem: str, pack: str, world: str
) -> dict | None:
    """Project ONE parsed world YAML file into a public-only node-tree section.

    Returns ``{"id": slug(file_stem), "label": humanized_stem, "node": <node>}`` or
    ``None`` when nothing public survives the ``classify()`` firewall. The file root
    is classified at ``key_path == ()``: KEEPER → ``None`` (whole keeper file),
    UNKNOWN → ``None`` + a WARN span (content drift, No Silent Fallbacks).
    """
    vis_root = classify(file_stem, ())
    if vis_root is Visibility.KEEPER:
        return None
    if vis_root is Visibility.UNKNOWN:
        with reference_unknown_field_span(
            pack=pack, world=world, file_stem=file_stem, key_path=()
        ):
            pass
        return None

    node = _project_node(data, file_stem=file_stem, key_path=(), pack=pack, world=world)
    if node is None:
        return None
    return {"id": slugify(file_stem), "label": _humanize_label(file_stem), "node": node}


def build_lore_projection(pack: str, world: str, *, pack_dir: Path, world_dir: Path) -> dict:
    """Assemble the public-projected lore document. This slice emits the map
    section only; Cast/POI/Timeline/generic-YAML sections land in later slices.
    """
    sections: list[dict] = []

    cartography = load_cartography_config(world_dir)
    if cartography is not None and cartography.regions:
        map_npc_slugs = frozenset(
            slug for region in cartography.regions.values() for slug, _label in _npc_pins(region)
        )
        gated_map_slugs = _gate_cast_slugs_on_manifest(
            map_npc_slugs,
            pack=pack,
            world=world,
            pack_dir=pack_dir,
        )
        sections.append(
            build_lore_map_section(
                cartography, pack=pack, world=world, portrait_on_r2_slugs=gated_map_slugs
            )
        )

    # Generic-YAML sections — one per present LORE_WORLD_FILES file, AFTER the
    # map section. EXCLUDED_FILES (and file-root KEEPER stems) never project.
    for filename in LORE_WORLD_FILES:
        if filename in EXCLUDED_FILES:
            continue
        path = world_dir / filename
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if data is None:
            continue
        section = build_generic_yaml_section(
            data, file_stem=path.stem, pack=pack, world=world
        )
        if section is not None:
            sections.append(section)

    return {"schema_version": 1, "pack": pack, "world": world, "sections": sections}
