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

from sidequest.genre.models.legends import Legend
from sidequest.genre.models.world import CartographyConfig
from sidequest.server.asset_urls import resolve_asset_url
from sidequest.server.reference_map import _edges_and_dangling, _npc_pins, load_cartography_config
from sidequest.server.reference_presenters import (
    cast_portrait_slug,
    poi_image_key,
    portrait_image_key,
)
from sidequest.server.reference_renderer import (
    EXCLUDED_FILES,
    LORE_WORLD_FILES,
    RULES_FILES,
    _cast_entry_is_projectable,
    _gate_cast_slugs_on_manifest,
    _gate_poi_slugs_on_manifest,
    _humanize_label,
    _is_devnote,
    load_cast_entries,
    load_poi_slug_map,
    load_points_of_interest,
)
from sidequest.server.reference_slug import slugify
from sidequest.server.reference_theme import (
    PACK_BLURBS,
    PACK_LABELS,
    _read_theme_yaml,
    _require_str,
)
from sidequest.server.reference_timeline import (
    _temporal_of,
    _year_key,
    load_legends,
    load_lore_history,
)
from sidequest.server.reference_visibility import Visibility, classify
from sidequest.server.utils import slugify_player_name
from sidequest.telemetry.spans.reference import (
    reference_devnote_suppressed_span,
    reference_map_dangling_edge_span,
    reference_map_pin_not_found_span,
    reference_map_pin_resolved_span,
    reference_map_rendered_span,
    reference_meta_missing_span,
    reference_npc_unratified_skipped_span,
    reference_poi_image_not_found_span,
    reference_poi_image_resolved_span,
    reference_portrait_not_found_span,
    reference_portrait_resolved_span,
    reference_timeline_rendered_span,
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


def build_cast_section(
    entries: list[dict],
    *,
    pack: str,
    world: str,
    portrait_on_r2_slugs: frozenset[str],
) -> dict | None:
    """Project the RATIFIED NPC cast into the public ``cast`` section dict.

    The data-shaping analog of ``present_lore_cast``. Mirrors
    ``build_lore_map_section``: the caller pre-computes the R2 slug set; this
    function does not load the manifest. Membership is gated through the shared
    ADR-138 §D4 ratification predicate (``_cast_entry_is_projectable`` →
    :func:`sidequest.game.npc_pool.is_projectable`) — never re-derived — and
    empty-name entries are skipped (parity with ``present_lore_cast``). The
    portrait URL is resolved server-side via ``resolve_asset_url`` over the
    world-scoped ``portrait_image_key`` when the slug is on R2, else ``None``;
    the client never sees a raw key/path. Only the public allowlist keys cross
    the boundary — keeper fields are never splatted in. Returns ``None`` when no
    projectable member survives.
    """
    members: list[dict] = []
    for entry in entries:
        if not _cast_entry_is_projectable(entry):
            continue
        name = str(entry.get("name", "")).strip()
        if not name:
            continue
        slug = cast_portrait_slug(entry)
        if slug in portrait_on_r2_slugs:
            with reference_portrait_resolved_span(slug=slug, pack=pack, world=world):
                pass
            portrait_url: str | None = resolve_asset_url(portrait_image_key(pack, world, slug))
        else:
            with reference_portrait_not_found_span(slug=slug, pack=pack, world=world):
                pass
            portrait_url = None
        role = entry.get("role")
        appearance = entry.get("appearance")
        members.append(
            {
                "slug": slug,
                "name": name,
                "role": role if role is None else str(role),
                "appearance": appearance if appearance is None else str(appearance),
                "portrait_url": portrait_url,
            }
        )

    if not members:
        return None
    return {"id": "cast", "label": "Cast", "members": members}


def build_poi_section(
    entries: list[dict],
    *,
    pack: str,
    world: str,
    poi_on_r2_slugs: frozenset[str],
) -> dict | None:
    """Project the R2-gated POIs into the public ``poi`` section dict.

    The data-shaping analog of ``present_renderable_landscapes`` (the "Renderable
    Landscapes" gallery). Mirrors ``build_cast_section``: the caller pre-computes
    the R2 **anchor**-slug set (the output of ``_gate_poi_slugs_on_manifest``);
    this function does not load the manifest.

    **Exclusion model.** A POI is projected only when its landscape is on R2 — a
    POI whose anchor slug is not in ``poi_on_r2_slugs`` is omitted entirely (never
    a member with ``image_url: None``), exactly as the gallery shows only POIs
    with rendered art. The excluded POI still fires
    ``reference_poi_image_not_found_span`` so the skip is observable (the GM/dev
    panel sees the gate ran), unlike the HTML gallery's silent ``continue``.
    Returns ``None`` when no POI survives the gate.

    The R2 object key is built from the **verbatim** authored slug
    (``poi_image_key`` over ``slug``-or-``name``), while membership keys on the
    **anchor** (``slugify``) form — the Story 71-38 decouple, so an underscore
    authored slug addresses its underscore R2 key, not the hyphen anchor. The
    ``image_url`` is resolved server-side via ``resolve_asset_url``; the client
    never sees a raw key/path. Only the public allowlist keys cross the boundary
    — keeper fields are never splatted in.
    """
    members: list[dict] = []
    for entry in entries:
        raw = entry.get("slug") or entry.get("name")
        if not raw:
            continue
        verbatim = str(raw)
        anchor = slugify(verbatim)
        if not anchor:
            continue
        if anchor not in poi_on_r2_slugs:
            with reference_poi_image_not_found_span(pack=pack, world=world, slug=anchor):
                pass
            continue
        with reference_poi_image_resolved_span(pack=pack, world=world, slug=anchor):
            pass
        image_url = resolve_asset_url(poi_image_key(pack, world, verbatim))
        region = entry.get("region")
        description = entry.get("description")
        members.append(
            {
                "slug": anchor,
                "name": str(entry.get("name", "")),
                "region": region if region is None else str(region),
                "description": description if description is None else str(description),
                "image_url": image_url,
            }
        )

    if not members:
        return None
    return {"id": "poi", "label": "Points of Interest", "entries": members}


def build_timeline_section(legends: list[Legend], *, history_prose: str | None) -> dict | None:
    """Project the world's legends into the public ``timeline`` section dict.

    The data-shaping analog of ``present_lore_timeline`` (``reference_timeline.py``,
    Story 65-12). Unlike POI/Cast there is no R2 art gate — legends emit no images.
    Returns ``None`` when there are no legends (the section is purely additive).

    **Allowlist firewall.** Each :class:`Legend` is projected through a fixed public
    allowlist — ``slug`` / ``name`` / ``summary`` / ``temporal`` only. A naive
    ``legend.model_dump()`` splat would carry the keeper-side typed fields
    (``related_tropes`` dormant-trope spoiler seeds per ADR-135 D1, plus
    ``notable_figures``, ``faction_grudges``, …); those never cross. The SAME
    ``legends.yaml`` is also projected via the generic-YAML path, where
    :func:`classify` carves ``related_tropes`` KEEPER (spec C1) — see
    ``reference_visibility.py``.

    **Honest conditional sort.** The temporal value (``era`` falling back to
    ``period``) is free-text. The dated spine sorts ascending ONLY when EVERY dated
    entry is a clean signed-integer year (``_year_key``); otherwise authored order is
    preserved rather than fabricating a cross-dialect chronology. Undated legends
    (no era and no period) always follow the dated spine, in authored order. The
    ``sort_mode`` is recorded on the SHIPPED Story 65-12 ``timeline_rendered`` span
    (reuse, not a new span) so the GM/dev panel knows whether a chronology was
    computed or fell back.
    """
    if not legends:
        return None

    entries: list[dict] = [
        {
            "slug": slugify_player_name(lg.name),
            "name": lg.name,
            "summary": lg.summary,
            "temporal": _temporal_of(lg),
        }
        for lg in legends
    ]
    dated = [e for e in entries if e["temporal"] is not None]
    undated = [e for e in entries if e["temporal"] is None]

    # Honest conditional sort: only when EVERY dated entry is a clean year.
    if dated and all(_year_key(e["temporal"] or "") is not None for e in dated):
        sort_mode = "sorted"
        # `or 0` is unreachable (all keys parse here); it only narrows the type.
        ordered_dated = sorted(dated, key=lambda e: _year_key(e["temporal"] or "") or 0)
    else:
        sort_mode = "authored_order"
        ordered_dated = list(dated)

    with reference_timeline_rendered_span(
        entry_count=len(entries),
        undated_count=len(undated),
        sort_mode=sort_mode,
    ):
        pass

    return {
        "id": "timeline",
        "label": "Timeline",
        "sort_mode": sort_mode,
        "preamble": history_prose,
        "entries": ordered_dated + undated,
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
                entries.append({"key": str(key), "label": _humanize_label(key), "node": child_node})
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
        with reference_unknown_field_span(pack=pack, world=world, file_stem=file_stem, key_path=()):
            pass
        return None

    node = _project_node(data, file_stem=file_stem, key_path=(), pack=pack, world=world)
    if node is None:
        return None
    return {"id": slugify(file_stem), "label": _humanize_label(file_stem), "node": node}


def build_theme_tokens(pack: str, *, pack_dir: Path) -> dict[str, str]:
    """Project a pack's ``theme.yaml`` into a flat CSS-var token set (Story 100-7).

    Returns a flat ``{"--var": "value"}`` dict the Phase-2 session-free injector
    (story 100-9, UI) splats directly via ``root.style.setProperty(k, v)`` —
    replacing the session-coupled ``theme_css`` WebSocket channel for the
    session-free reference routes.

    **Allowlist discipline is the security spine here**, NOT a keeper firewall.
    ``theme.yaml`` is public styling (``GenreTheme`` is ``extra="forbid"`` and
    ``reference_visibility.py`` carries no ``theme`` carve), so there is no
    ``classify()`` dimension. Instead the token set is built from an EXPLICIT
    allowlist of the six shared-``useGenreTheme`` palette vars, the two font vars,
    and the three dinkus glyphs — never a raw ``yaml.safe_load`` splat. Internal
    non-CSS config (``border_style``, ``dinkus.cooldown``/``default_weight``,
    ``session_opener``, authored ``_`` notes) and ``archetype`` (an HTML attribute,
    not a CSS var) are excluded by construction.

    Loud failure: a missing/empty required field raises ``MissingThemeFieldError``
    via the shared ``_require_str`` (No Silent Fallbacks) — a pack that cannot be
    themed 500s rather than rendering with collapsed defaults.
    """
    data, _pack_name = _read_theme_yaml(pack_dir)
    glyph = (data.get("dinkus") or {}).get("glyph") or {}
    return {
        "--primary": _require_str(data.get("primary"), "primary", pack),
        "--secondary": _require_str(data.get("secondary"), "secondary", pack),
        "--accent": _require_str(data.get("accent"), "accent", pack),
        "--background": _require_str(data.get("background"), "background", pack),
        "--surface": _require_str(data.get("surface"), "surface", pack),
        "--text": _require_str(data.get("text"), "text", pack),
        "--font-body": _require_str(data.get("web_font_family"), "web_font_family", pack),
        "--font-display": _require_str(
            data.get("display_font_family"), "display_font_family", pack
        ),
        "--dinkus-light": _require_str(glyph.get("light"), "dinkus.glyph.light", pack),
        "--dinkus-medium": _require_str(glyph.get("medium"), "dinkus.glyph.medium", pack),
        "--dinkus-heavy": _require_str(glyph.get("heavy"), "dinkus.glyph.heavy", pack),
    }


def build_reference_meta(pack: str, *, world_dir: Path | None = None) -> dict[str, str]:
    """Masthead chrome for the reference SPA (2026-06-09 redesign bundle).

    The React shell's ``Masthead`` needs a pack label, a dateline, and — on the
    lore page — the world's display name. Pack label and dateline come from the
    chrome constants in ``reference_theme.py`` (``PACK_LABELS``/``PACK_BLURBS``,
    stranded by the 100-12 SPA cutover and re-wired here); the world name comes
    from the world's ``world.yaml`` ``name`` field. The dinkus glyph is NOT
    carried here — it already rides the theme token set as ``--dinkus-light``.

    A gap in any field follows the documented chrome loud-fallback doctrine
    (see ``PACK_TOC``/``DEFAULT_TOC``): derive a humanized slug AND fire the
    ``sidequest.reference.meta_missing`` ERROR span so drift surfaces on the
    GM panel — never a silent default, never a 500 for missing prose chrome.
    """
    label = PACK_LABELS.get(pack)
    if label is None:
        with reference_meta_missing_span(pack=pack, field="pack_label"):
            label = _humanize_label(pack)
    meta: dict[str, str] = {"pack_label": label}

    dateline = PACK_BLURBS.get(pack)
    if dateline is None:
        with reference_meta_missing_span(pack=pack, field="dateline"):
            pass
    else:
        meta["dateline"] = dateline

    if world_dir is not None:
        world_name: str | None = None
        world_yaml = world_dir / "world.yaml"
        if world_yaml.is_file():
            with world_yaml.open(encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            raw = data.get("name") if isinstance(data, dict) else None
            if isinstance(raw, str) and raw.strip():
                world_name = raw.strip()
        if world_name is None:
            with reference_meta_missing_span(pack=pack, field="world_name"):
                world_name = _humanize_label(world_dir.name)
        meta["world_name"] = world_name

    return meta


def build_lore_projection(pack: str, world: str, *, pack_dir: Path, world_dir: Path) -> dict:
    """Assemble the public-projected lore document.

    Emits, in order: the ``map`` section (cartography, when present), the
    ``timeline`` section (the world's legends with an honest conditional sort), the
    ``poi`` section (history.yaml points_of_interest gated on R2 landscape art), the
    ``cast`` section (ratified NPCs gated on R2 portraits), one generic-YAML section
    per present ``LORE_WORLD_FILES`` file, then the ``legends`` section (full legend
    bodies — built from the per-file ``legends/`` directory form here; the flat
    ``legends.yaml`` form is covered by the generic-YAML loop). Each section is
    omitted when it has no public content.

    The server emits sections in build order only; *display* ordering is a client
    concern — the React reference page (``sidequest-ui``) sorts the sections it
    renders. This module keeps the projection job: deciding what public data exists.
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

    # Timeline section — the world-historical spine from the world's legends, AFTER
    # the map section. No R2 gate (legends emit no images); the honest conditional
    # sort records its mode on the timeline_rendered span. Omitted when the world
    # authors no legends. The keeper related_tropes field is firewalled both here
    # (allowlist) and on the generic-YAML legends path (classify() KEEPER, spec C1).
    timeline_legends = load_legends(world_dir)
    if timeline_legends:
        timeline_section = build_timeline_section(
            timeline_legends, history_prose=load_lore_history(world_dir)
        )
        if timeline_section is not None:
            sections.append(timeline_section)

    # POI section — public points of interest from history.yaml, AFTER the map
    # section. Gallery semantics: only POIs whose landscape is on R2 project
    # (exclusion model); an authored-but-art-less POI fires an observable
    # not_found span and is omitted, and the section collapses to None when none
    # survive. The R2 gate consults the {anchor: verbatim} slug map (verbatim
    # keys the R2 object, anchor keys membership — Story 71-38).
    poi_slug_map = load_poi_slug_map(world_dir)
    if poi_slug_map:
        gated_poi_slugs = _gate_poi_slugs_on_manifest(
            poi_slug_map,
            pack=pack,
            world=world,
            pack_dir=pack_dir,
        )
        poi_section = build_poi_section(
            load_points_of_interest(world_dir),
            pack=pack,
            world=world,
            poi_on_r2_slugs=gated_poi_slugs,
        )
        if poi_section is not None:
            sections.append(poi_section)

    # Cast section — public NPC cast from portrait_manifest.yaml, AFTER the map
    # section. The ADR-138 §D4 ratification gate withholds unratified phantoms;
    # the withheld count is recorded on a per-render span (fires even when 0, but
    # only when the world authors a Cast) so the skip is observable, never silent.
    cast_entries = load_cast_entries(world_dir)
    if cast_entries:
        ratified_entries = [e for e in cast_entries if _cast_entry_is_projectable(e)]
        with reference_npc_unratified_skipped_span(
            pack=pack,
            world=world,
            count=len(cast_entries) - len(ratified_entries),
        ):
            pass
        if ratified_entries:
            authored_portrait_slugs = frozenset(
                cast_portrait_slug(e) for e in ratified_entries if str(e.get("name", "")).strip()
            )
            gated_portrait_slugs = _gate_cast_slugs_on_manifest(
                authored_portrait_slugs,
                pack=pack,
                world=world,
                pack_dir=pack_dir,
            )
            cast_section = build_cast_section(
                ratified_entries,
                pack=pack,
                world=world,
                portrait_on_r2_slugs=gated_portrait_slugs,
            )
            if cast_section is not None:
                sections.append(cast_section)

    # Generic-YAML sections — one per present LORE_WORLD_FILES file, AFTER the
    # map section. EXCLUDED_FILES (and file-root KEEPER stems) never project.
    legends_dir_present = (world_dir / "legends").is_dir()
    for filename in LORE_WORLD_FILES:
        if filename in EXCLUDED_FILES:
            continue
        # The Legends section is built below from the per-file ``legends/``
        # directory when present (the dominant authoring form); skip the flat
        # ``legends.yaml`` file path here so the directory wins and we never emit
        # two ``id: "legends"`` sections. When there is no directory, the flat
        # file is projected here as usual.
        if filename == "legends.yaml" and legends_dir_present:
            continue
        path = world_dir / filename
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if data is None:
            continue
        section = build_generic_yaml_section(data, file_stem=path.stem, pack=pack, world=world)
        if section is not None:
            sections.append(section)

    # Legends section (per-file ``legends/`` directory form). Most live worlds
    # author legends as one ``*.yaml`` per legend in a ``legends/`` dir rather
    # than a flat ``legends.yaml`` — those worlds previously got ONLY the
    # chronological Timeline digest and no full Legends section. Build it here
    # from the same raw legend dicts, through the SAME generic renderer +
    # ``classify()`` firewall as the flat-file path (``related_tropes`` is KEEPER),
    # so both authoring forms yield an identical full ``legends`` section. The
    # flat-file form is handled by the generic loop above; the two are mutually
    # exclusive (see the skip guard) so there is never a duplicate.
    if legends_dir_present:
        raw_legends = [
            yaml.safe_load((world_dir / "legends" / f.name).read_text(encoding="utf-8"))
            for f in sorted((world_dir / "legends").glob("*.yaml"))
            if f.name not in ("_meta.yaml", ".gitkeep")
        ]
        raw_legends = [r for r in raw_legends if r is not None]
        if raw_legends:
            legends_section = build_generic_yaml_section(
                raw_legends, file_stem="legends", pack=pack, world=world
            )
            if legends_section is not None:
                sections.append(legends_section)

    return {"schema_version": 1, "pack": pack, "world": world, "sections": sections}


def build_rules_projection(pack: str, *, pack_dir: Path) -> dict:
    """Assemble the public-projected rules document (Story 100-6).

    The pack-tier analog of :func:`build_lore_projection`. The Rules page is the
    genre-tier **rulebook** — per-pack, not per-world — so the document carries no
    ``world`` key and there is no map/cast/POI/timeline section. It emits one
    generic-YAML node-tree section per present ``RULES_FILES`` file (skipping
    ``EXCLUDED_FILES``), each projected through the SAME ``classify()`` firewall the
    lore generic sections use. Routing every file through
    :func:`build_generic_yaml_section` is load-bearing: the rules-tier keeper carves
    already exist in ``reference_visibility.py`` (``rules`` ``narrator_hint`` on
    confrontations/edge/resources, ``power_tiers.*.*.npc``, ``beat_vocabulary.obstacles``),
    so the firewall is automatic — a raw ``yaml.safe_load`` splat would leak every
    keeper field. Sections with no public content are omitted; an empty pack yields
    an empty section list (purely additive per present file).
    """
    sections: list[dict] = []
    for filename in RULES_FILES:
        if filename in EXCLUDED_FILES:
            continue
        path = pack_dir / filename
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if data is None:
            continue
        section = build_generic_yaml_section(data, file_stem=path.stem, pack=pack, world="")
        if section is not None:
            sections.append(section)

    return {"schema_version": 1, "pack": pack, "sections": sections}
