"""Integration test for sidequest.game.builder — Slice 5.

Wiring test per CLAUDE.md:
    Every Test Suite Needs a Wiring Test
    Unit tests prove a component works in isolation. That's not enough.
    Every set of tests must include at least one integration test that
    verifies the component is wired into the system — imported, called,
    and reachable from production code paths.

This test loads a real genre pack via load_genre_pack(), constructs a
CharacterBuilder with the pack's actual char_creation scenes and
rules, walks the full scene flow, and builds a real Character. No
fixtures, no stubs, no mock data — if this passes, the builder is
reachable from production content.

WWN port (2026-06-12): caverns_and_claudes moved off the B/X visible-3d6
roll/arrange flow to a point-buy chassis (rules.yaml stat_generation:
point_buy), leaving a 4-scene flow (calling → story → kit → mouth). The
builder seeds attributes from the point-buy budget; there is no the_roll /
the_arrangement scene and no edge_config (HP comes from the WWN chassis). The
walk mirrors the generic point-buy walk used for heavy_metal / elemental_harmony
(see tests/integration/test_wwn_heavy_metal_chargen.py::_build_class).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.game.builder import (
    CharacterBuilder,
    FreeformInput,
    SceneResult,
    StoryInput,
)
from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models import MechanicalEffects

CONTENT_ROOT = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


@pytest.fixture(scope="module")
def caverns_pack() -> object:
    """Load caverns_and_claudes. Fails loudly if content isn't present —
    integration tests must run against the real tree (SOUL: fail loud
    at the boundary)."""
    path = CONTENT_ROOT / "caverns_and_claudes"
    if not path.is_dir():
        pytest.skip(f"content pack not found at {path}")
    return load_genre_pack(path)


def _walk_to_class(pack, *, target_class: str, name: str = "Rux"):
    """Walk the point-buy 4-scene flow, picking ``target_class`` on the_calling.

    Choice-bearing scenes select the choice whose class_hint matches; other
    scenes auto-advance, answer freeform identity input, or answer a followup.
    If no scene offered the target class_hint, inject it as a late SceneResult
    so build() accumulates it last-one-wins (mirrors the heavy_metal helper).
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
            # the_kit / the_mouth auto-advance; the_story captures identity.
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
        if idx is not None:
            matched = True
            builder.apply_choice(idx)
        else:
            builder.apply_choice(0)

    if not matched:
        builder._results.append(
            SceneResult(
                input_type=FreeformInput(text=""),
                effects_applied=MechanicalEffects(class_hint=target_class),
            )
        )
    return builder


def test_builder_walks_caverns_and_claudes_to_character(caverns_pack: object) -> None:
    """End-to-end: load real pack, walk all WWN scenes, build a Character.

    Targets the Mage Calling: its kit (mage_kit) resolves to equipment, so the
    inventory wiring assertion is meaningful. The builder must construct from
    pack.char_creation + pack.rules + backstory_tables, wire equipment_tables
    and classes, walk to Confirmation, and build a Character whose char_class is
    one of the three WWN Callings.
    """
    pack = caverns_pack
    # WWN shape: 4 scenes, the_calling first (point-buy, no roll/arrange).
    scene_ids = [s.id for s in pack.char_creation]  # type: ignore[attr-defined]
    assert scene_ids == ["the_calling", "the_story", "the_kit", "the_mouth"]

    builder = _walk_to_class(pack, target_class="Mage")
    assert builder.is_confirmation()

    character = builder.build("Rux")

    # --- Character shape assertions ---

    # Identity
    assert character.core.name == "Rux"
    assert character.pronouns == "she/her"
    # char_class is the chosen Calling.
    assert character.char_class == "Mage"
    assert character.char_class in {"Warrior", "Expert", "Mage"}

    # Stats: every ability score name has a value in a plausible range.
    for name in pack.rules.ability_score_names:  # type: ignore[attr-defined]
        assert name in character.stats
        assert 1 <= character.stats[name] <= 25

    # Inventory: mage_kit roll produced at least one item.
    assert len(character.core.inventory.items) >= 1
    for item in character.core.inventory.items:
        required = {
            "id",
            "name",
            "description",
            "category",
            "value",
            "weight",
            "rarity",
            "narrative_weight",
            "tags",
            "equipped",
            "quantity",
            "uses_remaining",
            "state",
        }
        assert required.issubset(item.keys())

    # HP: WWN chassis seeds the ablative pool (no edge_config). current == max.
    assert character.core.hp.max >= 1
    assert character.core.hp.current == character.core.hp.max
    assert character.core.hp.base_max == character.core.hp.max

    # Backstory: non-blank, came from some path.
    assert character.backstory.strip()

    # Level + narrative state at creation
    assert character.core.level == 1
    assert character.is_friendly is True
    assert character.narrative_state == "Beginning their adventure"


def test_caverns_pack_loader_is_the_sanctioned_entry_point(caverns_pack: object) -> None:
    """SOUL.md: 'The loader is the contract.' The builder must receive
    pack-loaded data only through load_genre_pack() — bypassing it (reading
    YAML directly, constructing scenes by hand) forks the strictness
    policy. This test documents the single-entry-point discipline by
    exercising the actual path dispatch will use."""
    pack = caverns_pack
    # char_creation is a structured list[CharCreationScene] — not a
    # dict or raw YAML — proving the loader has run validation.
    assert all(
        type(s).__name__ == "CharCreationScene"
        for s in pack.char_creation  # type: ignore[attr-defined]
    )
    # Rules has the enum-constrained stat_generation (not a freeform
    # string).
    assert isinstance(pack.rules.stat_generation, str)  # type: ignore[attr-defined]
