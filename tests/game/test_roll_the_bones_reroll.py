"""Story 103-3 RED — the 2-stat reroll budget (build plan §D-C).

Pinned contract:

- ``CharacterBuilder.reroll_stat(name)`` rerolls 3d6 for one stat.
  REPLACEMENT semantics: the new total stands even when lower (never
  best-of).
- Budget is two stats, once each, enforced server-side:
  - third distinct stat -> ``RerollBudgetExhaustedError`` (loud, names
    the stat)
  - same stat twice -> ``StatAlreadyRerolledError`` (loud, names the
    stat); a rejected attempt never burns budget
  - unknown stat name -> ``ValueError`` naming the offender
  - outside roll-the-bones mode -> ``RuntimeError`` (mirrors the
    "not in arrangement mode" precedent)
- ``reroll_budget_remaining`` is ``None`` outside the mode and counts
  2 -> 1 -> 0 inside it.
- Every reroll fires SPAN_CHARGEN_STAT_ROLL — rerolls are on the GM-panel
  record exactly like first rolls.
"""

from __future__ import annotations

import random

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.builder import CharacterBuilder
from sidequest.genre.models.character import (
    CharCreationChoice,
    CharCreationScene,
    MechanicalEffects,
)
from sidequest.genre.models.rules import RulesConfig

ORDER = ["STR", "DEX", "CON", "INT", "WIS", "CHA"]


class _ScriptedRng(random.Random):
    """Feed dice faces from a queue; explode loudly when it runs dry."""

    def __init__(self, faces: list[int]) -> None:
        super().__init__(0)
        self._faces = list(faces)

    def randint(self, a: int, b: int) -> int:  # type: ignore[override]
        if not self._faces:
            raise AssertionError("scripted RNG exhausted — test asked for too few faces")
        face = self._faces.pop(0)
        assert a <= face <= b, f"scripted face {face} outside [{a}, {b}]"
        return face


def _rules() -> RulesConfig:
    return RulesConfig(
        stat_generation="point_buy",
        point_buy_budget=27,
        ability_score_names=list(ORDER),
    )


def _scenes() -> list[CharCreationScene]:
    return [
        CharCreationScene(
            id="the_wager",
            title="The Wager",
            narration="Ledger or bones?",
            choices=[
                CharCreationChoice(
                    label="The Measured Path",
                    description="Default.",
                    mechanical_effects=MechanicalEffects(),
                ),
                CharCreationChoice(
                    label="Roll the Bones",
                    description="3d6 in order.",
                    mechanical_effects=MechanicalEffects(stat_generation="roll_the_bones"),
                ),
            ],
        ),
        CharCreationScene(
            id="the_name",
            title="Your Name",
            narration="Speak it.",
            allows_freeform=True,
        ),
    ]


def _bones_builder(rng: random.Random | None = None) -> CharacterBuilder:
    b = CharacterBuilder(scenes=_scenes(), rules=_rules(), rng=rng or random.Random(42))
    b.apply_choice(1)
    return b


# 18 sixes: initial roll makes every stat 18. Subsequent triples are the
# reroll faces, scripted per test.
_ALL_SIXES = [6] * 18


def test_reroll_replaces_value_even_when_lower() -> None:
    """Replacement, not best-of: an 18 rerolled into a 3 is a 3."""
    b = _bones_builder(_ScriptedRng(_ALL_SIXES + [1, 1, 1]))
    assert dict(b.rolled_stats() or [])["STR"] == 18
    b.reroll_stat("STR")
    assert dict(b.rolled_stats() or [])["STR"] == 3


def test_budget_counts_down_two_one_zero() -> None:
    b = _bones_builder(_ScriptedRng(_ALL_SIXES + [2, 3, 4, 5, 6, 1]))
    assert b.reroll_budget_remaining == 2
    b.reroll_stat("STR")
    assert b.reroll_budget_remaining == 1
    b.reroll_stat("DEX")
    assert b.reroll_budget_remaining == 0


def test_budget_is_none_outside_bones_mode() -> None:
    b = CharacterBuilder(scenes=_scenes(), rules=_rules(), rng=random.Random(42))
    assert b.reroll_budget_remaining is None


def test_third_distinct_stat_rejected() -> None:
    from sidequest.game.builder import RerollBudgetExhaustedError

    b = _bones_builder(_ScriptedRng(_ALL_SIXES + [1, 2, 3, 4, 5, 6]))
    b.reroll_stat("STR")
    b.reroll_stat("DEX")
    with pytest.raises(RerollBudgetExhaustedError, match="CON"):
        b.reroll_stat("CON")
    # The rejected attempt changed nothing.
    assert b.reroll_budget_remaining == 0
    assert dict(b.rolled_stats() or [])["CON"] == 18


def test_same_stat_twice_rejected_and_burns_no_budget() -> None:
    from sidequest.game.builder import StatAlreadyRerolledError

    b = _bones_builder(_ScriptedRng(_ALL_SIXES + [1, 1, 1]))
    b.reroll_stat("STR")
    assert b.reroll_budget_remaining == 1
    with pytest.raises(StatAlreadyRerolledError, match="STR"):
        b.reroll_stat("STR")
    assert b.reroll_budget_remaining == 1
    assert dict(b.rolled_stats() or [])["STR"] == 3


def test_unknown_stat_rejected_loudly() -> None:
    b = _bones_builder(_ScriptedRng(_ALL_SIXES))
    with pytest.raises(ValueError, match="LUCK"):
        b.reroll_stat("LUCK")
    assert b.reroll_budget_remaining == 2


def test_reroll_outside_bones_mode_rejected() -> None:
    b = CharacterBuilder(scenes=_scenes(), rules=_rules(), rng=random.Random(42))
    b.apply_choice(0)  # default path — no bones mode
    with pytest.raises(RuntimeError):
        b.reroll_stat("STR")


def test_rerolled_values_reach_built_character() -> None:
    b = _bones_builder(_ScriptedRng(_ALL_SIXES + [2, 2, 2]))
    b.reroll_stat("WIS")
    b.apply_freeform("Knuckles")
    character = b.build("Knuckles")
    assert character.stats["WIS"] == 6
    for name in ORDER:
        if name != "WIS":
            assert character.stats[name] == 18


def test_reroll_fires_stat_roll_span() -> None:
    from sidequest.telemetry.spans import SPAN_CHARGEN_STAT_ROLL

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    b = _bones_builder(_ScriptedRng(_ALL_SIXES + [4, 5, 6]))
    with tracer.start_as_current_span("reroll-test"):
        b.reroll_stat("DEX")

    events = [
        e
        for span in exporter.get_finished_spans()
        for e in (span.events or [])
        if e.name == SPAN_CHARGEN_STAT_ROLL
    ]
    assert len(events) == 1, "exactly one stat-roll event for one reroll"
    attrs = dict(events[0].attributes or {})
    assert attrs["stat"] == "DEX"
    assert list(attrs["dice"]) == [4, 5, 6]
    assert attrs["total"] == 15
