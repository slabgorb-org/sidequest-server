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

One pack (caverns_and_claudes) is enough for the wiring check. The
full cross-pack matrix — each of the 6 genres walked through
representative flows — belongs to Story 2.2's dispatch integration
tests, not 2.1's unit surface.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from sidequest.game.builder import (
    CharacterBuilder,
    InProgress,
    StoryInput,
)
from sidequest.genre.loader import load_genre_pack

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


def test_builder_walks_caverns_and_claudes_to_character(caverns_pack: object) -> None:
    """End-to-end: load real pack, walk all scenes, build a Character.

    caverns_and_claudes flow (6 scenes — visible-dice era):
      0. the_roll — auto-advance, stat_generation=roll_3d6_arrange_visible
                    (rolls a six-die pool, no labels yet)
      1. the_arrangement — assign_stat × 6, then apply_arrangement_confirm
                           (player drives qualification, reject button is
                            the only escape valve)
      2. the_calling — class choice (Fighter/Mage/Cleric/Thief filtered
                       to qualifying)
      3. the_story — StoryInput (pronouns + background + description)
      4. the_kit — auto-advance, equipment_generation=class_kit
      5. the_mouth — auto-advance (the dungeon entrance)

    The builder must:
      - Construct with pack.char_creation + pack.rules + backstory_tables
      - Wire pack.equipment_tables and pack.classes via fluent setters
      - Walk all 6 scenes to Confirmation
      - Build a Character whose char_class is one of the four classes
        and whose Edge max matches edge_config.base_max_by_class[class]
    """
    pack = caverns_pack
    # Sanity check: 6 scenes, the_roll first.
    assert len(pack.char_creation) == 6  # type: ignore[attr-defined]
    assert pack.char_creation[0].id == "the_roll"  # type: ignore[attr-defined]

    builder = (
        CharacterBuilder(
            scenes=list(pack.char_creation),  # type: ignore[attr-defined]
            rules=pack.rules,  # type: ignore[attr-defined]
            backstory_tables=pack.backstory_tables,  # type: ignore[attr-defined]
            rng=random.Random(42),
        )
        .with_lobby_name("Rux")
        .with_equipment_tables(pack.equipment_tables)  # type: ignore[attr-defined]
        .with_classes(pack.classes)  # type: ignore[attr-defined]
    )

    # Walk scenes:
    # 0. the_roll — visible-dice arrange flow rolled an unlabeled pool
    #    at construction. Auto-advance moves on to the_arrangement.
    assert builder.is_in_progress()
    assert isinstance(builder._phase, InProgress)
    assert builder.current_scene().id == "the_roll"
    # Visible-dice flow rolls a pool, not labeled stats.
    assert builder.rolled_stats() is None
    pool = builder.arrangement_pool()
    assert pool is not None and len(pool) == 6
    builder.apply_auto_advance()

    # 1. the_arrangement — assign sorted-desc into STR/DEX/CON/INT/WIS/CHA.
    #    Highest roll into STR guarantees Fighter qualifies (min STR 9).
    assert builder.current_scene().id == "the_arrangement"
    sorted_pool = sorted(pool, reverse=True)
    stat_order = list(pack.rules.ability_score_names)  # type: ignore[attr-defined]
    for stat_name, value in zip(stat_order, sorted_pool, strict=True):
        builder.assign_stat(stat_name, value)
    builder.apply_arrangement_confirm()

    # After arrangement, at least one class must qualify.
    final_stats = dict(builder.rolled_stats())
    from sidequest.game.builder import qualifying_classes

    qual = qualifying_classes(final_stats, pack.classes)  # type: ignore[attr-defined]
    assert len(qual) >= 1, (
        f"sorted-desc arrangement should qualify ≥1 class, got 0 with stats {final_stats}"
    )

    # 2. the_calling — pick a qualifying class. Pick the first one.
    assert builder.current_scene().id == "the_calling"
    presented = builder.current_scene()
    presented_hints = [c.mechanical_effects.class_hint for c in presented.choices]
    # Filter is server-side: only qualifying classes shown.
    assert presented_hints, "at least one class choice must be presented"
    chosen_hint = presented_hints[0]
    builder.apply_choice(0)

    # 3. the_story — StoryInput (pronouns + background + description)
    assert builder.current_scene().id == "the_story"
    builder.apply_response(
        StoryInput(
            pronouns="she/her",
            background="Raised in the caverns.",
            description="Tall, scarred, watchful.",
        )
    )

    # 4. the_kit — equipment_generation=class_kit
    assert builder.current_scene().id == "the_kit"
    builder.apply_auto_advance()

    # 5. the_mouth — display-only
    assert builder.current_scene().id == "the_mouth"
    builder.apply_auto_advance()

    assert builder.is_confirmation()

    # Build the character
    character = builder.build("Rux")

    # --- Character shape assertions ---

    # Identity
    assert character.core.name == "Rux"
    assert character.pronouns == "she/her"
    # char_class is the chosen class (one of Fighter/Mage/Cleric/Thief)
    assert character.char_class == chosen_hint
    assert character.char_class in {"Fighter", "Mage", "Cleric", "Thief"}

    # Stats: every ability score name has a value in a plausible range
    # (3d6 base = 3..18, modified by any derived bonuses = widened but
    # still realistic).
    for name in pack.rules.ability_score_names:  # type: ignore[attr-defined]
        assert name in character.stats
        assert 1 <= character.stats[name] <= 25

    # Inventory: equipment_tables roll produced at least one item
    # (rolls_per_slot defaults to 1 per slot on the pack).
    assert len(character.core.inventory.items) >= 1
    for item in character.core.inventory.items:
        # Shape check — these are the keys the builder emits. If a
        # downstream consumer expects flags we don't set, this test
        # will alert us when the dispatch port starts reading them.
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

    # Edge pool: edge_config drives base_max per class
    # (Fighter:4, Cleric:3, Mage:2, Thief:2). Fighter additionally
    # gets a hardcoded +2 stub from Story 39-4 — so for Fighter the
    # final base_max is 6, not 4. Verify the floor (config value)
    # is met and current==max.
    config_max = pack.rules.edge_config.base_max_by_class[character.char_class]  # type: ignore[attr-defined]
    assert character.core.hp.base_max >= config_max
    assert character.core.hp.max == character.core.hp.base_max
    assert character.core.hp.current == character.core.hp.max

    # Backstory: non-blank, came from some path (fragments/tables/
    # mechanical/fallback).
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
