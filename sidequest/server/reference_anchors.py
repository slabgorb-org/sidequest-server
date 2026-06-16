"""Pure URL builders + kind-specific helpers for the reference surface.

The builders compose the page path with a hash fragment derived from
``slugify`` (shared with the renderer). They do NOT validate pack/world
existence — callers attach URLs only when they already hold a session
bound to a known pack/world, so existence validation is the renderer's
job at HTTP-handle time.

Kind-specific helpers (``reference_url_for_*``) encode the v2 routing rule:
mechanics link to /reference/rules/<pack>; content links to
/reference/lore/<pack>/<world>. A helper returns ``None`` when the kind
does not map to a rendered page or the keyed entity is not present in
the caller-supplied registry.
"""

from __future__ import annotations

from sidequest.server.reference_slug import slugify


def build_rules_url(pack: str, kind: str, *keys: str) -> str:
    """Construct a rules-page URL with a kind-namespaced fragment.

    Example:
        build_rules_url("p", "class", "Burglar", "signature", "Cosh")
        -> "/reference/rules/p#class-burglar-signature-cosh"
    """
    if not keys:
        raise ValueError("build_rules_url requires at least one key segment")
    segments = [kind, *keys]
    fragment = "-".join(slugify(s) for s in segments)
    return f"/reference/rules/{pack}#{fragment}"


def build_lore_url(pack: str, world: str, kind: str, *keys: str) -> str:
    """Construct a lore-page URL with a kind-namespaced fragment."""
    if not keys:
        raise ValueError("build_lore_url requires at least one key segment")
    segments = [kind, *keys]
    fragment = "-".join(slugify(s) for s in segments)
    return f"/reference/lore/{pack}/{world}#{fragment}"


# --- Kind-specific helpers --------------------------------------------------


def reference_url_for_class(pack: str, class_name: str) -> str:
    """URL to a class section on the rules page."""
    return build_rules_url(pack, "class", class_name)


def reference_url_for_ability(
    *,
    pack: str,
    source: str,
    ability_name: str,
    owning_class_name: str | None,
) -> str | None:
    """URL to a class signature ability, or None if not a class-source ability.

    Only ``source == "Class"`` produces a URL, and only when the caller
    knows which class owns the ability. Race / Item / Play sources, or a
    class-source ability whose owner cannot be determined, return None
    (the UI renders plain text in that case).
    """
    if source != "Class" or not owning_class_name:
        return None
    return build_rules_url(pack, "class", owning_class_name, "signature", ability_name)


def reference_url_for_journal_entry(
    *,
    pack: str,
    world: str,
    category: str,
    content: str,
    legend_names: tuple[str, ...] = (),
    history_entries: tuple[str, ...] = (),
    location_names: tuple[str, ...] = (),
) -> str | None:
    """Map a journal entry to a lore-page URL.

    Routing:
      - Lore     -> first legend match, then history match, else None
      - Place    -> location match, else None
      - Person   -> None (npcs.yaml is excluded from rendering)
      - Quest    -> None (no rendered yaml)
      - Ability  -> handled by reference_url_for_ability instead; here None
    """
    if category == "Lore":
        if content in legend_names:
            return build_lore_url(pack, world, "legend", content)
        if content in history_entries:
            return build_lore_url(pack, world, "history", content)
        return None
    if category == "Place":
        if content in location_names:
            return build_lore_url(pack, world, "location", content)
        return None
    return None


def reference_url_for_location_entity(
    *,
    pack: str,
    world: str,
    entity_name: str,
    known_location_names: tuple[str, ...],
) -> str | None:
    """URL to a named location entity, or None if the world has no such location."""
    if entity_name not in known_location_names:
        return None
    return build_lore_url(pack, world, "location", entity_name)


def reference_url_for_region(
    *,
    pack: str,
    world: str,
    region_id: str,
    known_location_slugs: frozenset[str],
) -> str | None:
    """URL to the lore-page anchor for a region's header, or None.

    Story 63-6. ``known_location_slugs`` is the set of slugify-normalised
    location slugs that have a ``/reference/lore#location-<slug>`` anchor —
    the world's ``history.yaml`` ``points_of_interest`` manifest (Story 63-8).
    The region links only when its slug is in that set; otherwise None, so the
    UI renders the header as plain text rather than a dead link (no guessed URL).
    """
    if slugify(region_id) not in known_location_slugs:
        return None
    return build_lore_url(pack, world, "location", region_id)
