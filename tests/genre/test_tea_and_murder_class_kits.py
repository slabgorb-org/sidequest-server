"""Verify tea_and_murder callings resolve to inventory items.

Mirrors tests/genre/test_cc_class_kits.py but for the Brontë-gothic pack.
Each calling in classes.yaml must point to a kit_table in
equipment_tables.yaml whose item_ids all exist in inventory.yaml.
AC5 of story 49-4 requires the Doctor kit to deterministically include
gladstone_bag, stethoscope, clinical_thermometer, and bandages_linen.
"""

from __future__ import annotations

import random

from sidequest.game.builder import CharacterBuilder
from sidequest.genre.loader import GenreLoader
from sidequest.genre.models.character import (
    CharCreationChoice,
    CharCreationScene,
    MechanicalEffects,
)

_VICTORIA_KITS = (
    "doctor_kit",
    "clergyman_kit",
    "detective_kit",
    "society_kit",
    "governess_kit",
    "explorer_kit",
    "industrialist_kit",
)


def _kit_item_ids(pack, kit_id: str) -> set[str]:
    kit = pack.equipment_tables.class_tables[kit_id]
    return {item for _slot, items in kit.items() for item in items}


def test_tea_and_murder_loads():
    loader = GenreLoader()
    pack = loader.load("tea_and_murder")
    assert pack.classes, "tea_and_murder must declare classes.yaml"
    assert pack.equipment_tables is not None


def test_tea_and_murder_has_seven_class_kits():
    loader = GenreLoader()
    pack = loader.load("tea_and_murder")
    assert set(pack.equipment_tables.class_tables.keys()) == set(_VICTORIA_KITS)


def test_tea_and_murder_kit_items_exist_in_inventory():
    loader = GenreLoader()
    pack = loader.load("tea_and_murder")
    catalog_ids = {item.id for item in pack.inventory.item_catalog}
    for kit_id in _VICTORIA_KITS:
        for item_id in _kit_item_ids(pack, kit_id):
            assert item_id in catalog_ids, f"{kit_id} references missing item: {item_id}"


def test_tea_and_murder_classes_match_allowed_classes():
    """Every calling in classes.yaml must appear in rules.allowed_classes,
    or the chargen UI's class-qualification panel will be inconsistent
    with what the engine can actually instantiate."""
    loader = GenreLoader()
    pack = loader.load("tea_and_murder")
    allowed = set(pack.rules.allowed_classes)
    declared = {c.display_name for c in pack.classes}
    assert declared <= allowed, (
        f"classes.yaml has callings not in rules.allowed_classes: {declared - allowed}"
    )


def test_tea_and_murder_doctor_kit_guarantees_signature_items():
    """AC5: a Doctor chargen produces gladstone_bag, stethoscope,
    clinical_thermometer, bandages, and apothecary items every time.
    The kit guarantees them via singleton slots; this test pins the
    contract so a future refactor cannot quietly demote them."""
    loader = GenreLoader()
    pack = loader.load("tea_and_murder")
    doctor = pack.equipment_tables.class_tables["doctor_kit"]
    required = {
        "gladstone_bag",
        "stethoscope",
        "clinical_thermometer",
        "bandages_linen",
    }
    for item_id in required:
        slot_with_item = [slot for slot, items in doctor.items() if item_id in items]
        assert len(slot_with_item) == 1, (
            f"{item_id} should live in exactly one slot, found in {slot_with_item}"
        )
        slot = slot_with_item[0]
        assert doctor[slot] == [item_id], (
            f"{item_id} must be a singleton slot for guaranteed delivery, "
            f"but slot {slot!r} holds {doctor[slot]}"
        )
    # Apothecary spread — two rolls from three options means at least
    # one apothecary item is guaranteed, and two are likely.
    apothecary = doctor.get("apothecary", [])
    assert {"laudanum_bottle", "quinine_bottle", "iodine_tincture"}.issubset(set(apothecary))
    assert pack.equipment_tables.rolls_per_slot.get("apothecary", 1) >= 2


def test_tea_and_murder_doctor_chargen_produces_signature_items_end_to_end():
    """AC5 wiring test — drive CharacterBuilder against the real Tea & Murder
    pack with class_hint=Doctor and confirm the post-build inventory
    contains every AC5-required item. Mirrors the C&C class_kit
    integration test pattern but uses the loaded YAML, not fixtures.

    This is the integration check that pairs with the data-structure
    tests above: it proves the chargen pipeline actually picks up
    Tea & Murder's class_kit wiring end-to-end."""
    loader = GenreLoader()
    pack = loader.load("tea_and_murder")

    scenes = [
        CharCreationScene(
            id="calling",
            title="Calling",
            narration="What is your calling?",
            choices=[
                CharCreationChoice(
                    label="Doctor",
                    description="The country doctor.",
                    mechanical_effects=MechanicalEffects(class_hint="Doctor"),
                ),
            ],
        ),
        CharCreationScene(
            id="the_satchel",
            title="What You Brought",
            narration="The publican glances at your satchel.",
            mechanical_effects=MechanicalEffects(equipment_generation="class_kit"),
        ),
    ]

    builder = (
        CharacterBuilder(scenes, pack.rules, rng=random.Random(0))
        .with_equipment_tables(pack.equipment_tables)
        .with_classes(pack.classes)
    )
    builder.apply_choice(0)
    builder.apply_auto_advance()
    assert builder.is_confirmation()

    character = builder.build("Dr. Test")
    item_ids = [item["id"] for item in character.core.inventory.items]
    required = {
        "gladstone_bag",
        "stethoscope",
        "clinical_thermometer",
        "bandages_linen",
    }
    missing = required - set(item_ids)
    assert not missing, (
        f"AC5: Doctor chargen produced {item_ids}; missing required items "
        f"{missing}. Check doctor_kit singleton slots in equipment_tables.yaml."
    )
    # At least one apothecary item — guarantee via 2 rolls from 3 options.
    apothecary = {"laudanum_bottle", "quinine_bottle", "iodine_tincture"}
    assert apothecary & set(item_ids), (
        f"AC5: Doctor should arrive with apothecary supplies; got {item_ids}"
    )


def test_tea_and_murder_each_class_has_positive_starting_gold():
    """Every calling must ship with non-zero starting pounds so chargen
    arrives at the village with at least pocket-cash for low-level
    social gates (a coach fare, a card-game stake, a small bribe).
    Mirrors the C&C lesson from playtest 2026-05-06."""
    loader = GenreLoader()
    pack = loader.load("tea_and_murder")
    starting_gold = pack.inventory.starting_gold
    for c in pack.classes:
        assert c.display_name in starting_gold, (
            f"{c.display_name} missing from starting_gold — chargen will "
            f"emit gold_added=0 and the PC arrives penniless."
        )
        assert starting_gold[c.display_name] > 0
