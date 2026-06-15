"""Verify caverns_and_claudes class kits resolve to inventory items.

Story 120-1 (ADR-145): the genre baseline is 100% WWN-verbatim, so the genre
class kits reference only ``wwn_*`` catalog ids. The dungeon-flavor gear with no
WWN analog (lockpicks, ten_foot_pole, chalk, spellbook, component_pouch, the iron
helm, the heal potion) lives at the WORLD tier and is merged back into the kits
world-over-genre by ``resolve_equipment_tables`` (story 120-4). The genre-tier
tests below assert the WWN-pure baseline; the world-merge tests assert the
beneath_sunden override re-adds and resolves the bespoke items.
"""

from sidequest.genre.loader import GenreLoader
from sidequest.server.dispatch.equipment_tables_resolve import resolve_equipment_tables
from sidequest.server.dispatch.inventory_resolve import resolve_inventory

BENEATH_SUNDEN = "beneath_sunden"


def _kit_item_ids(pack, kit_id: str) -> set[str]:
    kit = pack.equipment_tables.class_tables[kit_id]
    return {item for slot, items in kit.items() for item in items}


def test_cc_has_three_class_kits():
    loader = GenreLoader()
    pack = loader.load("caverns_and_claudes")
    assert pack.equipment_tables is not None
    assert set(pack.equipment_tables.class_tables.keys()) == {
        "warrior_kit",
        "expert_kit",
        "mage_kit",
    }


def test_cc_every_class_kit_table_resolves():
    """Every ClassDef.kit_table must resolve to a class_tables block — a
    missing key silently produces an empty starting inventory (the
    No-Silent-Fallbacks failure mode behind the 2026-06-12 WWN-port re-key)."""
    loader = GenreLoader()
    pack = loader.load("caverns_and_claudes")
    table_keys = set(pack.equipment_tables.class_tables.keys())
    for cls in pack.classes:
        assert cls.kit_table in table_keys, (
            f"class '{cls.id}' kit_table '{cls.kit_table}' has no class_tables "
            f"block — chargen would emit an empty inventory."
        )


def test_cc_kit_items_exist_in_inventory():
    loader = GenreLoader()
    pack = loader.load("caverns_and_claudes")
    catalog_ids = {item.id for item in pack.inventory.item_catalog}
    for kit_id in ("warrior_kit", "expert_kit", "mage_kit"):
        for item_id in _kit_item_ids(pack, kit_id):
            assert item_id in catalog_ids, f"{kit_id} references missing item: {item_id}"


def test_cc_mage_kit_has_no_armor():
    loader = GenreLoader()
    pack = loader.load("caverns_and_claudes")
    mage_kit = pack.equipment_tables.class_tables["mage_kit"]
    assert mage_kit.get("armor", []) == []


def test_cc_expert_kit_has_lockpicks():
    """Lockpicks have no WWN SRD analog, so story 120-1 moved them to the world
    tier (worlds/beneath_sunden/inventory.yaml, mode=bespoke). The WWN-pure genre
    expert kit therefore no longer carries them; the beneath_sunden
    equipment_tables override merges them back world-over-genre (story 120-4).
    Assert the production-faithful merged kit, not the bare genre tier."""
    loader = GenreLoader()
    pack = loader.load("caverns_and_claudes")
    # WWN-pure genre baseline does NOT carry the bespoke dungeon item...
    genre_expert = pack.equipment_tables.class_tables["expert_kit"]
    assert "lockpicks" not in {i for items in genre_expert.values() for i in items}, (
        "lockpicks has no WWN analog and must NOT appear in the verbatim genre baseline"
    )
    # ...but the world override re-adds it (the merged kit is what chargen uses).
    merged = resolve_equipment_tables(pack, BENEATH_SUNDEN)
    merged_expert = merged.class_tables["expert_kit"]
    assert "lockpicks" in {i for items in merged_expert.values() for i in items}, (
        "the beneath_sunden world override must merge lockpicks back into the expert kit"
    )


def test_cc_beneath_sunden_merged_kits_resolve_against_world_catalog():
    """Wiring guard for the story 120-1 verbatim-sweep rename cascade: every id in
    the beneath_sunden world-merged chargen kits (and their guaranteed grants)
    must resolve to an item in the world-merged inventory catalog. The genre kits
    are WWN-pure ``wwn_*`` ids; the world override re-adds the bespoke dungeon
    items — both tiers must resolve together, exactly as production chargen sees
    them (connect.py: resolve_equipment_tables + resolve_inventory). This is the
    test that fails loudly if a rename leaves a kit id dangling."""
    loader = GenreLoader()
    pack = loader.load("caverns_and_claudes")
    tables = resolve_equipment_tables(pack, BENEATH_SUNDEN)
    inv = resolve_inventory(pack, BENEATH_SUNDEN)
    catalog_ids = {item.id for item in inv.item_catalog}

    missing: dict[str, list[str]] = {}
    for kit_id, kit in tables.class_tables.items():
        for item_id in {i for items in kit.values() for i in items}:
            if item_id not in catalog_ids:
                missing.setdefault(kit_id, []).append(item_id)
    for kit_id, grants in tables.guaranteed_grants.items():
        for grant in grants:
            for gid in (grant.item, grant.upgrade):
                if gid is not None and gid not in catalog_ids:
                    missing.setdefault(kit_id, []).append(gid)

    assert not missing, (
        "beneath_sunden world-merged chargen kits reference ids absent from the "
        f"world-merged inventory catalog (dangling after the verbatim sweep): {missing}"
    )


def test_cc_each_class_has_positive_starting_gold():
    """Every WWN Calling must ship with non-zero starting gold so chargen-end
    cash gates (Recruiter's Post bonds, dungeon entry tolls) are reachable
    by every class. Playtest 2026-05-06: Carl-the-Cleric arrived with
    `gold_added=0` and could not engage Brenna's two-silver-bond gate at all,
    locking the recruitment confrontation into a hard fail. Re-keyed for the
    WWN Callings (2026-06-12 port): Warrior/Expert/Mage.
    """
    loader = GenreLoader()
    pack = loader.load("caverns_and_claudes")
    starting_gold = pack.inventory.starting_gold
    for class_name in ("Warrior", "Expert", "Mage"):
        assert class_name in starting_gold, (
            f"{class_name} missing from starting_gold — chargen will emit "
            f"gold_added=0 and the PC can't engage cash-gated content."
        )
        assert starting_gold[class_name] > 0, (
            f"{class_name} starting_gold is {starting_gold[class_name]} — "
            f"must be positive to clear chargen-end cash gates."
        )
