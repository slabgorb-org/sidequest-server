"""Story 103-3 RED — Roll the Bones alt-attribute mode (build plan §D-C).

The Gamma World 4e register: 3d6 per stat, assigned in order over the
standard six (STR/DEX/CON/INT/WIS/CHA), hopeless characters allowed. An
*option* alongside the pack default — never a replacement.

Pinned contract (mirrors the 103-2 stock-branching precedent):

- A chargen choice carrying ``mechanical_effects.stat_generation:
  "roll_the_bones"`` adopts the mode at ``apply_choice`` time and eagerly
  rolls six 3d6 totals in ``ability_score_names`` order into
  ``rolled_stats`` (visible — narration and frames read it).
- The default choice (no ``stat_generation``) leaves the pack default
  (point_buy here) completely untouched.
- A scene tagged ``requires_stat_generation: "roll_the_bones"`` is the
  interaction surface; the skip-walk presents it only when the mode is
  active — same FILTER doctrine as ``requires_stock``, same
  one-result-per-presented-scene ledger (the 103-2 [HIGH] regression
  class).
- No clamping, no floor, no qualification loop: a bad array stands.
- Every roll fires SPAN_CHARGEN_STAT_ROLL with the three faces — the GM
  panel lie-detector proves the dice were real, not improvised.
"""

from __future__ import annotations

import random

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.builder import CharacterBuilder, UnknownStatGenerationError
from sidequest.genre.models.character import (
    CharCreationChoice,
    CharCreationScene,
    MechanicalEffects,
)
from sidequest.genre.models.rules import RulesConfig

ORDER = ["STR", "DEX", "CON", "INT", "WIS", "CHA"]


def _rules() -> RulesConfig:
    return RulesConfig(
        stat_generation="point_buy",
        point_buy_budget=27,
        ability_score_names=list(ORDER),
    )


def _mode_scene() -> CharCreationScene:
    return CharCreationScene(
        id="the_wager",
        title="The Wager",
        narration="How do you meet fate — ledger or bones?",
        choices=[
            CharCreationChoice(
                label="The Measured Path",
                description="Take the pack default.",
                mechanical_effects=MechanicalEffects(),
            ),
            CharCreationChoice(
                label="Roll the Bones",
                description="3d6, in order, and the dice stand.",
                mechanical_effects=MechanicalEffects(stat_generation="roll_the_bones"),
            ),
        ],
    )


def _bones_scene() -> CharCreationScene:
    """The rolling surface, gated on the adopted mode.

    Constructing this FAILS today: ``requires_stat_generation`` is the new
    scene field this story adds (model_config forbids extras).
    """
    return CharCreationScene(
        id="the_bones",
        title="The Bones",
        narration="Six casts, six fates, in order.",
        requires_stat_generation="roll_the_bones",
    )


def _name_scene() -> CharCreationScene:
    return CharCreationScene(
        id="the_name",
        title="Your Name",
        narration="Speak it.",
        allows_freeform=True,
    )


def _builder(
    scenes: list[CharCreationScene], *, rng: random.Random | None = None
) -> CharacterBuilder:
    return CharacterBuilder(scenes=scenes, rules=_rules(), rng=rng or random.Random(1234))


def _expected_rolls(seed: int) -> list[tuple[str, int]]:
    """Replay the builder's RNG convention: three d6 per stat, in order."""
    rng = random.Random(seed)
    out: list[tuple[str, int]] = []
    for name in ORDER:
        out.append((name, rng.randint(1, 6) + rng.randint(1, 6) + rng.randint(1, 6)))
    return out


class _AllOnes(random.Random):
    """RNG that bottoms out every die — the hopeless-character forge."""

    def randint(self, a: int, b: int) -> int:  # type: ignore[override]
        return a


# ---------------------------------------------------------------------------
# AC-1: mode selection — default unchanged when not selected
# ---------------------------------------------------------------------------


def test_default_choice_leaves_point_buy_untouched() -> None:
    """Picking the default choice never rolls; build() point-buys as before."""
    b = _builder([_mode_scene(), _name_scene()])
    b.apply_choice(0)
    assert b.rolled_stats() is None
    b.apply_freeform("Kael")
    character = b.build("Kael")
    assert set(character.stats.keys()) == set(ORDER)
    # Point-buy band, not a 3d6 spread: every value in [8, 15].
    assert all(8 <= v <= 15 for v in character.stats.values()), character.stats


# ---------------------------------------------------------------------------
# AC-2: roll correctness — six 3d6 in order, seeded, 3..18
# ---------------------------------------------------------------------------


def test_roll_the_bones_choice_rolls_six_in_order_seeded() -> None:
    """Adopting the mode rolls immediately: names in ORDER, values replayable."""
    seed = 99
    b = _builder([_mode_scene(), _name_scene()], rng=random.Random(seed))
    b.apply_choice(1)
    rolled = b.rolled_stats()
    assert rolled is not None, "roll_the_bones choice must roll eagerly"
    assert [name for name, _ in rolled] == ORDER
    assert rolled == _expected_rolls(seed)


@pytest.mark.parametrize("seed", range(12))
def test_roll_values_within_3_18(seed: int) -> None:
    b = _builder([_mode_scene(), _name_scene()], rng=random.Random(seed))
    b.apply_choice(1)
    rolled = b.rolled_stats()
    assert rolled is not None
    assert len(rolled) == 6
    assert all(3 <= total <= 18 for _, total in rolled), rolled


def test_built_character_uses_rolled_values() -> None:
    """generate_stats('roll_the_bones') hands the rolled array to build()."""
    b = _builder([_mode_scene(), _name_scene()], rng=random.Random(7))
    b.apply_choice(1)
    rolled = b.rolled_stats()
    assert rolled is not None
    b.apply_freeform("Marrow")
    character = b.build("Marrow")
    assert character.stats == dict(rolled)


def test_stat_bonuses_still_apply_additively() -> None:
    """The universal generate_stats contract holds for the new method."""
    boost_scene = CharCreationScene(
        id="the_brand",
        title="The Brand",
        narration="The mark on your arm.",
        choices=[
            CharCreationChoice(
                label="Iron Brand",
                description="+1 STR",
                mechanical_effects=MechanicalEffects(stat_bonuses={"STR": 1}),
            ),
        ],
    )
    b = _builder([_mode_scene(), boost_scene, _name_scene()], rng=random.Random(21))
    b.apply_choice(1)
    rolled = b.rolled_stats()
    assert rolled is not None
    base = dict(rolled)
    b.apply_choice(0)
    b.apply_freeform("Sear")
    character = b.build("Sear")
    assert character.stats["STR"] == base["STR"] + 1
    for name in ORDER[1:]:
        assert character.stats[name] == base[name]


# ---------------------------------------------------------------------------
# AC-5: hopeless characters allowed — no floor, no uplift, no clamp
# ---------------------------------------------------------------------------


def test_hopeless_array_stands() -> None:
    """All-3s survives to the built character. Nothing 'helps'."""
    b = _builder([_mode_scene(), _name_scene()], rng=_AllOnes())
    b.apply_choice(1)
    rolled = b.rolled_stats()
    assert rolled is not None
    assert [total for _, total in rolled] == [3, 3, 3, 3, 3, 3]
    b.apply_freeform("Doomed Pete")
    character = b.build("Doomed Pete")
    assert all(v == 3 for v in character.stats.values()), (
        f"hopeless array was uplifted: {character.stats}"
    )


def test_unknown_stat_generation_still_raises() -> None:
    """Regression pin: adding the new method must not loosen the loud gate."""
    b = _builder([_mode_scene(), _name_scene()])
    b._stat_generation = "calvinball"
    with pytest.raises(UnknownStatGenerationError):
        b.generate_stats(b.accumulated())


# ---------------------------------------------------------------------------
# Scene gating — requires_stat_generation skip-walk (103-2 doctrine)
# ---------------------------------------------------------------------------


def test_bones_scene_skipped_on_default_path() -> None:
    """Default pick walks straight past the gated scene; ledger stays 1:1."""
    b = _builder([_mode_scene(), _bones_scene(), _name_scene()])
    b.apply_choice(0)
    assert b.current_scene().id == "the_name"
    # One presented scene answered -> exactly one SceneResult (the 103-2
    # one-result-per-presented-scene invariant).
    assert len(b._results) == 1


def test_bones_scene_presented_on_bones_path() -> None:
    b = _builder([_mode_scene(), _bones_scene(), _name_scene()])
    b.apply_choice(1)
    assert b.current_scene().id == "the_bones"


def test_go_back_across_skipped_bones_scene() -> None:
    """The regression class that bit 103-2: back-nav over a skipped gate
    must land on the mode scene, never the skipped bones scene."""
    b = _builder([_mode_scene(), _bones_scene(), _name_scene()])
    b.apply_choice(0)
    assert b.current_scene().id == "the_name"
    b.go_back()
    assert b.current_scene().id == "the_wager"


# ---------------------------------------------------------------------------
# OTEL lie-detector — every die is on the record
# ---------------------------------------------------------------------------


def test_roll_fires_stat_roll_span_per_stat() -> None:
    from sidequest.telemetry.spans import SPAN_CHARGEN_STAT_ROLL

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    b = _builder([_mode_scene(), _name_scene()], rng=random.Random(3))
    with tracer.start_as_current_span("chargen-test"):
        b.apply_choice(1)

    events = [
        e
        for span in exporter.get_finished_spans()
        for e in (span.events or [])
        if e.name == SPAN_CHARGEN_STAT_ROLL
    ]
    assert len(events) == 6, "one stat-roll event per ability score"
    rolled = dict(b.rolled_stats() or [])
    seen_stats = []
    for event in events:
        attrs = dict(event.attributes or {})
        stat = attrs["stat"]
        dice = list(attrs["dice"])
        seen_stats.append(stat)
        assert len(dice) == 3
        assert all(1 <= d <= 6 for d in dice)
        assert sum(dice) == attrs["total"] == rolled[stat]
    assert seen_stats == ORDER
