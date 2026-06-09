"""Reference-page asset-key + slug helpers for the JSON projection.

Story 100-12 (Phase 4 cutover) retired the HTML presenter layer: the
``PresenterContext`` dispatch, the ``PRESENTERS`` registry, and every
``present_*`` function that emitted markup were deleted when the server got out
of the reference-HTML business (the React SPA now renders every section from the
JSON projection). What survives are the three pure helpers
``reference_projection.py`` and ``reference_renderer.py`` import to address R2
assets and key Cast entries:

* :func:`poi_image_key` / :func:`portrait_image_key` — the canonical raw R2
  object keys the manifest existence gates compare against, and
* :func:`cast_portrait_slug` — the portrait-key slug derivation for a Cast entry.

No function here emits markup.
"""

from __future__ import annotations

from sidequest.server.utils import slugify_player_name


def poi_image_key(pack: str, world: str, slug: str) -> str:
    """Canonical **raw R2 object key** for a POI landscape image.

    Single source of truth shared by the projection's image data and the Story
    65-8 manifest gate in ``reference_renderer``. They MUST agree on this format —
    if they drift, the gate would pass on a key the consumer never requests (or
    vice versa), silently breaking image emission.

    Returns a raw R2 key, NOT a URL: the gate compares it **directly** against
    ``r2_manifest.json`` keys (do not wrap), while the consumer wraps it in
    ``resolve_asset_url`` to build the ``src``. Wrapping on the gate side would
    never match a raw manifest key.
    """
    return f"genre_packs/{pack}/worlds/{world}/assets/poi/{slug}.png"


def portrait_image_key(pack: str, world: str, slug: str) -> str:
    """Canonical **raw R2 object key** for an NPC portrait image (Story 65-9).

    Portrait analog of :func:`poi_image_key`. Returns the **world-scoped** key
    that the portrait render script writes and that Story 65-6's
    ``_resolve_npc_portrait_url`` already constructs — so the Cast section's
    image and the 65-9 manifest gate agree by construction. ``slug`` is
    ``slugify_player_name(name)`` (the daemon-mirroring rule), so URL == filename.

    Like ``poi_image_key`` this is a raw key: the gate compares it directly to
    ``r2_manifest.json`` (no wrap); the consumer wraps it in ``resolve_asset_url``
    for the ``src``.
    """
    return f"genre_packs/{pack}/worlds/{world}/assets/portraits/{slug}.png"


def cast_portrait_slug(item: dict) -> str:
    """The portrait-key slug for a Cast manifest entry, decoupled from heading.

    Prefers the entry's explicit ``id`` (the portrait-key slug that keys the R2
    portrait ``<slug>.png``) when present and non-empty; otherwise derives it
    from the display ``name`` via ``slugify_player_name`` (the historical
    behavior every other world relies on, where ``name`` is authored as the
    display name and no ``id`` is present).

    This is the single derivation shared by the projection's Cast data (which
    keys the portrait image) and the R2 existence gate (which gates the slug set)
    so the gated set and the per-card key always agree. The ``id``-or-
    ``slugify(name)`` fallback is a schema-optional field with a deterministic
    derivation, not a silent config fallback."""
    raw_id = str(item.get("id", "")).strip()
    if raw_id:
        return raw_id
    return slugify_player_name(str(item.get("name", "")))
