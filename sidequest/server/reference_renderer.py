"""Data loaders + file mappings for the reference JSON projection.

Story 100-12 (Phase 4 cutover) retired this module's HTML-emitting half. The
server no longer renders ``/reference/*`` pages — that surface is now the React
SPA, fed by the JSON projection in ``reference_projection.py``. What survives
here are the pure *data* helpers the projection (and ``map_emit``) import:

* file-to-page mappings (``RULES_FILES``, ``LORE_WORLD_FILES``, ``EXCLUDED_FILES``),
* the player-facing label/devnote humanizers (``_humanize_label``, ``_is_devnote``),
* the POI / Cast loaders and their R2-manifest existence gates.

Everything that produced HTML — ``render_node``/``_render_*``, the TOC/hero/
document chrome, ``assemble_rules_page``/``assemble_lore_page``, presenter
dispatch and the scroll-spy script — was deleted in the cutover. No function
here emits markup; this is the serializer's data layer only.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

import yaml

from sidequest.foundation.reference_slug import slugify
from sidequest.game import npc_pool
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.server.reference_presenters import (
    poi_image_key,
    portrait_image_key,
)
from sidequest.telemetry.spans.reference import (
    reference_manifest_loaded_span,
)


def _humanize_label(raw: object) -> str:
    """Humanize an identifier-shaped key/id for a player-facing heading.

    The generic fallback printed raw YAML keys and item ids verbatim as
    headings — ``the_maw``, ``genre_conventions``, ``rolls_per_slot``,
    ``floor_it`` — leaking snake_case onto Rules/Lore pages (playtest
    2026-05-27, the dominant road_warrior failure). This converts
    identifier-shaped strings to Title Case ("The Maw", "Genre Conventions",
    "Rolls Per Slot", "Floor It").

    Conservative by design:

    - A string that already contains whitespace is assumed author-formatted and
      returned UNCHANGED ("Floor It", "Item 3").
    - A string carrying an underscore or hyphen is a developer identifier; it is
      ALWAYS split and Title-Cased so the raw separator can never survive into a
      heading ("the_maw" → "The Maw", "MECHANICAL_surface" → "Mechanical
      Surface", "tier-1" → "Tier 1"). Story 63-9 AC1: no raw "_" reaches HTML.
    - A *separator-free* token with any uppercase letter is an acronym or
      proper noun and returned UNCHANGED ("USB", "McGuffin"). NOTE: this only
      protects a whole-string acronym. An acronym embedded in a compound
      identifier key ("USB_port") still splits and Title-Cases each part
      ("Usb Port") — the AC1 safe invariant (no raw "_" in HTML) wins over
      acronym fidelity, since a snake_case key is a developer string first.
    - A bare lowercase word is Title-Cased ("setting" → "Setting").
    """
    text = str(raw).strip()
    if not text:
        return text
    if any(c.isspace() for c in text):
        return text
    if "_" in text or "-" in text:
        parts = [p for p in re.split(r"[_\-]+", text) if p]
        return " ".join(p.capitalize() for p in parts) or text
    if any(c.isupper() for c in text):
        return text
    return text.capitalize()


# Story 63-9: leading-token markers that flag developer/placeholder prose. A
# marker only suppresses when it is the leading token of the value (a token
# boundary follows), so legitimate in-world prose like "a list of todos" is
# never eaten. TEA-derived contract — do not widen without Architect sign-off.
_DEVNOTE_MARKERS: tuple[str, ...] = ("TODO", "FIXME", "XXX", "PLACEHOLDER", "DEV NOTE")


def _is_devnote(value: object) -> bool:
    """True when ``value`` is a string whose leading token is a dev-note marker."""
    if not isinstance(value, str):
        return False
    # Collapse internal whitespace runs (double space, tab, NBSP) to a single
    # space so multi-word markers like "DEV  NOTE" / "DEV\tNOTE" still match.
    normalized = re.sub(r"\s+", " ", value).strip()
    upper = normalized.upper()
    for marker in _DEVNOTE_MARKERS:
        if upper.startswith(marker):
            rest = normalized[len(marker) :]
            # Leading token only — the marker must be followed by a boundary,
            # not be the prefix of a longer word ("todos" must not match TODO).
            if not rest or not (rest[0].isalnum() or rest[0] == "_"):
                return True
    return False


# --- File mapping ---
# File-to-page mapping (see spec: 2026-05-23-reference-pages-design.md §File-to-Page Mapping)
RULES_FILES: tuple[str, ...] = (
    "archetypes.yaml",
    "classes.yaml",
    "rules.yaml",
    "progression.yaml",
    "magic.yaml",
    "power_tiers.yaml",
    "achievements.yaml",
    "equipment_tables.yaml",
    "inventory.yaml",
    "beat_vocabulary.yaml",
)

LORE_WORLD_FILES: tuple[str, ...] = (
    "world.yaml",
    "cultures.yaml",
    "history.yaml",
    "calendar.yaml",
    "demographics.yaml",
    "legends.yaml",
    "openings.yaml",
    "lore.yaml",
    "locations.yaml",
)

EXCLUDED_FILES: frozenset[str] = frozenset(
    {
        # Spoiler-bearing / keeper-side only
        "npcs.yaml",
        "seed_tropes.yaml",
        "tropes.yaml",
        # System-tier / metadata / asset config (not player-facing content)
        "prompts.yaml",
        "pack.yaml",
        "theme.yaml",
        "visual_style.yaml",
        "audio.yaml",
        "portrait_manifest.yaml",
        "cartography.yaml",
        "axes.yaml",
        "lethality_policy.yaml",
        "visibility_baseline.yaml",
        "char_creation.yaml",
    }
)


# --- POI / Cast loaders + R2-manifest existence gates ---


def load_points_of_interest(world_dir: Path) -> list[dict]:
    """Every ``points_of_interest`` dict authored in ``history.yaml`` (under
    ``chapters[]`` and/or top-level), in authored order.

    This is the single source shared by ``load_poi_image_slugs`` (which derives
    the R2-gate slug set) and the projection's landscape data builder (which
    surfaces the POIs as landscape cards). The POIs carry ``name``/``slug``/
    ``region``/``type``/``description`` — the same shape the legacy geography
    presenter wanted, but authored here in history.yaml, where every live world
    actually puts them (no world ships a geography.yaml/locations.yaml)."""
    path = world_dir / "history.yaml"
    if not path.exists():
        return []
    try:
        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ValueError(f"history.yaml: malformed YAML: {exc}") from exc
    if not isinstance(data, dict):
        return []
    pois: list[dict] = []
    chapters = data.get("chapters")
    if isinstance(chapters, list):
        for chapter in chapters:
            if isinstance(chapter, dict) and isinstance(chapter.get("points_of_interest"), list):
                pois.extend(p for p in chapter["points_of_interest"] if isinstance(p, dict))
    if isinstance(data.get("points_of_interest"), list):
        pois.extend(p for p in data["points_of_interest"] if isinstance(p, dict))
    return pois


def load_poi_slug_map(world_dir: Path) -> dict[str, str]:
    """Story 71-38: ``{anchor_slug: verbatim_slug}`` for each authored POI.

    The two forms are DISTINCT and serve different consumers:

    * **anchor_slug** = ``slugify(authored slug)`` (hyphen) — the
      ``location-{slug}`` / ``landscape-{slug}`` card id and the
      ``/reference/lore#location-<slug>`` deep-link anchor
      (:func:`reference_url_for_region`). Conventional, blast-radius-bearing
      (Story 63-6).
    * **verbatim_slug** = the authored ``history.yaml`` ``points_of_interest[].slug``
      VERBATIM (often underscore) — the R2 object key, because
      ``render_common.py`` writes ``<slug>.png`` from the authored slug
      unchanged. This is the key :func:`poi_image_key` must be fed (NEVER the
      slugified form) so the manifest gate matches the asset that is actually on
      R2.

    Conflating the two (slugifying the authored slug and then feeding it to
    ``poi_image_key``) is the Story 71-38 bug: a hyphen R2 key never matches the
    underscore manifest key, so every underscore-slug world emitted 0 POI
    ``<img>``. Keyed on the anchor form so a later POI cannot silently overwrite
    an earlier one whose authored slug slugifies identically."""
    mapping: dict[str, str] = {}
    for poi in load_points_of_interest(world_dir):
        raw = poi.get("slug") or poi.get("name")
        if raw:
            verbatim = str(raw)
            if anchor := slugify(verbatim):
                mapping.setdefault(anchor, verbatim)
    return mapping


def load_poi_image_slugs(world_dir: Path) -> frozenset[str]:
    """Story 63-8 / 71-38: the set of POI **anchor** slugs (``slugify``-normalised,
    hyphen) — the form the card ids and ``/reference/lore#location-<slug>`` deep-link
    anchors use (:func:`reference_url_for_region`, which matches
    ``slugify(region_id)`` against this set).

    This is the **anchor** projection of :func:`load_poi_slug_map`. The R2-object-key
    gate (:func:`_gate_poi_slugs_on_manifest`) does NOT consume this set — it takes the
    full ``{anchor: verbatim}`` map, because the R2 key is the *verbatim* authored slug,
    not the slugified anchor (Story 71-38 decouple). Kept as the anchor-only frozenset so
    the deep-link consumer in ``map_emit`` is untouched."""
    return frozenset(load_poi_slug_map(world_dir))


@lru_cache(maxsize=8)
def load_r2_manifest_keys(manifest_path: Path) -> frozenset[str]:
    """Story 65-8: the set of R2 object keys recorded in ``r2_manifest.json``
    (the Story 65-7 existence oracle). Story 65-9 reuses it for portrait keys.

    Used to gate POI and Cast-portrait image emission on the lore page so an
    authored-but-not-rendered asset never produces a broken image.

    **Caching / staleness (runbook):** ``@lru_cache(maxsize=8)`` keyed by the
    manifest ``Path`` — the result is computed once per distinct path and held
    for the process (up to 8 paths, then LRU-evicted). There is **no TTL and no
    mtime check**: a manifest regenerated after an asset upload is NOT picked up
    until the server restarts (or the cache is cleared via
    ``load_r2_manifest_keys.cache_clear()``). This is safe-failing — a just-
    rendered asset missing from a stale cached manifest renders text-only, never
    broken — but operators must restart the server after regenerating
    ``r2_manifest.json`` for new art to appear on the reference page.

    Fails loud — never returns a silently-empty set on error (No Silent
    Fallbacks): an absent file raises ``FileNotFoundError``; malformed JSON or a
    wrong-shape manifest (not a list, or an entry missing ``key``) raises
    ``ValueError``.
    """
    with manifest_path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError(
            f"r2_manifest.json: expected a JSON array of entries, got "
            f"{type(data).__name__}: {manifest_path}"
        )
    keys: set[str] = set()
    for entry in data:
        if not isinstance(entry, dict) or "key" not in entry:
            raise ValueError(f"r2_manifest.json: malformed entry (missing 'key'): {manifest_path}")
        keys.add(str(entry["key"]))
    return frozenset(keys)


def _gate_poi_slugs_on_manifest(
    slug_map: dict[str, str],
    *,
    pack: str,
    world: str,
    pack_dir: Path,
) -> frozenset[str]:
    """Story 65-8 / 71-38: filter authored POIs to those whose R2 landscape image
    is actually present in ``r2_manifest.json``, returning the surviving **anchor**
    slugs. Authored-but-not-on-R2 POIs render text-only — no broken image.

    Takes the ``{anchor_slug: verbatim_slug}`` map from :func:`load_poi_slug_map`.
    The manifest comparison feeds :func:`poi_image_key` the **verbatim** slug (the
    real R2 object key), while the returned set is the **anchor** (slugify) form the
    presenters key cards on — the Story 71-38 decouple. Feeding ``poi_image_key`` the
    anchor form was the bug: a hyphen key never matched an underscore manifest key.

    The manifest is required only when the world authors POIs; a POI-less lore
    page never consults it (so the manifest is not a hard dependency of every
    render). For a POI-bearing world, an absent/malformed manifest is a loud
    failure surfaced by :func:`load_r2_manifest_keys`. The manifest lives at the
    content root — ``pack_dir.parent.parent`` (prod:
    ``sidequest-content/r2_manifest.json``).

    Emits one ``reference_manifest_loaded`` span per render **when the world
    authors POIs** — a POI-less world short-circuits and emits no span, so the
    span count tracks feature-bearing renders, not every render.
    """
    if not slug_map:
        return frozenset()
    manifest_path = pack_dir.parent.parent / "r2_manifest.json"
    manifest_keys = load_r2_manifest_keys(manifest_path)
    world_prefix = f"genre_packs/{pack}/worlds/{world}/assets/poi/"
    world_key_count = sum(1 for key in manifest_keys if key.startswith(world_prefix))
    with reference_manifest_loaded_span(
        path=str(manifest_path),
        entry_count=len(manifest_keys),
        world_key_count=world_key_count,
    ):
        pass
    return frozenset(
        anchor
        for anchor, verbatim in slug_map.items()
        if poi_image_key(pack, world, verbatim) in manifest_keys
    )


def load_cast_entries(world_dir: Path) -> list[dict]:
    """Story 65-9: the public Cast projection — characters from
    ``portrait_manifest.yaml`` (the same file Story 65-6 reads for scene-time
    portraits). Returns ``[]`` when the world authors no manifest, so the caller
    omits the Cast section. Keeper-only ``npcs.yaml`` is never read here.

    Accepts either of the two top-level shapes the genre loader also tolerates: a
    ``{characters: [...]}`` mapping or a bare list. That two-shape tolerance is the
    only behavior shared with the genre loader — unlike the genre loader's
    ``_load_portrait_manifest`` (which ``model_validate``s each entry and would
    *raise* on a non-dict), the non-dict-item drop below is local to this function.
    A non-list ``characters:`` value is malformed first-party authoring and fails
    loud with a ``ValueError`` (No Silent Fallbacks), never an uncaught
    ``TypeError`` from iterating a scalar (Story 65-13).
    """
    path = world_dir / "portrait_manifest.yaml"
    if not path.exists():
        return []
    try:
        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ValueError(f"portrait_manifest.yaml: malformed YAML: {exc}") from exc
    if isinstance(data, dict):
        chars = data.get("characters", [])
    elif isinstance(data, list):
        chars = data
    else:
        chars = []
    if not isinstance(chars, list):
        raise ValueError(
            f"portrait_manifest.yaml: 'characters' must be a list, got "
            f"{type(chars).__name__}: {path}"
        )
    return [c for c in chars if isinstance(c, dict)]


def _cast_entry_is_projectable(entry: dict) -> bool:
    """ADR-138 §D4 ratification gate for a public Cast entry.

    A ``portrait_manifest.yaml`` entry is projectable onto the public reference
    page iff it is **ratified** — exactly the rule the ADR-118 retrieval index
    applies (75-12). Reuses the 75-11 single-source predicate
    (:func:`sidequest.game.npc_pool.is_projectable`) rather than re-deriving the
    rule: the YAML dict is adapted to a minimal :class:`NpcPoolMember` carrying
    only the gate-relevant fields (the manifest also carries author-facing keys
    like ``role``/``appearance``/``id`` that ``NpcPoolMember`` forbids, so the
    whole dict cannot be splatted in).

    Authored manifest content is never auto-minted, so ``observation_pending`` is
    ``False`` by design and this returns ``True`` in practice; the gate exists
    defensively so a future unratified entry cannot leak a phantom onto a
    player-facing page (No Silent Fallbacks — the skip is counted on an OTEL span,
    never silently rendered).

    The raw ``observation_pending`` value is handed to ``NpcPoolMember`` for
    Pydantic coercion rather than pre-wrapped in ``bool()``: ``bool("false")`` is
    ``True`` (every non-empty string is truthy), so a quoted-string authoring slip
    (``observation_pending: "false"``) would otherwise *silently withhold a
    ratified NPC*. Pydantic v2 coerces ``"false"``/``"true"``/``0``/``1`` to the
    correct bool and raises loudly on unsalvageable input. An explicit ``null``
    (Python ``None``) is coalesced to the ``False`` default — ``null`` means
    "unset", identical to an absent key, and must render rather than raise a
    ValidationError that would 500 the public page."""
    raw_pending = entry.get("observation_pending", False)
    member = NpcPoolMember(
        name=str(entry.get("name", "")),
        drawn_from="world_authored",
        observation_pending=False if raw_pending is None else raw_pending,
    )
    return npc_pool.is_projectable(member)


def _gate_cast_slugs_on_manifest(
    authored_slugs: frozenset[str],
    *,
    pack: str,
    world: str,
    pack_dir: Path,
) -> frozenset[str]:
    """Story 65-9: filter authored Cast portrait slugs to those whose R2 portrait
    image is present in ``r2_manifest.json``. Authored-but-not-on-R2 NPCs render
    text-only — no broken image. Portrait analog of
    :func:`_gate_poi_slugs_on_manifest`; same loaded-once manifest, same
    ``pack_dir.parent.parent`` discovery, same loud-failure contract.

    Emits one ``reference_manifest_loaded`` span when the world authors cast
    NPCs; a cast-less world short-circuits and emits no span.
    """
    if not authored_slugs:
        return frozenset()
    manifest_path = pack_dir.parent.parent / "r2_manifest.json"
    manifest_keys = load_r2_manifest_keys(manifest_path)
    world_prefix = f"genre_packs/{pack}/worlds/{world}/assets/portraits/"
    world_key_count = sum(1 for key in manifest_keys if key.startswith(world_prefix))
    with reference_manifest_loaded_span(
        path=str(manifest_path),
        entry_count=len(manifest_keys),
        world_key_count=world_key_count,
    ):
        pass
    return frozenset(
        slug for slug in authored_slugs if portrait_image_key(pack, world, slug) in manifest_keys
    )
