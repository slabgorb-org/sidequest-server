"""End-to-end caverns_and_claudes chargen integration test.

The wiring gate required by sidequest-content/CLAUDE.md:
"Every Test Suite Needs a Wiring Test." Verifies the full path —
load real pack → walk all 6 scenes (visible-dice era) → produce a
Character with class, edge, kit, and archetype-resolution all
flowing through correctly.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from sidequest.game.builder import CharacterBuilder, StoryInput, qualifying_classes
from sidequest.genre.loader import load_genre_pack

CONTENT_ROOT = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


@pytest.fixture
def cc_pack():
    path = CONTENT_ROOT / "caverns_and_claudes"
    if not path.is_dir():
        pytest.skip(f"content pack not found at {path}")
    return load_genre_pack(path)


def _force_arrange_all_18(builder, stat_order):
    """Stub the arrangement pool + assignment to all-18 so every class
    qualifies. apply_arrangement_confirm then materializes _rolled_stats.
    """
    builder._arrangement_pool = [18, 18, 18, 18, 18, 18]
    for stat in stat_order:
        builder.assign_stat(stat, 18)


def _drive_chargen(pack, *, target_class: str, rng_seed: int = 42):
    """Walk the 6-scene flow, picking the named class. Force pool to
    all-18 so all classes qualify (deterministic regardless of seed)."""
    builder = (
        CharacterBuilder(
            scenes=list(pack.char_creation),
            rules=pack.rules,
            backstory_tables=pack.backstory_tables,
            rng=random.Random(rng_seed),
        )
        .with_lobby_name("Wiring")
        .with_equipment_tables(pack.equipment_tables)
        .with_classes(pack.classes)
    )
    stat_order = list(pack.rules.ability_score_names)
    _force_arrange_all_18(builder, stat_order)

    # 0. the_roll — auto-advance (pool was rolled at construction).
    builder.apply_auto_advance()
    # 1. the_arrangement — confirm the all-18 assignment.
    builder.apply_arrangement_confirm()
    # 2. the_calling — pick by class_hint.
    scene = builder.current_scene()
    idx = next(
        (i for i, c in enumerate(scene.choices) if c.mechanical_effects.class_hint == target_class),
        None,
    )
    assert idx is not None, (
        f"target_class {target_class} not in qualifying choices: "
        f"{[c.mechanical_effects.class_hint for c in scene.choices]}"
    )
    builder.apply_choice(idx)
    # 3. the_story — pronouns + freeform background/description.
    builder.apply_response(
        StoryInput(
            pronouns="she/her",
            background="Raised in the caverns.",
            description="Tall, scarred, watchful.",
        )
    )
    # 4. the_kit — auto-advance, class_kit equipment generation.
    builder.apply_auto_advance()
    # 5. the_mouth — auto-advance, display only.
    builder.apply_auto_advance()
    return builder


def test_e2e_chargen_produces_classed_fighter(cc_pack):
    builder = _drive_chargen(cc_pack, target_class="Fighter")
    character = builder.build("Wiring")

    assert character.char_class == "Fighter"
    # edge_config[Fighter]=4, plus Story 39-4 hardcoded +2 stub → 6
    assert character.core.hp.base_max >= cc_pack.rules.edge_config.base_max_by_class["Fighter"]
    assert character.core.hp.current == character.core.hp.max
    assert len(character.core.inventory.items) > 0
    # Inventory pulled from fighter_kit only.
    fighter_kit = cc_pack.equipment_tables.class_tables["fighter_kit"]
    fighter_items = {i for items in fighter_kit.values() for i in items}
    rolled_ids = {i["id"] for i in character.core.inventory.items}
    assert rolled_ids.issubset(fighter_items), (
        f"Items {rolled_ids - fighter_items} leaked from outside fighter_kit"
    )


def test_e2e_chargen_produces_classed_mage(cc_pack):
    builder = _drive_chargen(cc_pack, target_class="Mage")
    character = builder.build("Wiring")
    assert character.char_class == "Mage"
    assert character.core.hp.base_max >= cc_pack.rules.edge_config.base_max_by_class["Mage"]
    # Mage kit has no armor — should not have any armor items.
    rolled_ids = {i["id"] for i in character.core.inventory.items}
    mage_kit = cc_pack.equipment_tables.class_tables["mage_kit"]
    mage_items = {i for items in mage_kit.values() for i in items}
    assert rolled_ids.issubset(mage_items)


def test_e2e_archetype_resolution_gate_passes(cc_pack):
    """Story 45-6's archetype-resolution gate requires both jungian_hint
    and rpg_role_hint populated. Class scene must set both."""
    builder = _drive_chargen(cc_pack, target_class="Cleric")
    acc = builder.accumulated()
    assert acc.jungian_hint == "caregiver"
    assert acc.rpg_role_hint == "healer"
    character = builder.build("Wiring")
    assert character.resolved_archetype == "caregiver/healer"


def test_e2e_qualifying_classes_observable_from_pack(cc_pack):
    """Smoke check: the public API surface for class qualification is
    reachable and behaves correctly with real pack data."""
    stats = {"STR": 9, "DEX": 9, "CON": 9, "INT": 9, "WIS": 9, "CHA": 9}
    qual = qualifying_classes(stats, cc_pack.classes)
    assert len(qual) == 4
    assert {c.id for c in qual} == {"fighter", "mage", "cleric", "thief"}


def test_e2e_low_str_filters_out_fighter(cc_pack):
    """STR=8 means Fighter shouldn't be presentable on the_calling scene."""
    builder = (
        CharacterBuilder(
            scenes=list(cc_pack.char_creation),
            rules=cc_pack.rules,
            backstory_tables=cc_pack.backstory_tables,
            rng=random.Random(42),
        )
        .with_equipment_tables(cc_pack.equipment_tables)
        .with_classes(cc_pack.classes)
    )
    # STR=8, all others=18 → Fighter shouldn't qualify; Mage/Cleric/Thief should.
    builder._arrangement_pool = [8, 18, 18, 18, 18, 18]
    stat_values = {"STR": 8, "DEX": 18, "CON": 18, "INT": 18, "WIS": 18, "CHA": 18}
    for stat, value in stat_values.items():
        builder.assign_stat(stat, value)
    # 0. the_roll — auto-advance.
    builder.apply_auto_advance()
    # 1. the_arrangement — confirm; advances to the_calling.
    builder.apply_arrangement_confirm()
    scene = builder.current_scene()
    presented_hints = [c.mechanical_effects.class_hint for c in scene.choices]
    assert "Fighter" not in presented_hints, (
        f"Fighter should be filtered out at STR=8, got: {presented_hints}"
    )
    assert sorted(presented_hints) == ["Cleric", "Mage", "Thief"]
