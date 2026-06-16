"""Resolve a narrator-gained item against the authored item catalog.

When the narrator reports an ``items_gained`` entry, the item-gain path in
``server/narration_apply.py`` mints a bare ``narrator:{slug}`` dict by
default — flavor only, with zero mechanical attributes and an id that no
authored-catalog lookup can match. This module gives that path a resolution
step: if the gained item's name/id matches an authored ``CatalogItem`` in the
pack's ``inventory.item_catalog``, build the runtime item dict from the
catalog entry instead.

Why it matters (playtest 2026-06-02): a narrator-gained "Blaster Rifle"
arrived as ``narrator:blaster_rifle`` with no ``damage`` and an id that
``damage_roll``'s Priority-3 catalog lookup (keyed on the *authored* id
``blaster_rifle``) could never hit — so the weapon was mechanically inert.
Resolving against the catalog sets the authored id AND serialises the
mechanical fields (damage / mitigation / armor_class), so a gained
weapon/armor is combat-live immediately.

This is the gained-item analogue of the chargen-loadout resolver
(``server/dispatch/chargen_loadout._item_dict_from_catalog``); it additionally
carries the mechanical fields that the chargen shape omits, because a gained
weapon/armor must be usable the moment it lands.
"""

from __future__ import annotations

from sidequest.genre.models.inventory import CatalogItem

# Authored ``narrative_weight`` is a string tier or a number; the runtime item
# dict uses a float (the narrator-mint default is 0.5). This mapping is
# cosmetic today — no runtime consumer reads item ``narrative_weight`` yet
# (P2-deferred, see ``game/creature_core.Inventory``) — but keeps the field
# numeric and ordered so a future consumer needn't special-case strings.
_NARRATIVE_WEIGHT_TIERS = {
    "trivial": 0.1,
    "minor": 0.3,
    "standard": 0.5,
    "major": 0.7,
    "legendary": 0.9,
}
_DEFAULT_NARRATIVE_WEIGHT = 0.5


def _slugify(name: str) -> str:
    """Mirror ``narration_apply._narrator_item_dict``'s id slug rule."""
    return name.strip().lower().replace(" ", "_").replace("-", "_")


def _coerce_narrative_weight(raw: object) -> float:
    if isinstance(raw, bool):  # bool is an int subclass — exclude explicitly
        return _DEFAULT_NARRATIVE_WEIGHT
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        return _NARRATIVE_WEIGHT_TIERS.get(raw.strip().casefold(), _DEFAULT_NARRATIVE_WEIGHT)
    return _DEFAULT_NARRATIVE_WEIGHT


def item_dict_from_catalog(item: CatalogItem, *, quantity: int = 1) -> dict:
    """Build a combat-live runtime item dict from an authored ``CatalogItem``.

    Preserves the authored ``id`` (so id-keyed catalog lookups bind) and
    serialises the mechanical fields (``damage`` / ``mitigation`` /
    ``armor_class``) when present so a gained weapon/armor works immediately.
    """
    item_dict: dict = {
        "id": item.id,
        "name": item.name,
        "description": item.description,
        "category": item.category,
        "value": int(item.value),
        "weight": float(item.weight),
        "rarity": item.rarity.strip() or "common",
        "narrative_weight": _coerce_narrative_weight(item.narrative_weight),
        "tags": list(item.tags),
        "equipped": False,
        "quantity": max(1, quantity),
        "uses_remaining": item.resource_ticks,
        "state": "Carried",
    }
    if item.damage is not None:
        item_dict["damage"] = item.damage.model_dump()
    if item.mitigation is not None:
        item_dict["mitigation"] = item.mitigation
    if item.armor_class is not None:
        item_dict["armor_class"] = item.armor_class
    # Story 106-4: a narrator-granted consumable carries its heal effect so it
    # works the moment it's picked up (parity with the chargen kit path).
    if item.heal_amount:
        item_dict["heal_amount"] = item.heal_amount
    return item_dict


def resolve_gained_item_dict(entry: dict, catalog: list[CatalogItem] | None) -> dict | None:
    """Return a runtime item dict built from a matching ``CatalogItem``, else None.

    Matching is conservative and exact — never fuzzy — so "a sword" can never
    bind to "Vorpal Blade of Doom". Match order: explicit (``narrator:``-stripped)
    id, name-slug, then case-folded name. ``None`` signals "no authored match";
    the caller mints the bare narrator dict (current behaviour).
    """
    if not catalog:
        return None
    name = str(entry.get("name", "") or "").strip()
    if not name:
        return None
    # Collapse internal whitespace runs so stray narrator spacing
    # ("Blaster  Rifle") still resolves to the authored "Blaster Rifle".
    name = " ".join(name.split())

    entry_id = str(entry.get("id", "") or "").strip()
    if entry_id.startswith("narrator:"):
        entry_id = entry_id[len("narrator:") :]
    slug = _slugify(name)

    by_id = {item.id: item for item in catalog}
    match = by_id.get(entry_id) or by_id.get(slug)
    if match is None:
        by_name = {item.name.strip().casefold(): item for item in catalog}
        match = by_name.get(name.casefold())
    if match is None:
        return None

    try:
        quantity = int(entry.get("quantity", 1) or 1)
    except (TypeError, ValueError):
        quantity = 1

    return item_dict_from_catalog(match, quantity=quantity)
