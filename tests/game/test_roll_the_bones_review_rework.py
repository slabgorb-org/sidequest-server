"""Story 103-3 review-rework RED — pins the Reviewer's repro-confirmed findings.

Review 2026-06-11 (REJECTED):

- [HIGH] Stale bones mode survives back-navigation: pick Roll the Bones,
  go_back, pick the DEFAULT choice — the gated bones scene was still
  presented and build() used the stale rolled array (AC-1 violation).
  The mode adoption was mutated state with no ledger anchor; undo must be
  ledger-driven (the 103-2 doctrine), keyed off the popped SceneResult's
  ``effects_applied.stat_generation``.
- [MEDIUM] ``reroll_stat`` was accepted off the bones scene — after
  ``apply_bones_confirm`` ("Lock the ... array") leftover budget could
  still reroll from the name scene or after the confirmation summary.
  Rerolls are only legal while the bones scene is the current scene.
- [MEDIUM] Back + re-pick re-rolled the entire array with a fresh budget —
  unlimited full-array fishing made the 2-stat budget decorative.
  Re-adoption must be idempotent: the existing array, budget, and
  rerolled-set survive; no new rolls, spans, or broadcasts fire.
- [LOW] The unknown-stat ValueError recited the full ability_score_names
  list — a configuration oracle for homebrew packs. Name the offender
  only; the valid set is already visible in the bones frame.
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
            id="the_bones",
            title="The Bones",
            narration="Six casts, in order.",
            requires_stat_generation="roll_the_bones",
        ),
        CharCreationScene(
            id="the_name",
            title="Your Name",
            narration="Speak it.",
            allows_freeform=True,
        ),
    ]


def _builder(seed: int = 7) -> CharacterBuilder:
    return CharacterBuilder(scenes=_scenes(), rules=_rules(), rng=random.Random(seed))


# ---------------------------------------------------------------------------
# [HIGH] back-nav out of bones, then default — the player's choice wins
# ---------------------------------------------------------------------------


def test_go_back_then_default_pick_clears_bones_mode() -> None:
    """The single worst path from review: a player peeks at Roll the Bones,
    recoils, goes back, and picks the default. The engine must honor the
    default — bones scene skipped, point-buy stats, no dice in the build."""
    b = _builder()
    b.apply_choice(1)
    assert b.current_scene().id == "the_bones"
    b.go_back()
    assert b.current_scene().id == "the_wager"
    b.apply_choice(0)
    # Bones scene must be SKIPPED — the default path goes straight to name.
    assert b.current_scene().id == "the_name", (
        "stale roll_the_bones mode leaked across go_back — the default pick "
        "must not walk the bones scene"
    )
    b.apply_freeform("Prudence")
    character = b.build("Prudence")
    # Point-buy band, not the stale 3d6 spread.
    assert all(8 <= v <= 15 for v in character.stats.values()), (
        f"build() used stale rolled stats after the player chose default: {character.stats}"
    )


def test_go_back_then_default_does_not_emit_bones_frame_state() -> None:
    """After reverting to default, the budget surface reads not-in-mode."""
    b = _builder()
    b.apply_choice(1)
    b.go_back()
    b.apply_choice(0)
    assert b.reroll_budget_remaining is None, (
        "reroll budget still armed after the player backed out of bones mode"
    )


def test_revert_alias_also_clears_bones_mode() -> None:
    """go_back's sibling revert() must honor the same ledger-driven undo."""
    b = _builder()
    b.apply_choice(1)
    b.revert()
    assert b.current_scene().id == "the_wager"
    b.apply_choice(0)
    assert b.current_scene().id == "the_name"


# ---------------------------------------------------------------------------
# [MEDIUM] rerolls are only legal while the bones scene is current
# ---------------------------------------------------------------------------


def test_reroll_after_bones_confirm_rejected() -> None:
    """'Lock the Roll the Bones array' must actually lock: leftover budget
    cannot be spent from the name scene after confirm."""
    b = _builder()
    b.apply_choice(1)
    b.apply_bones_confirm()
    assert b.current_scene().id == "the_name"
    before = dict(b.rolled_stats() or [])
    with pytest.raises(RuntimeError):
        b.reroll_stat("STR")
    assert dict(b.rolled_stats() or []) == before, "post-confirm reroll mutated the array"


def test_reroll_after_summary_phase_rejected() -> None:
    """Confirmation phase (summary on screen) is the juiciest reroll window —
    derived projections visible. Must be rejected, array untouched."""
    b = _builder()
    b.apply_choice(1)
    b.apply_bones_confirm()
    b.apply_freeform("Knuckles")
    assert b.is_confirmation()
    before = dict(b.rolled_stats() or [])
    with pytest.raises(RuntimeError):
        b.reroll_stat("DEX")
    assert dict(b.rolled_stats() or []) == before


def test_reroll_on_bones_scene_still_works() -> None:
    """The gate must not over-correct: on the bones scene, rerolls flow."""
    b = _builder()
    b.apply_choice(1)
    assert b.current_scene().id == "the_bones"
    b.reroll_stat("WIS")
    assert b.reroll_budget_remaining == 1


# ---------------------------------------------------------------------------
# [MEDIUM] idempotent re-adoption — no full-array fishing via Back
# ---------------------------------------------------------------------------


def test_repick_bones_preserves_array_and_budget() -> None:
    """Back + re-pick must NOT be a free full-array reroll. The first
    adoption's array and remaining budget survive re-adoption."""
    b = _builder()
    b.apply_choice(1)
    first = list(b.rolled_stats() or [])
    b.reroll_stat("STR")
    after_reroll = list(b.rolled_stats() or [])
    assert b.reroll_budget_remaining == 1
    b.go_back()
    b.apply_choice(1)
    assert b.current_scene().id == "the_bones"
    assert list(b.rolled_stats() or []) == after_reroll, (
        "re-adoption re-rolled the array — reroll-budget fishing via Back"
    )
    assert b.reroll_budget_remaining == 1, "re-adoption reset the spent budget"
    assert first != after_reroll  # sanity: the STR reroll actually changed something


def test_repick_bones_fires_no_new_rolls() -> None:
    """Re-adoption is silent: no new SPAN_CHARGEN_STAT_ROLL events, no new
    DiceResult broadcasts queued. The dice already on the table stand."""
    from sidequest.telemetry.spans import SPAN_CHARGEN_STAT_ROLL

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    b = _builder()
    b.apply_choice(1)
    b.consume_bones_broadcasts()  # drain the legitimate adoption broadcasts
    b.go_back()
    with tracer.start_as_current_span("repick"):
        b.apply_choice(1)
    events = [
        e
        for span in exporter.get_finished_spans()
        for e in (span.events or [])
        if e.name == SPAN_CHARGEN_STAT_ROLL
    ]
    assert events == [], "re-adoption rolled new dice"
    assert b.consume_bones_broadcasts() == [], "re-adoption queued new DiceResult broadcasts"


def test_same_stat_still_blocked_after_repick() -> None:
    """The once-each ledger survives re-adoption too."""
    from sidequest.game.builder import StatAlreadyRerolledError

    b = _builder()
    b.apply_choice(1)
    b.reroll_stat("CHA")
    b.go_back()
    b.apply_choice(1)
    with pytest.raises(StatAlreadyRerolledError, match="CHA"):
        b.reroll_stat("CHA")


# ---------------------------------------------------------------------------
# [LOW] unknown-stat error names the offender only — no configuration oracle
# ---------------------------------------------------------------------------


def test_unknown_stat_error_does_not_recite_ability_list() -> None:
    b = _builder()
    b.apply_choice(1)
    with pytest.raises(ValueError, match="LUCK") as excinfo:
        b.reroll_stat("LUCK")
    message = str(excinfo.value)
    for name in ORDER:
        assert name not in message, (
            f"unknown-stat error recites the ability list ({name!r} found): {message}"
        )
