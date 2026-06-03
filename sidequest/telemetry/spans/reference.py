"""Reference-URL attachment spans — v2 hyperlink subsystem observability.

Per CLAUDE.md OTEL principle, every subsystem decision emits a span. The
``reference`` URL subsystem (Tasks 6-9) attaches URLs onto protocol
objects at construction time. Three outcomes are observable:

- ``sidequest.reference.url_attached`` — INFO; URL was constructed and set
  on the protocol object. Carries the keyed identity (kind + key path).
- ``sidequest.reference.url_skipped`` — INFO; the keyed entity is not in
  the YAML registry (content drift; recoverable — the UI renders plain
  text in this branch).
- ``sidequest.reference.url_failed`` — ERROR; programmer error path
  reserved for impossible states (e.g. unknown pack/world flowing into a
  builder post-session-bind). Should never fire in practice.

All three are flat-only — the GM dashboard reads them via the
``agent_span_close`` fan-out; no typed-event extractor needed.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

from ._core import FLAT_ONLY_SPANS
from .span import Span

SPAN_REFERENCE_URL_ATTACHED = "sidequest.reference.url_attached"
SPAN_REFERENCE_URL_SKIPPED = "sidequest.reference.url_skipped"
SPAN_REFERENCE_URL_FAILED = "sidequest.reference.url_failed"

# Chrome-render failure spans (Story 63-4 Tasks 18 + 21; Story 63-7 Task C).
SPAN_REFERENCE_THEME_MISSING = "sidequest.reference.theme_missing"
SPAN_REFERENCE_HERO_UNBOUND = "sidequest.reference.hero_unbound"
SPAN_REFERENCE_TOC_MISSING = "sidequest.reference.toc_missing"

# Body-presenter dispatch spans (Story 63-8 / reference-body-presenters plan).
SPAN_REFERENCE_UNKNOWN_FIELD = "sidequest.reference.unknown_field"
SPAN_REFERENCE_UNPRESENTED_FIELD = "sidequest.reference.unpresented_field"
SPAN_REFERENCE_PRESENTER_ERROR = "sidequest.reference.presenter_error"

# POI landscape-image resolution spans (Story 63-8). The lore page's location
# cards attach an R2 landscape image when the location slug is in the
# history.yaml POI manifest. Both outcomes are observable so the GM/dev panel
# can confirm the renderer ran the lookup rather than silently rendering text.
SPAN_REFERENCE_POI_IMAGE_RESOLVED = "sidequest.reference.poi_image_resolved"
SPAN_REFERENCE_POI_IMAGE_NOT_FOUND = "sidequest.reference.poi_image_not_found"

# R2-manifest existence-gate span (Story 65-8). The lore page loads
# r2_manifest.json (the 65-7 R2 existence oracle) and gates POI <img> emission
# on key presence, so authored-but-not-rendered POIs never produce a broken
# image. Fired once per lore render that has POIs to gate.
SPAN_REFERENCE_MANIFEST_LOADED = "sidequest.reference.manifest_loaded"

# Humanization-guard suppression span (Story 63-9). Fired when the fallback
# walk drops a dev-note / placeholder value or a private (leading-underscore)
# key so it never reaches the player-/author-facing reference HTML.
SPAN_REFERENCE_DEVNOTE_SUPPRESSED = "sidequest.reference.devnote_suppressed"

# Lore-page Map section spans (Story 65-11). The Map section renders a
# server-side SVG node-link graph from cartography.yaml. Four decisions are
# observable so the GM/dev panel can confirm the graph was built from real
# cartography rather than improvised: the render summary (node/edge/pin counts),
# the per-pin portrait gate (resolved vs not-found, dedicated reference-namespaced
# spans rather than the scene-time scrapbook family), and a dropped dangling edge.
SPAN_REFERENCE_MAP_RENDERED = "sidequest.reference.map_rendered"
SPAN_REFERENCE_MAP_PIN_RESOLVED = "sidequest.reference.map_pin_resolved"
SPAN_REFERENCE_MAP_PIN_NOT_FOUND = "sidequest.reference.map_pin_not_found"
SPAN_REFERENCE_MAP_DANGLING_EDGE = "sidequest.reference.map_dangling_edge"

# Lore-page Cast section portrait-gate spans (Story 65-13). Dedicated, reference-
# namespaced spans for the Cast portrait gate — the portrait analog of the map-pin
# spans above and a migration off the scene-time scrapbook.npc_portrait_* family
# (whose docstrings describe attaching a portrait_url to a scrapbook ref on scene
# invocation — a semantic the reference page does not have). On the reference page,
# "not_found" means *authored-but-not-on-R2*, a distinct fact from the scrapbook
# family's "not authored at all" (ad-hoc scene NPC). Both outcomes are observable so
# the GM/dev panel can tell "authored & on R2" from "authored, not on R2".
SPAN_REFERENCE_PORTRAIT_RESOLVED = "sidequest.reference.portrait_resolved"
SPAN_REFERENCE_PORTRAIT_NOT_FOUND = "sidequest.reference.portrait_not_found"

FLAT_ONLY_SPANS.update(
    {
        SPAN_REFERENCE_URL_ATTACHED,
        SPAN_REFERENCE_URL_SKIPPED,
        SPAN_REFERENCE_URL_FAILED,
        SPAN_REFERENCE_THEME_MISSING,
        SPAN_REFERENCE_HERO_UNBOUND,
        SPAN_REFERENCE_TOC_MISSING,
        SPAN_REFERENCE_UNKNOWN_FIELD,
        SPAN_REFERENCE_UNPRESENTED_FIELD,
        SPAN_REFERENCE_PRESENTER_ERROR,
        SPAN_REFERENCE_POI_IMAGE_RESOLVED,
        SPAN_REFERENCE_POI_IMAGE_NOT_FOUND,
        SPAN_REFERENCE_MANIFEST_LOADED,
        SPAN_REFERENCE_DEVNOTE_SUPPRESSED,
        SPAN_REFERENCE_MAP_RENDERED,
        SPAN_REFERENCE_MAP_PIN_RESOLVED,
        SPAN_REFERENCE_MAP_PIN_NOT_FOUND,
        SPAN_REFERENCE_MAP_DANGLING_EDGE,
        SPAN_REFERENCE_PORTRAIT_RESOLVED,
        SPAN_REFERENCE_PORTRAIT_NOT_FOUND,
    }
)


def _attrs(
    *,
    kind: str,
    pack: str,
    world: str | None,
    keys: tuple[str, ...],
    extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "reference.kind": kind,
        "reference.pack": pack,
        "reference.keys": "/".join(keys),
    }
    if world is not None:
        out["reference.world"] = world
    if extras:
        out.update(extras)
    return out


@contextmanager
def reference_url_attached_span(
    *,
    kind: str,
    pack: str,
    world: str | None,
    keys: tuple[str, ...],
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_REFERENCE_URL_ATTACHED,
        _attrs(kind=kind, pack=pack, world=world, keys=keys),
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def reference_url_skipped_span(
    *,
    kind: str,
    pack: str,
    world: str | None,
    keys: tuple[str, ...],
    reason: str,
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_REFERENCE_URL_SKIPPED,
        _attrs(
            kind=kind,
            pack=pack,
            world=world,
            keys=keys,
            extras={"reference.reason": reason},
        ),
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def reference_url_failed_span(
    *,
    kind: str,
    pack: str,
    world: str | None,
    keys: tuple[str, ...],
    reason: str,
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_REFERENCE_URL_FAILED,
        _attrs(
            kind=kind,
            pack=pack,
            world=world,
            keys=keys,
            extras={"reference.reason": reason},
        ),
        tracer_override=_tracer,
    ) as span:
        yield span


# --- Chrome-render failure spans (Story 63-4) ---


@contextmanager
def reference_theme_missing_span(
    *,
    pack: str,
    field: str,
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    """ERROR span fired when theme.yaml lacks a required chrome field.

    The renderer raises ``MissingThemeFieldError`` from inside this span so
    the OTEL exporter sees the failure status as well as the attributes.
    """
    with Span.open(
        SPAN_REFERENCE_THEME_MISSING,
        {"reference.pack": pack, "reference.field": field},
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def reference_hero_unbound_span(
    *,
    pack: str,
    world: str,
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    """WARN span fired when lore.yaml is missing or unbound for a lore page;
    the hero falls back to the pack name instead of the world name."""
    with Span.open(
        SPAN_REFERENCE_HERO_UNBOUND,
        {"reference.pack": pack, "reference.world": world},
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def reference_toc_missing_span(
    *,
    pack: str,
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    """ERROR span fired when ``PACK_TOC`` has no entry for the requested pack
    and the renderer falls through to the documented 2-item default TOC
    (``reckoning``, ``bearing``).

    Not a silent fallback per SOUL doctrine — the GM panel surfaces the
    gap so authoring drift (a new pack added to content without chrome
    metadata) is visible in OTEL rather than buried in "the reference
    page looks slightly off."
    """
    with Span.open(
        SPAN_REFERENCE_TOC_MISSING,
        {"reference.pack": pack},
        tracer_override=_tracer,
    ) as span:
        yield span


# --- Body-presenter dispatch spans (Story 63-8) ---


@contextmanager
def reference_unknown_field_span(
    *,
    pack: str,
    world: str | None,
    file_stem: str,
    key_path: tuple[str, ...],
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    """WARN — fired when a YAML field is in neither PUBLIC nor KEEPER.

    The dispatcher drops the field. Stderr log-once deduping happens in the
    renderer; the span itself fires every render so OTEL traces stay honest
    about the per-render state."""
    attrs: dict[str, Any] = {
        "reference.pack": pack,
        "reference.file_stem": file_stem,
        "reference.key_path": ".".join(key_path) or "<root>",
    }
    if world is not None:
        attrs["reference.world"] = world
    with Span.open(
        SPAN_REFERENCE_UNKNOWN_FIELD,
        attrs,
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def reference_unpresented_field_span(
    *,
    pack: str,
    file_stem: str,
    key_path: tuple[str, ...],
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    """INFO — fired when a PUBLIC field has no registered presenter; the
    generic dispatcher renders it as label/value. Intentional fallback, not
    a defect — surfaced so coverage gaps are visible without alarming."""
    with Span.open(
        SPAN_REFERENCE_UNPRESENTED_FIELD,
        {
            "reference.pack": pack,
            "reference.file_stem": file_stem,
            "reference.key_path": ".".join(key_path) or "<root>",
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def reference_presenter_error_span(
    *,
    pack: str,
    file_stem: str,
    key_path: tuple[str, ...],
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    """ERROR — fired when a presenter raises. Caller (the dispatcher) is
    responsible for recording the exception and re-raising; this helper
    matches the thin Span.open shape used by the rest of the module."""
    with Span.open(
        SPAN_REFERENCE_PRESENTER_ERROR,
        {
            "reference.pack": pack,
            "reference.file_stem": file_stem,
            "reference.key_path": ".".join(key_path) or "<root>",
        },
        tracer_override=_tracer,
    ) as span:
        yield span


# --- Humanization-guard suppression span (Story 63-9) ---


@contextmanager
def reference_devnote_suppressed_span(
    *,
    pack: str,
    world: str | None,
    file_stem: str,
    key_path: tuple[str, ...],
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    """WARN — fired when the fallback walk suppresses a dev-note / placeholder
    value or a private (leading-underscore) key so it never reaches the
    player-/author-facing reference HTML.

    Loud suppression, not a silent drop (SOUL "No Silent Fallbacks"): the GM
    panel can see WHICH field was dropped on WHICH page via key_path."""
    attrs: dict[str, Any] = {
        "reference.pack": pack,
        "reference.file_stem": file_stem,
        "reference.key_path": ".".join(key_path) or "<root>",
    }
    if world is not None:
        attrs["reference.world"] = world
    with Span.open(
        SPAN_REFERENCE_DEVNOTE_SUPPRESSED,
        attrs,
        tracer_override=_tracer,
    ) as span:
        yield span


# --- POI landscape-image resolution spans (Story 63-8) ---


def _poi_attrs(*, pack: str, world: str | None, slug: str) -> dict[str, Any]:
    attrs: dict[str, Any] = {"reference.pack": pack, "reference.slug": slug}
    if world is not None:
        attrs["reference.world"] = world
    return attrs


@contextmanager
def reference_poi_image_resolved_span(
    *,
    pack: str,
    world: str | None,
    slug: str,
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    """INFO — fired when a location card's slug is in the history.yaml POI
    manifest and an R2 landscape ``<img>`` is emitted into the card."""
    with Span.open(
        SPAN_REFERENCE_POI_IMAGE_RESOLVED,
        _poi_attrs(pack=pack, world=world, slug=slug),
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def reference_poi_image_not_found_span(
    *,
    pack: str,
    world: str | None,
    slug: str,
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    """INFO — fired when a rendered location has no matching POI image and the
    card renders text-only. Missing landscape art is EXPECTED (not every
    location has one); this is observability, not an error. The span lets the
    GM/dev panel confirm the lookup ran rather than silently skipping."""
    with Span.open(
        SPAN_REFERENCE_POI_IMAGE_NOT_FOUND,
        _poi_attrs(pack=pack, world=world, slug=slug),
        tracer_override=_tracer,
    ) as span:
        yield span


# --- R2-manifest existence-gate span (Story 65-8) ---


@contextmanager
def reference_manifest_loaded_span(
    *,
    path: str,
    entry_count: int,
    world_key_count: int,
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    """INFO — fired when a lore render consults r2_manifest.json (the 65-7 R2
    existence oracle) to gate POI ``<img>`` emission. Carries the
    manifest path, total entry count, and the number of keys under this world's
    POI prefix, so the GM/dev panel can confirm the gate ran against a real
    oracle rather than improvising image URLs."""
    with Span.open(
        SPAN_REFERENCE_MANIFEST_LOADED,
        {
            "reference.manifest_path": path,
            "reference.manifest_entry_count": entry_count,
            "reference.world_key_count": world_key_count,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


# --- Lore-page Map section spans (Story 65-11) ---


@contextmanager
def reference_map_rendered_span(
    *,
    node_count: int,
    edge_count: int,
    npc_pin_count: int,
    resolved_pin_count: int,
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    """INFO — fired once per lore render that builds a Map section. Carries the
    graph census (nodes, de-duplicated edges, npc pins, and how many of those
    pins resolved a portrait on R2) so the GM/dev panel can confirm the SVG was
    built from real cartography rather than improvised."""
    with Span.open(
        SPAN_REFERENCE_MAP_RENDERED,
        {
            "reference.map_node_count": node_count,
            "reference.map_edge_count": edge_count,
            "reference.map_npc_pin_count": npc_pin_count,
            "reference.map_resolved_pin_count": resolved_pin_count,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def reference_map_pin_resolved_span(
    *,
    slug: str,
    region: str,
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    """INFO — fired when a map npc pin's world-scoped portrait key IS in
    r2_manifest.json, so the pin renders its portrait image."""
    with Span.open(
        SPAN_REFERENCE_MAP_PIN_RESOLVED,
        {"slug": slug, "reference.map_region": region},
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def reference_map_pin_not_found_span(
    *,
    slug: str,
    region: str,
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    """INFO — fired when a map npc pin's portrait is NOT on R2, so the pin renders
    a non-image marker (never a broken <img>). The span proves the gate ran."""
    with Span.open(
        SPAN_REFERENCE_MAP_PIN_NOT_FOUND,
        {"slug": slug, "reference.map_region": region},
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def reference_map_dangling_edge_span(
    *,
    source_region: str,
    dangling_region: str,
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    """WARN — fired when a region's ``adjacent`` list names a region id that does
    not exist in cartography.regions. The edge is dropped (not drawn) and this
    span names the bad reference so the gap is fail-visible, not silent."""
    with Span.open(
        SPAN_REFERENCE_MAP_DANGLING_EDGE,
        {
            "reference.map_source_region": source_region,
            "reference.map_dangling_region": dangling_region,
            "reference.level": "WARN",
        },
        tracer_override=_tracer,
    ) as span:
        yield span


# --- Lore-page Cast portrait-gate spans (Story 65-13) ---


def _portrait_attrs(*, slug: str, pack: str, world: str) -> dict[str, str]:
    return {"slug": slug, "reference.pack": pack, "reference.world": world}


@contextmanager
def reference_portrait_resolved_span(
    *,
    slug: str,
    pack: str,
    world: str,
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    """INFO — fired when a Cast NPC's world-scoped portrait key IS in
    r2_manifest.json, so the Cast card renders its R2 portrait ``<img>``."""
    with Span.open(
        SPAN_REFERENCE_PORTRAIT_RESOLVED,
        _portrait_attrs(slug=slug, pack=pack, world=world),
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def reference_portrait_not_found_span(
    *,
    slug: str,
    pack: str,
    world: str,
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    """INFO — fired when a Cast NPC is authored in portrait_manifest.yaml but her
    portrait is NOT on R2, so the card renders text-only (never a broken ``<img>``).
    Distinct from the scene-time ``scrapbook.npc_portrait_not_found`` ("not authored
    at all"): here the NPC IS authored, just not yet rendered. The span proves the
    gate ran."""
    with Span.open(
        SPAN_REFERENCE_PORTRAIT_NOT_FOUND,
        _portrait_attrs(slug=slug, pack=pack, world=world),
        tracer_override=_tracer,
    ) as span:
        yield span
