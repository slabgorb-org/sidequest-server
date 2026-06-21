"""Story 153-4 — SWN narrative chargen must produce the shaped WN "14-to-7"
attribute spread, not the flat point-buy array.

FINDING (epic-153 playtest sweep, space_opera/SWN): a freshly-built SWN
character comes out with **flat** ability scores — point-buy budget 27 spread
round-robin across the pack's six attributes yields ``[13, 13, 13, 12, 12, 12]``.
Under the WN modifier ladder (8-13 → +0) *every* attribute modifier is 0, so the
character has no mechanical shape at all — no prime, no dump stat.

The fix (ADR-142 "shaped-attribute retune" / ADR-143 ruleset-owned chargen seam):
the WN family OWNS attribute generation and emits the canonical WWN/SWN SRD
standard array ``[14, 12, 11, 10, 9, 7]`` — the literal "14-to-7 spread" — which
the three WWN packs (caverns_and_claudes, elemental_harmony, heavy_metal) already
use via ``RulesConfig.standard_array``. The player never reaches a point-buy
surface in narrative chargen, so the point-buy path is dead weight under a WN
binding; the WN ruleset supersedes it with the spread regardless of what the pack
authored for ``stat_generation``.

These tests drive the REAL production seams (``CharacterBuilder.generate_stats``
and ``CharacterBuilder.build``) with synthetic rules that mirror the real
space_opera SWN pack (``ruleset: swn``, ``stat_generation: point_buy``,
``point_buy_budget: 27``, six named abilities). No Claude calls.

RED until the WN attribute seam emits the shaped spread:
  current  generate_stats → {Physique:13, Reflex:13, Intellect:13,
                             Cunning:12, Resolve:12, Influence:12}  (all +0)
  expected generate_stats → values == [14, 12, 11, 10, 9, 7]
                             prime (Physique) == 14 (+1), a dump stat == 7 (-1)
"""

from __future__ import annotations

import random

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import sidequest.telemetry.spans as spans_module
from sidequest.game.builder import CharacterBuilder
from sidequest.game.ruleset.swn import swn_attribute_modifier
from sidequest.genre.models.character import (
    CharCreationChoice,
    CharCreationScene,
    ClassDef,
    MechanicalEffects,
)
from sidequest.genre.models.rules import RulesConfig

# ---------------------------------------------------------------------------
# Synthetic SWN fixtures — mirror the real space_opera pack rules.yaml exactly.
# ---------------------------------------------------------------------------

# The canonical WWN/SWN SRD standard array — the "14-to-7 spread" the story names.
# The three live WWN packs already author this via RulesConfig.standard_array.
WN_SHAPED_SPREAD = [14, 12, 11, 10, 9, 7]

# space_opera's six ability names (rules.yaml ability_score_names), in order.
SWN_ABILITY_NAMES = ["Physique", "Reflex", "Intellect", "Cunning", "Resolve", "Influence"]

# space_opera's swn.attribute_map (rules.yaml).
SWN_ATTRIBUTE_MAP = {
    "STRENGTH": "Physique",
    "CONSTITUTION": "Resolve",
    "DEXTERITY": "Reflex",
    "INTELLIGENCE": "Intellect",
    "WISDOM": "Cunning",
    "CHARISMA": "Influence",
}


def _swn_rules() -> RulesConfig:
    """Real space_opera SWN rules: swn ruleset, flat point-buy 27, six abilities.

    This is the *current authored* config — the one that produces flat stats.
    The fix must shape these even though the pack authors point_buy, because the
    player never reaches a point-buy surface in narrative chargen (the WN ruleset
    owns attribute generation per ADR-142/143)."""
    return RulesConfig.model_validate(
        {
            "ruleset": "swn",
            "stat_generation": "point_buy",
            "point_buy_budget": 27,
            "ability_score_names": SWN_ABILITY_NAMES,
            "swn": {"attribute_map": SWN_ATTRIBUTE_MAP},
        }
    )


def _soldier_class() -> ClassDef:
    """SWN Soldier — Physique prime (matches the real classes.yaml)."""
    return ClassDef.model_validate(
        {
            "id": "soldier",
            "display_name": "Soldier",
            "rpg_role": "fighter",
            "jungian_default": "Hero",
            "prime_requisite": "Physique",
            "minimum_score": 9,
            "kit_table": "k",
        }
    )


def _pick_soldier_scene() -> list[CharCreationScene]:
    return [
        CharCreationScene(
            id="crucible",
            title="What forged you?",
            narration="N",
            choices=[
                CharCreationChoice(
                    label="Soldier",
                    description="A frontline veteran.",
                    mechanical_effects=MechanicalEffects(class_hint="Soldier"),
                )
            ],
        )
    ]


def _build_soldier_acc(builder: CharacterBuilder):
    """Walk the single class-pick scene and return the accumulated choices."""
    builder.apply_choice(0)
    return builder.accumulated()


def _stats_via_generate(builder: CharacterBuilder) -> dict[str, int]:
    acc = _build_soldier_acc(builder)
    return builder.generate_stats(acc)


@pytest.fixture
def span_exporter(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    """Route Span.open through an in-memory exporter (pattern from
    test_chargen_seam_wiring.py)."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test.swn_shaped_spread")
    monkeypatch.setattr(spans_module, "tracer", lambda: tracer)
    return exporter


# ---------------------------------------------------------------------------
# AC-1: SWN narrative chargen applies the WN 14-to-7 attribute spread.
# ---------------------------------------------------------------------------


def test_swn_generate_stats_yields_shaped_14_to_7_spread() -> None:
    """The six generated ability values ARE the WN SRD standard array.

    RED today: point-buy 27 round-robin yields [13,13,13,12,12,12]. The fix must
    supersede the dead point-buy path with the shaped spread."""
    builder = CharacterBuilder(
        scenes=_pick_soldier_scene(), rules=_swn_rules(), rng=random.Random(1)
    ).with_classes([_soldier_class()])

    stats = _stats_via_generate(builder)

    assert sorted(stats.values(), reverse=True) == WN_SHAPED_SPREAD, (
        f"SWN chargen must emit the WN 14-to-7 spread {WN_SHAPED_SPREAD}; "
        f"got {sorted(stats.values(), reverse=True)} (flat point-buy is the bug)"
    )


def test_swn_chargen_is_not_mechanically_flat() -> None:
    """The character must have a real mechanical shape — at least two DISTINCT WN
    attribute modifiers across its stats.

    This is the essence of the finding: [13,13,13,12,12,12] gives every stat a +0
    modifier (1 distinct modifier) — flat. The shaped spread gives {+1, 0, -1}."""
    builder = CharacterBuilder(
        scenes=_pick_soldier_scene(), rules=_swn_rules(), rng=random.Random(2)
    ).with_classes([_soldier_class()])

    stats = _stats_via_generate(builder)
    modifiers = {swn_attribute_modifier(v) for v in stats.values()}

    assert len(modifiers) >= 2, (
        f"SWN character is mechanically flat — all stats share one modifier "
        f"{modifiers}. Expected a differentiated spread with multiple modifier "
        f"bands. Stats: {stats}"
    )


def test_swn_prime_requisite_gets_the_top_value() -> None:
    """AC-1: the Calling's prime requisite is the single highest score (14, +1).

    Soldier's prime is Physique. WN prime-aware placement already exists; it just
    needs a pool whose top value is 14. RED today: prime sits at the flat 13."""
    builder = CharacterBuilder(
        scenes=_pick_soldier_scene(), rules=_swn_rules(), rng=random.Random(3)
    ).with_classes([_soldier_class()])

    stats = _stats_via_generate(builder)

    assert stats["Physique"] == 14, (
        f"Soldier's prime (Physique) must land the spread's top value 14; "
        f"got {stats['Physique']}. Stats: {stats}"
    )
    assert swn_attribute_modifier(stats["Physique"]) == 1, (
        "the prime requisite must carry a +1 modifier under the shaped spread"
    )
    # Unique max — no other stat ties the prime.
    assert list(stats.values()).count(14) == 1, f"the prime must be the SOLE top score; got {stats}"


def test_swn_chargen_has_a_dump_stat_at_minus_one() -> None:
    """The "to 7" end of the spread: the lowest stat is 7 (a -1 dump stat).

    A flat point-buy character has no dump stat (min 12, +0). The shaped spread's
    floor is 7."""
    builder = CharacterBuilder(
        scenes=_pick_soldier_scene(), rules=_swn_rules(), rng=random.Random(4)
    ).with_classes([_soldier_class()])

    stats = _stats_via_generate(builder)

    assert min(stats.values()) == 7, (
        f"the spread must bottom out at 7 (a -1 dump stat); got min {min(stats.values())}. "
        f"Stats: {stats}"
    )
    assert swn_attribute_modifier(min(stats.values())) == -1


# ---------------------------------------------------------------------------
# AC-4 (synthetic wiring): the shape survives the full production build() path.
# ---------------------------------------------------------------------------


def test_swn_built_character_carries_the_shaped_spread() -> None:
    """WIRING: the shaped spread reaches the final Character via the real
    ``build()`` finalization, not just the ``generate_stats`` seam in isolation."""
    builder = CharacterBuilder(
        scenes=_pick_soldier_scene(), rules=_swn_rules(), rng=random.Random(5)
    ).with_classes([_soldier_class()])
    builder.apply_choice(0)
    assert builder.is_confirmation(), f"expected confirmation; phase={builder._phase}"

    character = builder.build("Vasquez")

    assert sorted(character.stats.values(), reverse=True) == WN_SHAPED_SPREAD, (
        f"built SWN Character must carry the shaped spread; got {character.stats}"
    )
    assert character.stats["Physique"] == 14, (
        f"built Soldier's prime (Physique) must be 14; got {character.stats}"
    )


# ---------------------------------------------------------------------------
# AC-3: OTEL watcher visibility — the lie-detector confirms the shaped placement.
# ---------------------------------------------------------------------------


def test_swn_attributes_assigned_span_reports_shaped_top(
    span_exporter: InMemorySpanExporter,
) -> None:
    """``swn.chargen.attributes_assigned`` must fire reporting the shaped top
    value 14 and the Physique prime — so the GM panel can confirm the spread
    actually drove placement (not flat improvisation).

    RED today: the span fires with top=13 (flat point-buy pool)."""
    builder = CharacterBuilder(
        scenes=_pick_soldier_scene(), rules=_swn_rules(), rng=random.Random(6)
    ).with_classes([_soldier_class()])
    builder.apply_choice(0)
    builder.build("Vasquez")

    finished = span_exporter.get_finished_spans()
    spans = [s for s in finished if s.name == "swn.chargen.attributes_assigned"]
    assert spans, (
        f"swn.chargen.attributes_assigned span must fire during chargen; "
        f"got {[s.name for s in finished]}"
    )
    span = spans[-1]
    assert span.attributes["top"] == 14, (
        f"attributes_assigned span must report the shaped top value 14; "
        f"got top={span.attributes['top']} (flat point-buy is the bug)"
    )
    assert span.attributes["prime"] == "Physique", (
        f"span must report the Soldier's prime (Physique); got {span.attributes['prime']!r}"
    )
