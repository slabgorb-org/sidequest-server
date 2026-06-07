"""Inventory catalog resolver — world-tier precedence over genre-tier.

Epic 94 (genre/world boundary correction, supersedes ADR-120
"mechanics-in-genre"): a world's item catalog, class starting-kits, starting
gold, and currency are a world-tier CAST/CATALOG surface — the loot a world
ships — not a genre mechanic. The genre tier is the rulebook only.

Mirrors :func:`sidequest.server.dispatch.class_resolve.resolve_classes`: when
the selected world declares its own ``inventory.yaml``, that replaces the
genre-level inventory wholesale. There is no merge — a world's catalog,
starting_equipment, starting_gold, and currency are taken as a complete unit,
because the same class id (e.g. "Soldier") maps to a *different* loadout per
world and currencies diverge, so merging would cross-contaminate worlds.
World-empty (or world-not-in-pack) falls through to the genre-tier
``pack.inventory`` (which packs like caverns_and_claudes still ship).

Emits a ``state_transition`` watcher event recording which tier the inventory
was resolved from, so the GM panel can prove the chargen loadout / currency /
gained-item catalog read from the world tier rather than improvising it.
"""

from __future__ import annotations

from sidequest.genre.models.inventory import InventoryConfig
from sidequest.genre.models.pack import GenrePack


def resolve_inventory(
    pack: GenrePack,
    world_slug: str | None,
) -> InventoryConfig | None:
    """Return the inventory config for a connection.

    World-tier when ``pack.worlds[world_slug].inventory`` is non-None;
    otherwise genre-tier ``pack.inventory``. The world inventory **replaces**
    the genre one — it is not merged. Falsy ``world_slug`` (``None`` or empty
    string) and unknown worlds both fall through to the genre tier. ``None`` is
    a valid return (a pack with no inventory at either tier); callers that wire
    a loadout already treat ``None`` as a no-op (see
    ``chargen_loadout.apply_starting_loadout``).
    """
    tier = "genre"
    if world_slug:
        world = pack.worlds.get(world_slug)
        if world is not None and world.inventory is not None:
            _emit_inventory_resolved(world_slug=world_slug, tier="world", config=world.inventory)
            return world.inventory
    _emit_inventory_resolved(world_slug=world_slug or "", tier=tier, config=pack.inventory)
    return pack.inventory


def _emit_inventory_resolved(*, world_slug: str, tier: str, config: InventoryConfig | None) -> None:
    """Emit a ``state_transition`` span recording the inventory tier.

    OTEL Observability Principle: every world-tier resolution decision fires a
    span so the GM panel can confirm the chargen loadout / currency / gained-item
    catalog got its inventory from the world tier (epic 94) and is not
    improvising. ``config=None`` (no inventory at either tier) still fires the
    span — negative confirmation that the resolver ran.
    """
    from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

    _watcher_publish(
        "state_transition",
        {
            "field": "resolved_inventory",
            "op": "resolved",
            "world_slug": world_slug,
            "tier": tier,
            "catalog_count": len(config.item_catalog) if config is not None else 0,
            "class_kit_count": len(config.starting_equipment) if config is not None else 0,
            "has_config": config is not None,
        },
        component="genre",
    )
