"""WWN spell-catalog resolver — world-tier precedence over genre-tier.

Epic 94 (genre/world boundary correction, supersedes ADR-120
"mechanics-in-genre"): a world's WWN spell catalog is a world-tier
CAST/CATALOG surface — the catalog of magic a world ships — not a genre
mechanic. The genre tier is the rulebook only (resolution rules + the WWN
magic block on ``rules.wwn``).

Mirrors :mod:`sidequest.server.dispatch.class_resolve`: when the selected
world declares its own ``wwn_spell_catalog`` (``worlds/<slug>/spells_wwn.yaml``),
that replaces the genre-level catalog wholesale. There is no merge.
World-empty (or world-not-in-pack) falls through to the genre-tier
``pack.wwn_spell_catalog`` (which, for migrated packs, is itself the union
of every world's catalog — see ``loader.load_genre_pack``).

Emits a ``state_transition`` watcher event recording which tier the
catalog was resolved from, so the GM panel can prove the cast pipeline
read the spell catalog from the world tier rather than improvising it.
"""

from __future__ import annotations

from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.wwn_spell import WwnSpellCatalog


def resolve_wwn_spell_catalog(
    pack: GenrePack,
    world_slug: str | None,
) -> WwnSpellCatalog | None:
    """Return the WWN spell catalog for a connection/turn.

    World-tier when ``pack.worlds[world_slug].wwn_spell_catalog`` is
    present; otherwise genre-tier ``pack.wwn_spell_catalog``. The world
    catalog **replaces** the genre catalog — it is not merged. Falsy
    ``world_slug`` (``None`` or empty string) and unknown worlds both
    fall through to the genre tier. ``None`` when neither tier ships a
    catalog (a valid state for a non-wwn or catalog-free pack — no silent
    fallback to a fabricated catalog).
    """
    tier = "genre"
    if world_slug:
        world = pack.worlds.get(world_slug)
        if world is not None and world.wwn_spell_catalog is not None:
            resolved = world.wwn_spell_catalog
            _emit_catalog_resolved(
                world_slug=world_slug,
                tier="world",
                spell_count=len(resolved.spells),
            )
            return resolved
    resolved = pack.wwn_spell_catalog
    _emit_catalog_resolved(
        world_slug=world_slug or "",
        tier=tier,
        spell_count=len(resolved.spells) if resolved is not None else 0,
    )
    return resolved


def _emit_catalog_resolved(*, world_slug: str, tier: str, spell_count: int) -> None:
    """Emit a ``state_transition`` span recording the spell-catalog tier.

    OTEL Observability Principle: every world-tier resolution decision
    fires a span so the GM panel can confirm the cast pipeline got its
    spell catalog from the world tier (epic 94) and is not improvising.
    """
    from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

    _watcher_publish(
        "state_transition",
        {
            "field": "wwn_spell_catalog",
            "op": "resolved",
            "world_slug": world_slug,
            "tier": tier,
            "spell_count": spell_count,
        },
        component="genre",
    )
