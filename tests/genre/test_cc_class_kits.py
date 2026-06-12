"""Verify caverns_and_claudes class kits resolve to inventory items."""

from sidequest.genre.loader import GenreLoader


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
    loader = GenreLoader()
    pack = loader.load("caverns_and_claudes")
    expert_kit = pack.equipment_tables.class_tables["expert_kit"]
    assert "lockpicks" in {i for items in expert_kit.values() for i in items}


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
