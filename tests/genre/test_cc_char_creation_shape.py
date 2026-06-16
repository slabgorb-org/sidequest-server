"""Verify caverns_and_claudes char_creation.yaml has the expected
6-scene shape with visible-dice arrange flow and class_kit equipment."""

from sidequest.genre.loader import GenreLoader


def test_cc_chargen_has_six_scenes_in_order():
    loader = GenreLoader()
    pack = loader.load("caverns_and_claudes")
    scene_ids = [s.id for s in pack.char_creation]
    assert len(scene_ids) == 6
    assert scene_ids[0] == "the_roll"
    assert scene_ids[1] == "the_arrangement"
    assert scene_ids[2] == "the_calling"
    assert scene_ids[3] == "the_story"
    assert scene_ids[4] == "the_kit"
    assert scene_ids[5] == "the_mouth"


def test_cc_roll_scene_uses_arrange_visible():
    loader = GenreLoader()
    pack = loader.load("caverns_and_claudes")
    roll_scene = next(s for s in pack.char_creation if s.id == "the_roll")
    assert roll_scene.mechanical_effects is not None
    assert roll_scene.mechanical_effects.stat_generation == "roll_3d6_arrange_visible"
    # Defaults removed — class scene sets jungian/rpg_role per-choice.
    assert roll_scene.mechanical_effects.jungian_hint is None
    assert roll_scene.mechanical_effects.rpg_role_hint is None


def test_cc_class_scene_has_four_class_choices():
    loader = GenreLoader()
    pack = loader.load("caverns_and_claudes")
    class_scene = next(s for s in pack.char_creation if s.id == "the_calling")
    class_hints = sorted(c.mechanical_effects.class_hint for c in class_scene.choices)
    assert class_hints == ["Cleric", "Fighter", "Mage", "Thief"]


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
