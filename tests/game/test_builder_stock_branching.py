"""Story 103-2 RED — per-stock branching of the chargen flow (builder walk).

Build plan §D-B: a stock-selection chargen step branches the existing
``mutation`` step. The branching primitive pinned here is GENERIC and
schema-driven (zero per-stock engine cases — the world AUTHORS per-stock
scenes, the engine only matches tags):

  - ``MechanicalEffects`` grows ``stock_id`` (and ``saint_id`` for the
    Saint-Marked branch — 103-1's preset path finally gets its selection
    surface; see chargen_mixin's 103-1 plumbing comment).
  - ``CharCreationScene`` grows ``requires_stock``: the scene is presented
    ONLY when a previously applied choice carried a matching
    ``mechanical_effects.stock_id``; non-matching scenes are skipped.
  - A flow with no stock scene (flickering_reach, every other world) never
    matches a ``requires_stock`` scene — and a flow with no
    ``requires_stock`` scenes at all walks exactly as today (AC1: absence
    = single-path, current behavior preserved).
  - The builder exposes ``chosen_stock_id`` / ``chosen_saint_id`` so the
    chargen confirm handler can plumb them to
    ``init_mutation_state_for_session`` without re-walking scenes.
"""

from __future__ import annotations

from sidequest.game.builder import CharacterBuilder, ChoiceInput
from sidequest.genre.models.character import (
    CharCreationChoice,
    CharCreationScene,
    MechanicalEffects,
)
from sidequest.genre.models.rules import RulesConfig

# ---------------------------------------------------------------------------
# Scene fixtures — a stock step branching into per-stock mutation scenes
# ---------------------------------------------------------------------------


def _choice(label: str, **effects) -> CharCreationChoice:
    return CharCreationChoice(
        label=label,
        description=f"{label} described",
        mechanical_effects=MechanicalEffects(**effects),
    )


def _stock_scenes() -> list[CharCreationScene]:
    return [
        CharCreationScene(
            id="origins",
            title="Where You Woke Up",
            narration="The Seaboard remembers.",
            choices=[_choice("On the tide flats", background="tide flats")],
        ),
        CharCreationScene(
            id="stock",
            title="What You Are",
            narration="Six roads into the world.",
            choices=[
                _choice("Sleeper", stock_id="sleeper"),
                _choice("Harbor Seal Uplift", stock_id="harbor_seal"),
            ],
        ),
        CharCreationScene(
            id="the_cold_rack",
            title="The Cold Rack",
            narration="Your implants hum awake.",
            requires_stock="sleeper",
            choices=[
                _choice("Cortex Booster", item_hint="cortex_booster"),
                _choice("Subdermal Weave", item_hint="subdermal_weave"),
            ],
        ),
        CharCreationScene(
            id="the_spring",
            title="The Saint's Spring",
            narration="Drink, and be marked.",
            requires_stock="harbor_seal",
            choices=[
                _choice("Drink from Herman's spring", saint_id="herman_of_the_acushnet"),
                _choice("Refuse the water"),
            ],
        ),
        CharCreationScene(
            id="confirmation",
            title="The Wasteland Awaits",
            narration="It is decided.",
            choices=[],
        ),
    ]


def _plain_scenes() -> list[CharCreationScene]:
    """A flow with NO stock machinery at all — the flickering_reach shape."""
    return [
        CharCreationScene(
            id="origins",
            title="Where You Woke Up",
            narration="The Reach flickers.",
            choices=[_choice("In the salt camp", background="salt camp")],
        ),
        CharCreationScene(
            id="confirmation",
            title="The Wasteland Awaits",
            narration="It is decided.",
            choices=[],
        ),
    ]


def _builder(scenes: list[CharCreationScene]) -> CharacterBuilder:
    return CharacterBuilder(scenes=scenes, rules=RulesConfig())


def _walk_ids(builder: CharacterBuilder, picks: dict[str, int]) -> list[str]:
    """Walk the builder applying ``picks[scene_id]`` (default choice 0) at
    each scene; return the scene ids actually PRESENTED, in order."""
    presented: list[str] = []
    while True:
        scene = builder.current_scene()
        presented.append(scene.id)
        if scene.id == "confirmation":
            return presented
        builder.apply_response(ChoiceInput(index=picks.get(scene.id, 0)))


# ---------------------------------------------------------------------------
# Model seam — the branching vocabulary exists
# ---------------------------------------------------------------------------


def test_mechanical_effects_carry_stock_and_saint_ids() -> None:
    effects = MechanicalEffects(stock_id="sleeper", saint_id="herman_of_the_acushnet")
    assert effects.stock_id == "sleeper"
    assert effects.saint_id == "herman_of_the_acushnet"


def test_scene_carries_requires_stock_default_none() -> None:
    scene = CharCreationScene(id="x", title="X", narration="n")
    assert scene.requires_stock is None


# ---------------------------------------------------------------------------
# Branch walk — the mutation step forks per chosen stock
# ---------------------------------------------------------------------------


def test_sleeper_pick_presents_sleeper_branch_only() -> None:
    builder = _builder(_stock_scenes())
    presented = _walk_ids(builder, {"stock": 0})
    assert "the_cold_rack" in presented, "the sleeper branch scene must be presented"
    assert "the_spring" not in presented, "the harbor_seal branch must be skipped"
    assert presented[-1] == "confirmation"


def test_animal_pick_presents_saint_spring_branch_only() -> None:
    builder = _builder(_stock_scenes())
    presented = _walk_ids(builder, {"stock": 1})
    assert "the_spring" in presented
    assert "the_cold_rack" not in presented


def test_untagged_scenes_present_for_every_stock() -> None:
    """origins + confirmation carry no requires_stock — they are the shared
    spine and must appear on both branches."""
    for pick in (0, 1):
        builder = _builder(_stock_scenes())
        presented = _walk_ids(builder, {"stock": pick})
        assert presented[0] == "origins"
        assert presented[-1] == "confirmation"


def test_flow_without_stock_machinery_walks_unchanged() -> None:
    """AC1, flickering_reach half: a scene list with no stock scene and no
    requires_stock tags walks exactly as authored — single-path preserved."""
    builder = _builder(_plain_scenes())
    presented = _walk_ids(builder, {})
    assert presented == ["origins", "confirmation"]


def test_branch_scenes_skipped_when_no_stock_chosen() -> None:
    """A requires_stock scene in a flow where the player never passed a
    stock choice must be skipped, not crash — the tag is a filter, never a
    requirement that some stock exists."""
    scenes = [s for s in _stock_scenes() if s.id != "stock"]
    builder = _builder(scenes)
    presented = _walk_ids(builder, {})
    assert presented == ["origins", "confirmation"]


# ---------------------------------------------------------------------------
# Confirm plumbing — chosen ids exposed for init_mutation_state_for_session
# ---------------------------------------------------------------------------


def test_builder_exposes_chosen_stock_id() -> None:
    builder = _builder(_stock_scenes())
    _walk_ids(builder, {"stock": 0})
    assert builder.chosen_stock_id == "sleeper"


def test_builder_exposes_chosen_saint_id_from_branch() -> None:
    """The Saint-Marked selection surface (103-1's deferred half): drinking
    from the spring on the branch scene records the saint for confirm."""
    builder = _builder(_stock_scenes())
    _walk_ids(builder, {"stock": 1, "the_spring": 0})
    assert builder.chosen_stock_id == "harbor_seal"
    assert builder.chosen_saint_id == "herman_of_the_acushnet"


def test_chosen_ids_none_when_flow_has_no_stock_step() -> None:
    builder = _builder(_plain_scenes())
    _walk_ids(builder, {})
    assert builder.chosen_stock_id is None
    assert builder.chosen_saint_id is None
