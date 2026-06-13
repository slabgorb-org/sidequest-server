"""Starting-equipment loadout wiring for chargen confirmation.

Port of the loadout block inside ``dispatch_character_creation`` in
``sidequest-api/crates/sidequest-server/src/dispatch/connect.rs``
(lines ~1745-1864): after ``builder.build()`` produces a Character with
only builder-side item_hints, this module wires the class-specific
starting equipment from ``pack.inventory.starting_equipment`` into the
character's inventory, resolving item ids through ``item_catalog`` and
accumulating ``starting_gold``.

The Python ``Inventory.items`` field is a ``list[dict]`` (Phase 1
simplification — Rust uses the typed ``Item`` struct). Item dicts
produced here mirror the Rust ``Item`` JSON shape so save-file parity
holds across the port.
"""

from __future__ import annotations

import logging

from opentelemetry import trace as otel_trace

from sidequest.game.character import Character
from sidequest.game.vessel_tags import bind_rig_pool_from_inventory
from sidequest.genre.models.inventory import CatalogItem, InventoryConfig
from sidequest.telemetry.spans import (
    SPAN_CHARGEN_STARTING_EQUIPMENT_MISSING,
    SPAN_CHARGEN_STARTING_KIT_DEDUP_EVALUATED,
    SPAN_CHARGEN_STARTING_KIT_DEDUP_FIRED,
    chargen_armor_equipped_span,
    chargen_armor_unresolved_span,
)

logger = logging.getLogger(__name__)
_tracer = otel_trace.get_tracer(__name__)


def _match_class(mapping_keys: list[str], class_name: str) -> str | None:
    """Case-insensitive lookup for ``starting_equipment[class]`` / ``starting_gold[class]``.

    Rust uses ``.iter().find(|(k, _)| k.to_lowercase() == class_lower)``;
    this preserves the first-match-wins semantics.
    """
    class_lower = class_name.casefold()
    for key in mapping_keys:
        if key.casefold() == class_lower:
            return key
    return None


def _item_dict_from_catalog(catalog_item: CatalogItem) -> dict:
    """Build the loadout item dict from a catalog entry.

    Mirrors the Rust ``Item`` JSON shape (connect.rs:1795-1812).
    """
    rarity = catalog_item.rarity.strip() or "common"
    return {
        "id": catalog_item.id,
        "name": catalog_item.name,
        "description": catalog_item.description,
        "category": catalog_item.category,
        "value": int(catalog_item.value),
        "weight": float(catalog_item.weight),
        "rarity": rarity,
        "narrative_weight": 0.3,
        "tags": list(catalog_item.tags),
        "equipped": False,
        "quantity": 1,
        "uses_remaining": catalog_item.resource_ticks,
        "state": "Carried",
    }


def _upgrade_hint_items_from_catalog(
    items: list[dict], catalog_by_id: dict[str, CatalogItem]
) -> int:
    """Rewrite builder-produced ``item_hint`` dicts from the catalog.

    The chargen ``CharacterBuilder`` (builder.py) produces minimal item
    dicts from scene ``item_hint`` ids — without access to the pack
    inventory catalog it hardcodes ``category: weapon`` and a boilerplate
    description. Once the catalog is available in ``apply_starting_loadout``
    we can upgrade each matching hint to the canonical entry, preserving
    the builder's ``equipped`` / ``quantity`` flags.

    Returns the number of items upgraded. Items whose id isn't in the
    catalog are left untouched (minimal fallback stands).
    """
    upgraded = 0
    for i, item in enumerate(items):
        catalog_item = catalog_by_id.get(item.get("id", ""))
        if catalog_item is None:
            continue
        preserved_equipped = item.get("equipped", False)
        preserved_quantity = item.get("quantity", 1)
        replacement = _item_dict_from_catalog(catalog_item)
        replacement["equipped"] = preserved_equipped
        replacement["quantity"] = preserved_quantity
        items[i] = replacement
        upgraded += 1
    return upgraded


def _item_dict_minimal(item_id: str) -> dict:
    """Build a minimal item dict for ids that aren't in the catalog.

    Mirrors the Rust fallback branch (connect.rs:1814-1849). Used when a
    pack references an item id in ``starting_equipment`` that isn't
    declared in ``item_catalog`` — we still honor the loadout rather than
    silently dropping the item.
    """
    display = item_id.replace("_", " ")
    return {
        "id": item_id,
        "name": display,
        "description": "Starting equipment",
        "category": "equipment",
        "value": 0,
        "weight": 1.0,
        "rarity": "common",
        "narrative_weight": 0.2,
        "tags": [],
        "equipped": False,
        "quantity": 1,
        "uses_remaining": None,
        "state": "Carried",
    }


def apply_starting_loadout(
    character: Character,
    inventory_config: InventoryConfig | None,
    *,
    genre: str = "",
    world: str = "",
    player_id: str = "",
) -> tuple[int, int]:
    """Append class-specific starting equipment and gold to the character's inventory.

    Port of connect.rs:1745-1864. Mutates ``character.core.inventory`` in
    place — items append to ``inventory.items`` (builder-side hints are
    preserved), gold increments by ``starting_gold[class]``.

    Story 45-12 added an identity-aware dedup pass: any catalogue id that
    is already represented on ``character.core.inventory.items`` (matched
    by id OR by case-insensitive name) is skipped rather than appended a
    second time. Both extractors — ``CharacterBuilder.equipment_tables``
    and ``starting_equipment[class]`` — are legitimate; this seam owns
    deduplicating their union so Blutka-style 24-item kits stop shipping.

    Args:
        character: The built character (class already set).
        inventory_config: Genre pack inventory config; ``None`` means the
            pack has no inventory.yaml and we no-op (still emits the
            evaluated span for negative confirmation).
        genre: Genre slug for OTEL attribution. Pass ``sd.snapshot.genre_slug``
            from the chargen-confirm wire site.
        world: World slug for OTEL attribution.
        player_id: Player identity for OTEL attribution.

    Returns:
        ``(items_added, gold_added)`` — for logging and assertion in
        tests. ``items_added`` is the count after dedup (skipped items
        are NOT in this count). Both zero when the pack has no
        inventory config or when the character's class isn't in
        ``starting_equipment``.
    """
    class_name = character.char_class
    pre_dedup_count = len(character.core.inventory.items)

    # Defaults flow through the no-config path so the evaluated span fires
    # with zero counts (CLAUDE.md OTEL Observability Principle —
    # negative-confirmation contract: "no spans" must never mean "the
    # dedup pass was forgotten").
    equipment_ids: list[str] = []
    gold: int = 0
    items_upgraded = 0
    items_added = 0
    skipped_ids: list[str] = []

    if inventory_config is not None:
        equipment_key = _match_class(list(inventory_config.starting_equipment.keys()), class_name)
        gold_key = _match_class(list(inventory_config.starting_gold.keys()), class_name)
        equipment_ids = inventory_config.starting_equipment[equipment_key] if equipment_key else []
        gold = inventory_config.starting_gold[gold_key] if gold_key else 0

        # No Silent Fallbacks (playtest 2026-06-07, five_points): an
        # inventory.yaml that declares no loadout for this class used to
        # complete chargen silently with an empty inventory — the PLAYER
        # discovered the content gap. Make it loud at chargen time so the
        # GM panel surfaces the defect the turn it happens.
        if equipment_key is None and gold_key is None:
            declared = sorted(inventory_config.starting_equipment.keys())
            logger.warning(
                "chargen.starting_equipment_missing class=%s genre=%s world=%s "
                "declared_classes=%s — inventory.yaml has no starting_equipment/"
                "starting_gold entry for this class; character ships with an "
                "empty loadout (content gap)",
                class_name,
                genre,
                world,
                declared,
            )
            with _tracer.start_as_current_span(SPAN_CHARGEN_STARTING_EQUIPMENT_MISSING) as gap:
                gap.set_attribute("class_name", class_name)
                gap.set_attribute("declared_classes", ",".join(declared))
                gap.set_attribute("genre", genre)
                gap.set_attribute("world", world)
                gap.set_attribute("player_id", player_id)
        catalog_by_id = {item.id: item for item in inventory_config.item_catalog}

        # Upgrade builder-produced item_hint dicts (from chargen scene
        # choices like "Mystery Compass") against the catalog. Without
        # this, those items ship to the UI with the builder's stub
        # metadata — ``category: weapon`` + ``description: "Starting
        # equipment: X"``.
        items_upgraded = _upgrade_hint_items_from_catalog(
            character.core.inventory.items, catalog_by_id
        )

        # Dedup pass (Story 45-12). The ``existing_*`` sets are mutated
        # inside the loop so intra-batch duplicates within
        # ``equipment_ids`` itself collapse alongside builder-vs-catalogue
        # overlap.
        existing_ids: set[str] = {
            str(it.get("id", "")).strip().lower()
            for it in character.core.inventory.items
            if it.get("id")
        }
        existing_names: set[str] = {
            str(it.get("name", "")).strip().lower()
            for it in character.core.inventory.items
            if it.get("name")
        }

        for item_id in equipment_ids:
            catalog_item = catalog_by_id.get(item_id)
            candidate = (
                _item_dict_from_catalog(catalog_item)
                if catalog_item is not None
                else _item_dict_minimal(item_id)
            )
            cand_id = str(candidate.get("id", "")).strip().lower()
            cand_name = str(candidate.get("name", "")).strip().lower()
            if (cand_id and cand_id in existing_ids) or (cand_name and cand_name in existing_names):
                skipped_ids.append(cand_id or cand_name or item_id)
                continue
            character.core.inventory.items.append(candidate)
            if cand_id:
                existing_ids.add(cand_id)
            if cand_name:
                existing_names.add(cand_name)
            items_added += 1

        if gold:
            character.core.inventory.gold += gold

    # Story 53-2 (Epic 53 Road Warrior): bind a RigComposurePool to the
    # character iff the just-populated inventory contains a vessel-tagged
    # item. Runs after the loadout loop so genre-pack vessels (e.g.
    # ``rig_tier_1_prospect`` in road_warrior) reach
    # ``character.core.rig_pool``. Idempotent — if a reloaded save
    # already supplied a pool, this no-ops. Propagates
    # ``InvalidVesselTagsError`` loudly when content tags are malformed
    # (CLAUDE.md "No Silent Fallbacks").
    bind_rig_pool_from_inventory(character.core, character_id=character.core.name)

    final_count = len(character.core.inventory.items)
    skipped_count = len(skipped_ids)

    # Single emission point for the evaluated span — fires on every code
    # path (None config, no class match, full overlap, partial overlap,
    # disjoint).
    with _tracer.start_as_current_span(SPAN_CHARGEN_STARTING_KIT_DEDUP_EVALUATED) as span:
        span.set_attribute("class_name", class_name)
        span.set_attribute("pre_dedup_count", pre_dedup_count)
        span.set_attribute("equipment_ids_count", len(equipment_ids))
        span.set_attribute("skipped_count", skipped_count)
        span.set_attribute("items_added", items_added)
        span.set_attribute("items_upgraded", items_upgraded)
        span.set_attribute("final_count", final_count)
        span.set_attribute("genre", genre)
        span.set_attribute("world", world)
        span.set_attribute("player_id", player_id)

    # Fired span only when the dedup actually skipped at least one item.
    # The skipped_ids list is the load-bearing payload for the GM panel.
    if skipped_count > 0:
        with _tracer.start_as_current_span(SPAN_CHARGEN_STARTING_KIT_DEDUP_FIRED) as span:
            span.set_attribute("class_name", class_name)
            span.set_attribute("skipped_count", skipped_count)
            # OTEL attribute spec accepts homogeneous sequences of
            # primitives — pass the list as-is.
            span.set_attribute("skipped_ids", skipped_ids)
            span.set_attribute("items_added", items_added)
            span.set_attribute("final_count", final_count)
            span.set_attribute("genre", genre)
            span.set_attribute("world", world)
            span.set_attribute("player_id", player_id)

    if items_added or gold or items_upgraded or skipped_count:
        logger.info(
            "chargen.starting_equipment — wired from inventory.yaml "
            "class=%s items_added=%d items_upgraded=%d skipped=%d "
            "gold_added=%d",
            class_name,
            items_added,
            items_upgraded,
            skipped_count,
            gold,
        )

    return (items_added, gold)


def equip_starting_armor(
    character: Character,
    inventory_config: InventoryConfig | None,
    *,
    genre: str = "",
    world: str = "",
    player_id: str = "",
) -> int:
    """Equip the kit-rolled armor and derive ``core.armor_class`` from content.

    Story 106-1 (WWN combat hardening). The kit-roll loop (builder.py) appends
    every item ``equipped: False`` and nothing recomputes ``armor_class``, so
    every Warrior fought at the unarmored base AC 10 even with Leather Armor in
    the pack. This post-loadout step flips the equipped flag on armor-category
    items and recomputes ``character.core.armor_class`` from the equipped armor's
    catalog ``armor_class`` — the value is **sourced from content (WWN SRD)**,
    never an invented engine constant.

    WWN uses best-armor AC (a torso piece sets AC; armor is not additive), so the
    derived AC is the maximum catalog ``armor_class`` among the equipped armor
    pieces. The multi-piece combination/shield-bonus rule is deferred (story
    scope); today ``warrior_kit`` rolls a single armor piece.

    No Silent Fallback: an armor item whose catalog entry declares no
    ``armor_class`` (or has no catalog entry) is NOT equipped/derived — it emits
    a loud ``chargen.armor_unresolved`` span so the content gap surfaces at
    chargen instead of the PC silently fighting at the unraised AC. A character
    with no armor item at all is legitimately unarmored: no spans, AC unchanged
    (the unarmored default, normally 10 — but a world override may pre-seed it).

    Returns the resulting ``core.armor_class`` (for logging/assertion).
    """
    core = character.core
    ac_before = int(core.armor_class)

    armor_items = [
        item
        for item in core.inventory.items
        if str(item.get("category", "")).strip().lower() == "armor"
    ]
    if not armor_items:
        # Legitimately unarmored — not a content gap. Stay silent, AC unchanged.
        return ac_before

    catalog_by_id: dict[str, CatalogItem] = (
        {item.id: item for item in inventory_config.item_catalog}
        if inventory_config is not None
        else {}
    )

    pc_name = core.name
    valued: list[tuple[dict, int]] = []
    for item in armor_items:
        item_id = str(item.get("id", ""))
        item_name = str(item.get("name", ""))
        catalog_item = catalog_by_id.get(item_id)
        catalog_ac = catalog_item.armor_class if catalog_item is not None else None
        if catalog_ac is None:
            reason = "no_catalog_entry" if catalog_item is None else "catalog_armor_class_missing"
            logger.warning(
                "chargen.armor_unresolved item=%s pc=%s genre=%s world=%s reason=%s "
                "— equipped armor has no catalog armor_class to derive from; the PC "
                "is NOT silently left at AC 10 (content gap surfaced at chargen)",
                item_id,
                pc_name,
                genre,
                world,
                reason,
            )
            with chargen_armor_unresolved_span(
                item_id=item_id,
                item_name=item_name,
                reason=reason,
                pc_name=pc_name,
                genre=genre,
                world=world,
                player_id=player_id,
            ):
                pass
            continue
        valued.append((item, int(catalog_ac)))

    if not valued:
        # Armor present but none had a derivable value — already surfaced loudly.
        return ac_before

    ac_after = max(ac for _, ac in valued)
    core.armor_class = ac_after

    for item, catalog_ac in valued:
        item["equipped"] = True
        with chargen_armor_equipped_span(
            item_id=str(item.get("id", "")),
            item_name=str(item.get("name", "")),
            armor_class=catalog_ac,
            ac_before=ac_before,
            ac_after=ac_after,
            equipped_after=True,
            pc_name=pc_name,
            genre=genre,
            world=world,
            player_id=player_id,
        ):
            pass

    logger.info(
        "chargen.armor_equipped pc=%s ac %d -> %d (equipped %d armor piece(s)) genre=%s world=%s",
        pc_name,
        ac_before,
        ac_after,
        len(valued),
        genre,
        world,
    )
    return ac_after
