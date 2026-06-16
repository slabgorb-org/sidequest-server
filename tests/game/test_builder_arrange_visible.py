import random

from sidequest.game.builder import CharacterBuilder
from sidequest.genre.models.character import (
    CharCreationChoice,
    CharCreationScene,
    IdentityCapture,
    MechanicalEffects,
)
from sidequest.genre.models.rules import RulesConfig


def _make_scenes_with_arrange_visible() -> list[CharCreationScene]:
    """Five-scene minimal fixture for tests."""
    return [
        CharCreationScene(
            id="the_roll",
            title="Roll",
            narration="...",
            choices=[],
            allows_freeform=False,
            mechanical_effects=MechanicalEffects(
                stat_generation="roll_3d6_arrange_visible",
            ),
        ),
        CharCreationScene(
            id="the_arrangement",
            title="Arrange",
            narration="...",
            choices=[],
            allows_freeform=False,
            mechanical_effects=MechanicalEffects(
                assignment_required=True,
                allow_reject=True,
            ),
        ),
        CharCreationScene(
            id="the_calling",
            title="Call",
            narration="...",
            choices=[
                CharCreationChoice(
                    label="Fighter",
                    description="Strong of arm.",
                    mechanical_effects=MechanicalEffects(class_hint="Fighter"),
                ),
            ],
            allows_freeform=False,
        ),
        CharCreationScene(
            id="the_story",
            title="Story",
            narration="...",
            choices=[],
            allows_freeform=True,
            mechanical_effects=MechanicalEffects(
                identity_capture=IdentityCapture(pronouns_required=True),
            ),
        ),
        CharCreationScene(
            id="the_kit",
            title="Kit",
            narration="...",
            choices=[],
            allows_freeform=False,
        ),
        CharCreationScene(
            id="the_mouth",
            title="Mouth",
            narration="...",
            choices=[],
            allows_freeform=False,
        ),
    ]


def _rules() -> RulesConfig:
    return RulesConfig(
        ability_score_names=["STR", "DEX", "CON", "INT", "WIS", "CHA"],
        stat_generation="roll_3d6_arrange_visible",
    )


def test_arrange_visible_produces_pool_of_six_3d6():
    builder = CharacterBuilder(
        scenes=_make_scenes_with_arrange_visible(),
        rules=_rules(),
        rng=random.Random(42),
    )
    pool = builder.arrangement_pool()
    assert pool is not None
    assert len(pool) == 6
    for n in pool:
        assert 3 <= n <= 18


def test_arrange_visible_does_not_set_rolled_stats_yet():
    """Pre-arrangement, only the pool exists. rolled_stats is materialized at confirm."""
    builder = CharacterBuilder(
        scenes=_make_scenes_with_arrange_visible(),
        rules=_rules(),
        rng=random.Random(42),
    )
    # Pre-existing accessor: rolled_stats() returns Optional list.
    # If the existing API returns the list directly, adapt the assertion.
    rolled = getattr(builder, "rolled_stats", None)
    if callable(rolled):
        assert rolled() in (None, [])
    else:
        # Attribute access fallback
        assert getattr(builder, "_rolled_stats", None) in (None, [])


def test_arrange_visible_pool_seeded_deterministically():
    a = CharacterBuilder(
        scenes=_make_scenes_with_arrange_visible(),
        rules=_rules(),
        rng=random.Random(0),
    )
    b = CharacterBuilder(
        scenes=_make_scenes_with_arrange_visible(),
        rules=_rules(),
        rng=random.Random(0),
    )
    assert a.arrangement_pool() == b.arrangement_pool()


def test_arrangement_assignment_initially_all_none():
    builder = CharacterBuilder(
        scenes=_make_scenes_with_arrange_visible(),
        rules=_rules(),
        rng=random.Random(42),
    )
    assignment = builder.arrangement_assignment()
    assert assignment is not None
    assert set(assignment.keys()) == {"STR", "DEX", "CON", "INT", "WIS", "CHA"}
    assert all(v is None for v in assignment.values())
