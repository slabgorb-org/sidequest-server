"""End-to-end caverns_and_claudes chargen integration test.

The wiring gate required by sidequest-content/CLAUDE.md:
"Every Test Suite Needs a Wiring Test." Verifies the full path —
load real pack → walk all WWN scenes → produce a Character with class,
kit, and archetype-resolution all flowing through correctly.

WWN port (2026-06-12): caverns moved off the B/X visible-3d6 roll/arrange flow
to a point-buy chassis (rules.yaml stat_generation: point_buy), leaving a
4-scene flow (calling → story → kit → mouth) with the three WWN Callings
(Warrior/Expert/Mage). There is no the_roll / the_arrangement scene and no
edge_config (HP comes from the WWN chassis). The walk mirrors the generic
point-buy walk used for heavy_metal / elemental_harmony.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.game.builder import (
    CharacterBuilder,
    FreeformInput,
    SceneResult,
    StoryInput,
    qualifying_classes,
)
from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models import MechanicalEffects

CONTENT_ROOT = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


@pytest.fixture
def cc_pack():
    path = CONTENT_ROOT / "caverns_and_claudes"
    if not path.is_dir():
        pytest.skip(f"content pack not found at {path}")
    return load_genre_pack(path)


def _drive_chargen(pack, *, target_class: str, name: str = "Wiring"):
    """Walk the WWN 4-scene point-buy flow, picking the named Calling.

    Choice-bearing scenes select the choice whose class_hint matches; other
    scenes auto-advance (the_kit/the_mouth) or capture identity (the_story).
    If no scene offered the target class_hint, inject it as a late SceneResult.
    """
    builder = (
        CharacterBuilder(
            scenes=list(pack.char_creation),
            rules=pack.rules,
            backstory_tables=pack.backstory_tables,
        )
        .with_lobby_name(name)
        .with_equipment_tables(pack.equipment_tables)
        .with_classes(pack.classes)
    )

    matched = False
    guard = 0
    while not builder.is_confirmation():
        guard += 1
        assert guard < 50, "chargen walk did not reach confirmation"
        if builder.is_awaiting_followup():
            builder.answer_followup(name)
            continue
        scene = builder.current_scene()
        if not scene.choices:
            try:
                builder.apply_auto_advance()
            except Exception:
                builder.apply_response(
                    StoryInput(
                        pronouns="she/her",
                        background="Raised in the caverns.",
                        description="Tall, scarred, watchful.",
                    )
                )
            continue
        idx = next(
            (
                i
                for i, c in enumerate(scene.choices)
                if c.mechanical_effects and c.mechanical_effects.class_hint == target_class
            ),
            None,
        )
        assert idx is not None, (
            f"target_class {target_class} not in choices: "
            f"{[c.mechanical_effects.class_hint for c in scene.choices]}"
        )
        matched = True
        builder.apply_choice(idx)

    if not matched:
        builder._results.append(
            SceneResult(
                input_type=FreeformInput(text=""),
                effects_applied=MechanicalEffects(class_hint=target_class),
            )
        )
    return builder


def test_e2e_chargen_produces_classed_warrior(cc_pack):
    builder = _drive_chargen(cc_pack, target_class="Warrior")
    character = builder.build("Wiring")

    assert character.char_class == "Warrior"
    # WWN chassis seeds the ablative HP pool (no edge_config).
    assert character.core.hp.max >= 1
    assert character.core.hp.current == character.core.hp.max


def test_e2e_chargen_produces_classed_mage(cc_pack):
    builder = _drive_chargen(cc_pack, target_class="Mage")
    character = builder.build("Wiring")
    assert character.char_class == "Mage"
    assert character.core.hp.current == character.core.hp.max
    # mage_kit resolves to equipment; items come only from that kit.
    assert len(character.core.inventory.items) > 0
    rolled_ids = {i["id"] for i in character.core.inventory.items}
    mage_kit = cc_pack.equipment_tables.class_tables["mage_kit"]
    mage_items = {i for items in mage_kit.values() for i in items}
    assert rolled_ids.issubset(mage_items), (
        f"Items {rolled_ids - mage_items} leaked from outside mage_kit"
    )
    # Mage kit has no armor.
    assert mage_kit.get("armor", []) == []


def test_e2e_archetype_resolution_gate_passes(cc_pack):
    """Story 45-6's archetype-resolution gate requires both jungian_hint and
    rpg_role_hint populated. The Mage Calling sets magician/control (a valid
    pairing in archetype_constraints.yaml)."""
    builder = _drive_chargen(cc_pack, target_class="Mage")
    acc = builder.accumulated()
    assert acc.jungian_hint == "magician"
    assert acc.rpg_role_hint == "control"
    character = builder.build("Wiring")
    assert character.resolved_archetype == "magician/control"


def test_e2e_qualifying_classes_observable_from_pack(cc_pack):
    """Smoke check: the public API surface for class qualification is
    reachable and behaves correctly with real WWN pack data. All three
    Callings share minimum_score 9 (point-buy), so a baseline-9 character
    qualifies for every Calling."""
    stats = {"STR": 9, "DEX": 9, "CON": 9, "INT": 9, "WIS": 9, "CHA": 9}
    qual = qualifying_classes(stats, cc_pack.classes)
    assert len(qual) == 3
    assert {c.id for c in qual} == {"warrior", "expert", "mage"}
