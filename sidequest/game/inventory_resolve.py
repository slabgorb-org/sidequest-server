"""Inventory catalog resolver — genre baseline ∪ world catalog, per-field merge.

ADR-145 D3 (supersedes the epic-94 wholesale-replace seam): the genre tier holds
the SRD baseline catalog (the shared core every world of that genre inherits) and
is **non-droppable**. A world's ``inventory.yaml`` no longer replaces the catalog
wholesale — it **merges over the baseline by item ``id``**:

- ``item_catalog`` unions by ``id``. Genre-only ids survive; world-only ids are
  added; a shared id merges **per-field** against the baseline entry:
    * **Mechanical fields** (``category``, ``damage``, ``armor_class``,
      ``mitigation``, ``tech_level``, ``range_band``, ``magazine``, ``weight``,
      ``value``, ``resource_ticks``, ``heal_amount``) inherit and lock from the genre
      baseline. The world MUST NOT re-stat a ``mode=verbatim`` baseline item —
      a world override that changes a locked mechanical field on a verbatim item
      is a hard error (``VerbatimFieldLockError``; No Silent Fallbacks, the
      ADR-143 forbidden re-tune).
    * **Presentation fields** (``name``, ``description``, ``lore``,
      ``narrative_weight``, plus ``tags``/``rarity``/``power_level``) take the
      world override when present, else inherit.
    * **Provenance is preserved** from the baseline — a reskin is still the SRD
      item mechanically, so ``mode``/``srd``/``srd_ref`` stay the baseline's.
- ``starting_equipment`` / ``starting_gold`` / ``currency`` keep
  **world-replaces-genre** (these are world-kit choices, genuinely world-owned,
  ADR-140-correct — the same class id maps to a different loadout per world and
  currencies diverge, so unioning them would cross-contaminate worlds).

A world that ships no ``inventory.yaml`` (or is absent from the pack) resolves to
the pure genre baseline. A genre with no baseline catalog leaves the world catalog
standing alone (nothing to merge into).

Emits a ``state_transition`` watcher event recording the merge decision and its
counts, so the GM panel can prove the chargen loadout / currency / gained-item
catalog read the merged catalog rather than improvising it.
"""

from __future__ import annotations

from sidequest.genre.models.inventory import CatalogItem, InventoryConfig
from sidequest.genre.models.pack import GenrePack

# Mechanical fields inherit-and-lock from the genre baseline (ADR-145 D3). A
# world override of a ``mode=verbatim`` baseline item MUST NOT change any of
# these. Presentation fields (name/description/lore/narrative_weight + cosmetic
# tags/rarity/power_level) take the world override and are NOT locked.
_MECHANICAL_FIELDS = (
    "category",
    "damage",
    "armor_class",
    "mitigation",
    "tech_level",
    "range_band",
    "magazine",
    "weight",
    "value",
    "resource_ticks",
    "heal_amount",
    "system_strain",  # CWN cyberware strain cost — SRD-bound, locked (114-5)
)
_PRESENTATION_FIELDS = (
    "name",
    "description",
    "lore",
    "narrative_weight",
    "tags",
    "rarity",
    "power_level",
)

# A field-default on a world item means "the world did not author this field," so
# it does not override the baseline and does not trip the verbatim lock. This lets
# a presentation-only reskin omit the mechanics (value/weight sit at the model
# defaults, damage stays None) without being read as a re-stat.
_FIELD_DEFAULTS: dict[str, object] = {
    "category": "",
    "damage": None,
    "armor_class": None,
    "mitigation": None,
    "tech_level": None,
    "range_band": None,
    "magazine": None,
    "weight": 0.0,
    "value": 0,
    "resource_ticks": None,
    "heal_amount": None,
    "system_strain": None,
}


class VerbatimFieldLockError(ValueError):
    """A world override changed a locked mechanical field of a verbatim baseline.

    ADR-145 D3/D4 (the ADR-143 forbidden re-tune): a ``mode=verbatim`` baseline
    item's mechanical envelope is SRD-bound and may not be re-stated by a world.
    Presentation may be reskinned freely; mechanics may not. Fail loud.
    """


def _world_overrides_field(world_item: CatalogItem, field: str) -> bool:
    """True when the world item authored ``field`` (i.e. set it off its default).

    A model-default value (``None``/``0``/``0.0``) counts as "not authored," so a
    presentation-only reskin that omits mechanics does not override or lock.
    """
    return getattr(world_item, field) != _FIELD_DEFAULTS[field]


def _merge_item(baseline: CatalogItem, world_item: CatalogItem) -> CatalogItem:
    """Merge a shared-id world item over its genre baseline, per-field.

    Mechanical fields inherit+lock from the baseline; presentation fields take the
    world override when authored; provenance is preserved from the baseline.
    Raises :class:`VerbatimFieldLockError` if the world re-stats a mechanical field
    of a ``mode=verbatim`` baseline item.
    """
    is_verbatim = baseline.provenance is not None and baseline.provenance.mode == "verbatim"

    merged: dict[str, object] = baseline.model_dump()
    # Mechanical fields stay the baseline's (already copied above). For verbatim
    # baselines, a world attempt to change one is a hard, named error.
    if is_verbatim:
        for field in _MECHANICAL_FIELDS:
            if _world_overrides_field(world_item, field) and getattr(world_item, field) != getattr(
                baseline, field
            ):
                raise VerbatimFieldLockError(
                    f"world override of verbatim item {baseline.id!r} may not change "
                    f"locked mechanical field {field!r} "
                    f"(baseline={getattr(baseline, field)!r}, world={getattr(world_item, field)!r}); "
                    f"a verbatim SRD item's mechanics are locked (ADR-145 D3)"
                )
    # Presentation fields take the world override when the world authored them.
    for field in _PRESENTATION_FIELDS:
        world_value = getattr(world_item, field)
        if world_value not in (None, "", [], 0):
            merged[field] = world_value
    # Provenance is preserved from the baseline — a reskin is still the SRD item.
    merged["provenance"] = (
        baseline.provenance.model_dump() if baseline.provenance is not None else None
    )
    return CatalogItem.model_validate(merged)


def merge_inventory_catalog(
    baseline: list[CatalogItem],
    world: list[CatalogItem],
) -> tuple[list[CatalogItem], dict[str, int]]:
    """Union genre ``baseline`` and ``world`` catalogs by ``id``, per-field.

    Returns ``(merged_list, counts)``. ``counts`` carries
    ``baseline_catalog_count`` (baseline size), ``world_override_count``
    (shared ids the world reskinned), and ``world_added_count`` (world-only ids).
    Genre-only baseline ids are retained verbatim; shared ids merge per-field via
    :func:`_merge_item` (raising on a verbatim mechanical re-stat); world-only ids
    are appended. Baseline order is preserved, world-added ids follow in order.
    """
    by_id: dict[str, CatalogItem] = {item.id: item for item in baseline}
    override_count = 0
    added: list[CatalogItem] = []
    for world_item in world:
        if world_item.id in by_id:
            by_id[world_item.id] = _merge_item(by_id[world_item.id], world_item)
            override_count += 1
        else:
            added.append(world_item)

    merged = list(by_id.values()) + added
    counts = {
        "baseline_catalog_count": len(baseline),
        "world_override_count": override_count,
        "world_added_count": len(added),
    }
    return merged, counts


def resolve_inventory(
    pack: GenrePack,
    world_slug: str | None,
) -> InventoryConfig | None:
    """Return the resolved inventory config for a connection.

    The ``item_catalog`` is the genre baseline **unioned** with the world catalog
    by ``id`` (genre baseline non-droppable; world wins per-field; see ADR-145 D3
    and the module docstring). The non-catalog fields — ``currency``,
    ``starting_equipment``, ``starting_gold`` — keep **world-replaces-genre**: when
    the world ships an inventory they are taken from the world wholesale (the same
    class id maps to a different loadout per world). Falsy ``world_slug`` (``None``
    or empty string), unknown worlds, and worlds with no ``inventory.yaml`` resolve
    to the pure genre baseline. ``None`` is a valid return (no inventory at either
    tier); callers that wire a loadout already treat ``None`` as a no-op (see
    ``chargen_loadout.apply_starting_loadout``).

    Raises :class:`VerbatimFieldLockError` if a shared-id world override re-stats a
    locked mechanical field of a ``mode=verbatim`` baseline item.
    """
    world_inv: InventoryConfig | None = None
    if world_slug:
        world = pack.worlds.get(world_slug)
        if world is not None and world.inventory is not None:
            world_inv = world.inventory

    if world_inv is None:
        # Pure genre baseline (or no inventory at either tier).
        _emit_inventory_resolved(world_slug=world_slug or "", tier="genre", config=pack.inventory)
        return pack.inventory

    baseline_catalog = pack.inventory.item_catalog if pack.inventory is not None else []
    merged_catalog, counts = merge_inventory_catalog(baseline_catalog, world_inv.item_catalog)

    # Catalog merges; currency / starting_equipment / starting_gold replace from
    # the world wholesale. ``ship_weapons`` is genre-tier-only (story 114-15) — carry
    # it from the genre baseline so a world that ships its own inventory.yaml (and so
    # hits this merge path, which model_copies from the WORLD config) still inherits
    # the dogfight ship weapon instead of silently dropping it.
    genre_ship_weapons = pack.inventory.ship_weapons if pack.inventory is not None else []
    resolved = world_inv.model_copy(
        update={"item_catalog": merged_catalog, "ship_weapons": list(genre_ship_weapons)}
    )

    _emit_inventory_merged(world_slug=world_slug or "", config=resolved, counts=counts)
    return resolved


def _emit_inventory_resolved(*, world_slug: str, tier: str, config: InventoryConfig | None) -> None:
    """Emit a ``state_transition`` span for a non-merge (pure-tier) resolution.

    Fires on the pure-genre-baseline path and the world-ships-no-inventory path
    (``op="resolved"``). OTEL Observability Principle: every resolution decision
    fires a span so the GM panel can confirm the chargen loadout / currency /
    gained-item catalog got its inventory from the resolver and is not improvising.
    ``config=None`` (no inventory at either tier) still fires — negative
    confirmation that the resolver ran.
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


def _emit_inventory_merged(
    *, world_slug: str, config: InventoryConfig, counts: dict[str, int]
) -> None:
    """Emit a ``state_transition`` span for the world∪baseline merge path.

    ADR-145 D3 + OTEL Observability Principle: the merge decision records
    ``op="merged"`` so the GM panel can prove a union merge engaged (not a
    wholesale replace), with the baseline / world-override / world-added counts and
    the resolved union size (``catalog_count``).
    """
    from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

    _watcher_publish(
        "state_transition",
        {
            "field": "resolved_inventory",
            "op": "merged",
            "world_slug": world_slug,
            "tier": "world",
            "catalog_count": len(config.item_catalog),
            "class_kit_count": len(config.starting_equipment),
            "has_config": True,
            **counts,
        },
        component="genre",
    )
