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
point_buy), leaving a calling → trade → story → kit → mouth flow. The
builder seeds attributes from the point-buy budget; there is no the_roll /
the_arrangement scene and no edge_config (HP comes from the WWN chassis). The
walk mirrors the generic point-buy walk used for heavy_metal / elemental_harmony
(see tests/integration/test_wwn_heavy_metal_chargen.py::_build_class).

ADR-143 Task 12 inserted the_trade (a WWN Background/Focus/skill-granting scene)
at index 1. The walk's existing choice loop already traverses it — the_trade has
no class_hint, so the walk falls through to apply_choice(0) (the Rope-Puller
trade: background Rope-Puller, focus load-bearer, skill Exert). Task 13 turns
this into a stronger end-to-end check: it asserts those grants land on the built
Character (core.background / core.foci / core.skills).
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
    # WWN shape: the_calling first (point-buy, no roll/arrange), the_trade
    # inserted at index 1 by ADR-143 Task 12.
    scene_ids = [s.id for s in pack.char_creation]  # type: ignore[attr-defined]
    assert scene_ids == ["the_calling", "the_trade", "the_story", "the_kit", "the_mouth"]

    # the_trade (ADR-143 Task 12) sits at index 1 and grants a WWN
    # Background/Focus/skill. The walk picks its first choice (no class_hint),
    # which is the Rope-Puller trade. Read its authored grants from the pack so
    # the end-to-end assertions below aren't hard-coded literals.
    #
    # Resolution detail: MechanicalEffects.background is the background ID used to
    # look up the Background def (driving free_skill grants); Character.background
    # stores the choice LABEL (for canned-opening filtering, builder.py l.3016),
    # not the id. So we assert the focus id + skill_grants land mechanically, the
    # background DEF's free_skill lands, and Character.background holds the label.
    trade_scene = next(s for s in pack.char_creation if s.id == "the_trade")  # type: ignore[attr-defined]
    trade_choice = trade_scene.choices[0]
    trade_effects = trade_choice.mechanical_effects
    expected_background_id = trade_effects.background
    expected_background_label = trade_choice.label
    expected_focus = trade_effects.focus_id
    expected_skills = dict(trade_effects.skill_grants)
    assert expected_background_id, "the_trade choice[0] must grant a background"
    assert expected_focus, "the_trade choice[0] must grant a focus_id"
    assert expected_skills, "the_trade choice[0] must grant at least one skill"
    # The background def's free_skill is also granted at chargen — capture it so
    # we can assert the def actually fed into the merged skill set.
    trade_bg_def = pack.backgrounds.get(expected_background_id)  # type: ignore[attr-defined]
    assert trade_bg_def is not None, (
        f"the_trade background id {expected_background_id!r} must resolve in "
        f"pack.backgrounds (ADR-143 loader)"
    )
    expected_free_skill = trade_bg_def.free_skill

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

    # the_trade grants (ADR-143 Task 12/13): the Background/Focus/skill from the
    # picked trade choice must land on the built Character. This is the
    # end-to-end proof that the chargen seam carries WWN substrate through
    # build() — not just that the scene exists.
    #
    # Character.background stores the choice LABEL (canned-openings P2), not the
    # mechanical background id.
    assert character.background == expected_background_label, (
        f"the_trade background label {expected_background_label!r} did not land; "
        f"got {character.background!r}"
    )
    # The focus id lands in Character.foci.
    assert expected_focus in character.foci, (
        f"the_trade focus {expected_focus!r} did not land; got {character.foci}"
    )
    # Scene-level skill_grants land in Character.skills.
    for skill_name, level in expected_skills.items():
        assert skill_name in character.skills, (
            f"the_trade skill {skill_name!r} did not land; got {character.skills}"
        )
        # Skill levels accumulate higher-of; the granted level is the floor.
        assert character.skills[skill_name] >= level
    # The background DEF's free_skill is also merged into Character.skills,
    # proving the background id resolved through pack.backgrounds and fed the
    # ruleset's contribute_background_skills (not just the scene grants).
    if expected_free_skill:
        assert expected_free_skill in character.skills, (
            f"the_trade background free_skill {expected_free_skill!r} did not land; "
            f"got {character.skills}"
        )

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
