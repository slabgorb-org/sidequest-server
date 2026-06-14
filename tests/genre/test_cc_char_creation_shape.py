"""Verify caverns_and_claudes char_creation.yaml has the expected shape.

WWN port (2026-06-12): the B/X visible-3d6 roll/arrange scenes were retired in
favor of the point-buy chassis (rules.yaml stat_generation: point_buy), leaving
a calling → trade → story → kit → mouth flow that mirrors elemental_harmony /
heavy_metal. The three Callings (Warrior/Expert/Mage) are offered on the_calling.

ADR-143 Task 12 inserted the_trade (a WWN Background/Focus/skill-granting scene)
at index 1, between the_calling and the_story — so the flow is now five scenes.

NOTE: this asserts the structure of a REAL pack's char_creation.yaml, so it is a
content-invariant test that arguably belongs in the pack validator per the
project's "no content in unit tests" rule. Updating it in place is the right
minimal fix for the Task-12 scene insertion; a future move to the validator is
the cleaner home.
"""

from sidequest.genre.loader import GenreLoader


def test_cc_chargen_scenes_in_order():
    loader = GenreLoader()
    pack = loader.load("caverns_and_claudes")
    scene_ids = [s.id for s in pack.char_creation]
    assert scene_ids == ["the_calling", "the_trade", "the_story", "the_kit", "the_mouth"]


def test_cc_class_scene_has_three_calling_choices():
    loader = GenreLoader()
    pack = loader.load("caverns_and_claudes")
    class_scene = next(s for s in pack.char_creation if s.id == "the_calling")
    class_hints = sorted(c.mechanical_effects.class_hint for c in class_scene.choices)
    assert class_hints == ["Expert", "Mage", "Warrior"]


def test_cc_class_scene_choices_carry_role_and_jungian():
    loader = GenreLoader()
    pack = loader.load("caverns_and_claudes")
    class_scene = next(s for s in pack.char_creation if s.id == "the_calling")
    for choice in class_scene.choices:
        assert choice.mechanical_effects.rpg_role_hint is not None, (
            f"choice {choice.label} missing rpg_role_hint"
        )
        assert choice.mechanical_effects.jungian_hint is not None, (
            f"choice {choice.label} missing jungian_hint"
        )


def test_cc_kit_scene_uses_class_kit_generation():
    loader = GenreLoader()
    pack = loader.load("caverns_and_claudes")
    kit_scene = next(s for s in pack.char_creation if s.id == "the_kit")
    assert kit_scene.mechanical_effects is not None
    assert kit_scene.mechanical_effects.equipment_generation == "class_kit"
