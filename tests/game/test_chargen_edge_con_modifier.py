"""Tests for Story 39-10 — Chargen Edge seed += CON modifier.

ADR-078 amendment 2026-05-10: retire the Story 39-4 Fighter +2 smoke-gate
stub and replace it with a general CON-modifier formula for Edge seed.

Formula: ``edge.base_max += (CON_score - 10) // 2``, floored at 1.

Expected (Fighter base_max=4 per caverns_and_claudes edge_config):
  - CON 17 -> mod +3 -> Edge 7  (was 6 with the +2 stub)
  - CON 9  -> mod -1 -> Edge 3  (was 6 with the +2 stub)
  - CON 3  -> mod -4 -> Edge 1  (floored)
  - CON 10 -> mod  0 -> Edge 4  (unchanged from base)

These tests are RED until Dev wires CON through and removes the stub.
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game import creature_core as creature_core_mod
from sidequest.game.builder import CharacterBuilder
from sidequest.game.creature_core import hp_pool_from_config
from sidequest.genre.models.character import (
    CharCreationChoice,
    CharCreationScene,
    MechanicalEffects,
)
from sidequest.genre.models.rules import (
    EdgeConfig,
    EdgeThresholdDecl,
    RulesConfig,
)

ABILITY_NAMES = ["STR", "DEX", "CON", "INT", "WIS", "CHA"]


def _make_choice(label: str, **fx: object) -> CharCreationChoice:
    return CharCreationChoice(
        label=label,
        description="desc",
        mechanical_effects=MechanicalEffects(**fx),  # type: ignore[arg-type]
    )


def _make_scene(scene_id: str, choices: list[CharCreationChoice]) -> CharCreationScene:
    return CharCreationScene(
        id=scene_id,
        title="T",
        narration="N",
        choices=choices,
    )


def _caverns_edge_config() -> EdgeConfig:
    """Fixture matching caverns_and_claudes edge_config base_max_by_class."""
    return EdgeConfig(
        base_max_by_class={
            "Fighter": 4,
            "Cleric": 3,
            "Mage": 2,
            "Thief": 2,
        },
        thresholds=[
            EdgeThresholdDecl(at=1, event_id="edge_strained", narrator_hint="Fraying."),
        ],
    )


def _rules_for_class(class_name: str) -> RulesConfig:
    """3d6_strict rules so callers can override _rolled_stats deterministically."""
    return RulesConfig(
        stat_generation="roll_3d6_strict",
        ability_score_names=list(ABILITY_NAMES),
        default_class=class_name,
        default_race="Human",
        edge_config=_caverns_edge_config(),
    )


def _builder_with_class_and_con(class_name: str, con_score: int) -> CharacterBuilder:
    """Build a one-class-scene builder, force CON to `con_score`."""
    rules = _rules_for_class(class_name)
    scenes = [
        _make_scene(
            "class",
            choices=[_make_choice(class_name, class_hint=class_name)],
        ),
    ]
    b = CharacterBuilder(scenes=scenes, rules=rules)
    # Override the rolled stats deterministically. STR/DEX/INT/WIS/CHA
    # are not load-bearing for Edge math; only CON matters.
    b._rolled_stats = [
        ("STR", 10),
        ("DEX", 10),
        ("CON", con_score),
        ("INT", 10),
        ("WIS", 10),
        ("CHA", 10),
    ]
    b.apply_choice(0)
    assert b.is_confirmation()
    return b


# ===========================================================================
# Unit tests on edge_pool_from_config — the formula itself
# ===========================================================================


class TestHpPoolFromConfigConModifier:
    """Direct unit tests on the function — bypasses the builder."""

    @pytest.mark.parametrize(
        ("class_name", "con_score", "expected_max"),
        [
            # Fighter base 4
            ("Fighter", 3, 1),  # mod -4 -> floor 1
            ("Fighter", 9, 3),  # mod -1
            ("Fighter", 10, 4),  # mod 0
            ("Fighter", 14, 6),  # mod +2
            ("Fighter", 17, 7),  # mod +3
            # Cleric base 3
            ("Cleric", 3, 1),  # mod -4 -> floor 1
            ("Cleric", 9, 2),  # mod -1
            ("Cleric", 10, 3),  # mod 0
            ("Cleric", 14, 5),  # mod +2
            ("Cleric", 17, 6),  # mod +3
            # Mage base 2
            ("Mage", 3, 1),  # mod -4 -> floor 1
            ("Mage", 9, 1),  # mod -1 -> base 1, still valid; if Dev wants strict floor it's still 1
            ("Mage", 10, 2),  # mod 0
            ("Mage", 14, 4),  # mod +2
            ("Mage", 17, 5),  # mod +3
            # Thief base 2
            ("Thief", 3, 1),  # mod -4 -> floor 1
            ("Thief", 9, 1),  # mod -1
            ("Thief", 10, 2),  # mod 0
            ("Thief", 14, 4),  # mod +2
            ("Thief", 17, 5),  # mod +3
        ],
    )
    def test_hp_pool_applies_con_modifier(
        self, class_name: str, con_score: int, expected_max: int
    ) -> None:
        cfg = _caverns_edge_config()
        pool = hp_pool_from_config(cfg, class_name, con_score=con_score)
        assert pool.base_max == expected_max, (
            f"{class_name} with CON {con_score}: expected base_max={expected_max}, "
            f"got {pool.base_max}"
        )
        assert pool.max == expected_max
        assert pool.current == expected_max

    def test_floor_at_one_for_very_low_con(self) -> None:
        """CON 3 across every class must floor at 1 — character is alive."""
        cfg = _caverns_edge_config()
        for class_name in ("Fighter", "Cleric", "Mage", "Thief"):
            pool = hp_pool_from_config(cfg, class_name, con_score=3)
            assert pool.base_max >= 1, f"{class_name} CON 3 collapsed below 1"
            assert pool.max >= 1

    def test_con_10_is_neutral(self) -> None:
        """CON 10 (mod 0) must not change base_max — sanity check on the formula."""
        cfg = _caverns_edge_config()
        for class_name, expected_base in (
            ("Fighter", 4),
            ("Cleric", 3),
            ("Mage", 2),
            ("Thief", 2),
        ):
            pool = hp_pool_from_config(cfg, class_name, con_score=10)
            assert pool.base_max == expected_base


# ===========================================================================
# Builder integration — CON flows from rolled_stats into Edge
# ===========================================================================


class TestBuilderEdgeSeedingWithCon:
    """End-to-end: rolled CON should land in the seeded Edge pool."""

    def test_fighter_con_17_seeds_edge_7(self) -> None:
        b = _builder_with_class_and_con("Fighter", 17)
        char = b.build("Boudica")
        assert char.core.hp.base_max == 7
        assert char.core.hp.max == 7
        assert char.core.hp.current == 7

    def test_fighter_con_9_seeds_edge_3(self) -> None:
        """The +2 stub previously made this also land at 6; new formula gives 3."""
        b = _builder_with_class_and_con("Fighter", 9)
        char = b.build("Old Marcus")
        assert char.core.hp.base_max == 3
        assert char.core.hp.max == 3

    def test_fighter_con_3_floors_at_1(self) -> None:
        b = _builder_with_class_and_con("Fighter", 3)
        char = b.build("Sickly Tom")
        assert char.core.hp.base_max == 1
        assert char.core.hp.max == 1
        assert char.core.hp.current == 1

    def test_mage_con_17_seeds_edge_5(self) -> None:
        """Mage base 2 + CON +3 = 5. Universal: CON applies to all classes."""
        b = _builder_with_class_and_con("Mage", 17)
        char = b.build("Iron-lunged Mage")
        assert char.core.hp.base_max == 5

    def test_cleric_con_14_seeds_edge_5(self) -> None:
        b = _builder_with_class_and_con("Cleric", 14)
        char = b.build("Hale Cleric")
        assert char.core.hp.base_max == 5

    def test_fighter_plus_two_stub_no_longer_applied(self) -> None:
        """Regression: with CON 10 (mod 0), Fighter should land at base 4, not
        base + 2 = 6. If this asserts 6, the Story 39-4 stub is still alive."""
        b = _builder_with_class_and_con("Fighter", 10)
        char = b.build("Average Fighter")
        assert char.core.hp.base_max == 4, (
            "Fighter +2 stub appears to still be applied — story 39-10 retires it"
        )
        assert char.core.hp.max == 4


# ===========================================================================
# OTEL — new event shape, old event retired
# ===========================================================================


def _fresh_otel() -> tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _events_by_name(exporter: InMemorySpanExporter) -> dict[str, list]:
    result: dict[str, list] = {}
    for span in exporter.get_finished_spans():
        for event in span.events:
            result.setdefault(event.name, []).append(event)
    return result


class TestEdgeSeededOtelEvent:
    """`chargen.hp_seeded` must carry the new CON-mod fields; the legacy
    `chargen.advancement_stub_applied` must no longer be emitted."""

    def test_hp_seeded_event_includes_con_modifier_and_formula(self) -> None:
        provider, exporter = _fresh_otel()
        tracer = provider.get_tracer("test")

        b = _builder_with_class_and_con("Fighter", 17)
        with tracer.start_as_current_span("build_span"):
            b.build("Boudica")

        events = _events_by_name(exporter)
        assert "chargen.hp_seeded" in events, (
            f"chargen.hp_seeded missing; events seen: {sorted(events)}"
        )
        attrs = dict(events["chargen.hp_seeded"][0].attributes or {})
        assert attrs.get("con_modifier") == 3, (
            f"con_modifier should be +3 for CON 17; got {attrs!r}"
        )
        assert attrs.get("seed_formula") == "class_base+con_mod", (
            f"seed_formula should be 'class_base+con_mod'; got {attrs!r}"
        )
        # base_max attribute remains useful — should now reflect the
        # post-modifier value, not the unmodified class base.
        assert attrs.get("base_max") == 7

    def test_hp_seeded_event_records_negative_con_modifier(self) -> None:
        provider, exporter = _fresh_otel()
        tracer = provider.get_tracer("test")

        b = _builder_with_class_and_con("Fighter", 9)
        with tracer.start_as_current_span("build_span"):
            b.build("Old Marcus")

        events = _events_by_name(exporter)
        attrs = dict(events["chargen.hp_seeded"][0].attributes or {})
        assert attrs.get("con_modifier") == -1
        assert attrs.get("base_max") == 3

    def test_advancement_stub_applied_event_no_longer_emitted(self) -> None:
        """The 39-4 OTEL event for the +2 stub should be gone — its
        subsystem is dead. Emitting it on retired logic is illusionism."""
        provider, exporter = _fresh_otel()
        tracer = provider.get_tracer("test")

        b = _builder_with_class_and_con("Fighter", 17)
        with tracer.start_as_current_span("build_span"):
            b.build("Boudica")

        events = _events_by_name(exporter)
        assert "chargen.advancement_stub_applied" not in events, (
            "chargen.advancement_stub_applied event survived — Story 39-4 "
            "stub should be retired in Story 39-10"
        )


# ===========================================================================
# Wiring — production builder path actually flows CON into the function
# ===========================================================================


class TestChargenAccumulatorFlowsConIntoHp:
    """Spy on hp_pool_from_config to prove the builder passes con_score
    drawn from the rolled stats — not just a default. This catches the
    failure mode where Dev extends the function signature but forgets to
    update the call site (CLAUDE.md 'Verify Wiring, Not Just Existence')."""

    def test_builder_passes_rolled_con_to_hp_pool_from_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}
        real_fn = creature_core_mod.hp_pool_from_config

        def spy(
            edge_config: object,
            class_name: str,
            *args: object,
            **kwargs: object,
        ):
            captured["args"] = args
            captured["kwargs"] = kwargs
            captured["class_name"] = class_name
            return real_fn(edge_config, class_name, *args, **kwargs)

        # Patch where builder.py imports it from (creature_core).
        monkeypatch.setattr("sidequest.game.builder.hp_pool_from_config", spy)

        b = _builder_with_class_and_con("Fighter", 14)
        b.build("Wired Fighter")

        assert "kwargs" in captured or "args" in captured, "spy never invoked"
        # The con_score should arrive as either a kwarg or a positional.
        kwargs = captured.get("kwargs") or {}
        args = captured.get("args") or ()
        con_observed = (
            kwargs.get("con_score") if "con_score" in kwargs else (args[0] if args else None)
        )
        assert con_observed == 14, (
            f"edge_pool_from_config was called but con_score was not 14; "
            f"args={args!r} kwargs={kwargs!r}. Likely Dev extended the "
            f"function signature but forgot to update the builder call site."
        )
        assert captured["class_name"] == "Fighter"
