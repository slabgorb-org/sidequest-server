"""World-over-genre resolution for chargen equipment tables (story 120-4).

Parallel to :func:`sidequest.server.dispatch.inventory_resolve.resolve_inventory`
and :func:`sidequest.server.dispatch.class_resolve.resolve_classes`: the genre tier
is the SRD chargen-kit rulebook, and a world may ship
``worlds/<slug>/equipment_tables.yaml`` to add dungeon/flavor gear that a verbatim
genre baseline cannot carry (ADR-140; the caverns_and_claudes case, story 120-1).

Merge semantics (Keith's ruling 2026-06-15):
  * ``class_tables`` — per-slot APPEND within each kit (genre items first, then the
    world's); a slot present only in the world is added; a kit present only in the
    world is added.
  * ``guaranteed_grants`` — APPEND by kit id.
  * ``tables`` — per-slot APPEND (the ``random_table`` fallback flow).
  * ``rolls_per_slot`` — world OVERRIDES per key.

ADDITIVE: a world that ships no ``equipment_tables.yaml`` resolves to the genre tier
unchanged, so nothing changes for unmigrated worlds.
"""

from __future__ import annotations

from sidequest.genre.models.character import EquipmentTables
from sidequest.genre.models.pack import GenrePack


def _append_by_key(genre: dict[str, list], world: dict[str, list]) -> dict[str, list]:
    """Union the keys (genre order first, then world-only) appending world values
    after genre values per key."""
    keys = list(genre) + [k for k in world if k not in genre]
    return {k: list(genre.get(k, [])) + list(world.get(k, [])) for k in keys}


def _merge_equipment_tables(genre: EquipmentTables, world: EquipmentTables) -> EquipmentTables:
    class_tables: dict[str, dict[str, list[str]]] = {}
    kit_ids = list(genre.class_tables) + [
        k for k in world.class_tables if k not in genre.class_tables
    ]
    for kit_id in kit_ids:
        class_tables[kit_id] = _append_by_key(
            genre.class_tables.get(kit_id, {}), world.class_tables.get(kit_id, {})
        )
    return EquipmentTables(
        tables=_append_by_key(genre.tables, world.tables),
        # rolls_per_slot is a per-slot count, not a list — world overrides per key.
        rolls_per_slot={**genre.rolls_per_slot, **world.rolls_per_slot},
        class_tables=class_tables,
        guaranteed_grants=_append_by_key(genre.guaranteed_grants, world.guaranteed_grants),
    )


def resolve_equipment_tables(
    pack: GenrePack,
    world_slug: str | None,
) -> EquipmentTables | None:
    """Return the chargen equipment tables for a connection, world-merged over genre.

    Falsy ``world_slug``, unknown worlds, and worlds with no
    ``equipment_tables.yaml`` resolve to the pure genre tier (``pack.equipment_tables``,
    possibly ``None``). When a world ships an override it is merged over the genre per
    the module-docstring semantics.
    """
    genre = pack.equipment_tables
    world: EquipmentTables | None = None
    if world_slug:
        w = pack.worlds.get(world_slug)
        if w is not None:
            world = w.equipment_tables

    if world is None:
        # Pure genre tier (or nothing at either tier). Negative-confirmation span.
        _emit_resolved(world_slug=world_slug or "", config=genre)
        return genre

    merged = _merge_equipment_tables(genre, world) if genre is not None else world
    _emit_merged(world_slug=world_slug or "", config=merged)
    return merged


def _emit_resolved(*, world_slug: str, config: EquipmentTables | None) -> None:
    """Emit a ``state_transition`` span for the pure-genre (no-merge) path so the GM
    panel can confirm the resolver ran and is not improvising (OTEL Observability
    Principle). Mirrors ``inventory_resolve._emit_inventory_resolved``."""
    from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

    _watcher_publish(
        "state_transition",
        {
            "field": "resolved_equipment_tables",
            "op": "resolved",
            "world_slug": world_slug,
            "tier": "genre",
            "kit_count": 0 if config is None else len(config.class_tables),
            "has_config": config is not None,
        },
        component="genre",
    )


def _emit_merged(*, world_slug: str, config: EquipmentTables) -> None:
    """Emit a ``state_transition`` span for the world∪genre merge path (``op="merged"``)
    so the GM panel can prove the world tier engaged. Mirrors
    ``inventory_resolve._emit_inventory_merged``."""
    from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

    _watcher_publish(
        "state_transition",
        {
            "field": "resolved_equipment_tables",
            "op": "merged",
            "world_slug": world_slug,
            "tier": "world",
            "kit_count": len(config.class_tables),
            "grant_kit_count": len(config.guaranteed_grants),
            "has_config": True,
        },
        component="genre",
    )
