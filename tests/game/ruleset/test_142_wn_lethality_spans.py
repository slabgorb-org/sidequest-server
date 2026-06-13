"""Lethality span-parity net for the WN-family ruleset modules (ADR-142 DD-4).

Pins the CURRENT span names emitted by WWN and CWN lethality events, and
asserts the CORRECTED awn.* names that AWN SHOULD emit (but currently
does not — it inherits CwnRulesetModule verbatim and emits cwn.* instead).

Structure:
  - wwn + cwn params: plain (must PASS now and after Task 6).
  - awn params: wrapped in xfail(strict=True) — must FAIL now (emits cwn.*),
    must PASS after Task 6 when slug-parameterised emitters land.

Four lethality events, one parametrised test each:
  1. trauma.roll       — resolve_trauma with a weapon that has a trauma_die.
  2. system_strain.delta — apply_system_strain kind="temporary".
  3. shock.applied     — resolve_shock with shock weapon vs. low-AC target.
  4. mortal_injury.declared + major_injury.roll — resolve_downed with
     scene_traumatic=True and a seed that FAILS the physical save.

Exporter pattern: the local ``_exporter()`` helper (same as test_cwn_trauma.py
and test_cwn_system_strain.py). Builds a fresh TracerProvider+InMemorySpanExporter
per call, returns ``(exporter, tracer)``, and passes ``_tracer=tracer`` into
the ruleset method so spans land in the local exporter regardless of the global
TracerProvider state. No fixtures required — no conftest dependency.
"""

from __future__ import annotations

import random

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.creature_core import CreatureCore
from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.system_strain import SystemStrainPool
from sidequest.genre.models.inventory import DamageSpec
from sidequest.genre.models.rules import AwnConfig, CwnConfig, WwnConfig

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_AMAP = {
    "STRENGTH": "Brawn",
    "CONSTITUTION": "Body",
    "DEXTERITY": "Reflex",
    "INTELLIGENCE": "Tech",
    "WISDOM": "Instinct",
    "CHARISMA": "Cool",
}

# Config instances keyed by slug.
_CFG = {
    "wwn": WwnConfig(attribute_map=_AMAP),
    "cwn": CwnConfig(attribute_map=_AMAP),
    "awn": AwnConfig(attribute_map=_AMAP),
}


def _exporter():
    """Local in-memory span exporter (same pattern as test_cwn_trauma.py).

    Returns (exporter, tracer) — pass tracer as _tracer= to the ruleset
    method so spans land in the local exporter, not the global provider.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


def _strain_core() -> CreatureCore:
    return CreatureCore(
        name="Jax",
        description="runner",
        personality="cool",
        system_strain=SystemStrainPool(current=2, max=12, permanent=0),
    )


def _downed_core() -> CreatureCore:
    return CreatureCore(
        name="Hero",
        description="brave",
        personality="determined",
    )


# ---------------------------------------------------------------------------
# Parametrize helpers
# ---------------------------------------------------------------------------

# Plain params for wwn and cwn; awn is xfail(strict=True) because it
# currently inherits CwnRulesetModule verbatim and emits cwn.* spans.
_AWN_XFAIL = pytest.mark.xfail(
    reason=(
        "awn.* lethality spans land in Task 6 (ADR-142 DD-4); "
        "current code emits cwn.* because AwnRulesetModule subclasses "
        "CwnRulesetModule with no overrides"
    ),
    strict=True,
)

_PARAMS = [
    ("wwn", "wwn"),
    ("cwn", "cwn"),
    pytest.param("awn", "awn", marks=_AWN_XFAIL),
]


# ---------------------------------------------------------------------------
# Event 1 — trauma.roll
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug,expected_prefix", _PARAMS)
def test_trauma_roll_span_prefix(slug: str, expected_prefix: str) -> None:
    """resolve_trauma emits {expected_prefix}.trauma.roll and NO other prefix.

    Forces a traumatic roll: random.Random(42).randint(1,6) == 6 which meets
    the default trauma_target=6, so the span fires with traumatic=True.
    """
    mod = get_ruleset_module(slug)
    spec = DamageSpec(dice="2d6", trauma_die="1d6", trauma_rating=2)
    rng = random.Random(42)  # randint(1,6) → 6 (meets target 6 → traumatic)
    exporter, tracer = _exporter()

    mod.resolve_trauma(
        spec=spec,
        base_total=8,
        cfg=_CFG[slug],
        rng=rng,
        actor="Mook",
        _tracer=tracer,
    )

    span_names = [s.name for s in exporter.get_finished_spans()]
    expected = f"{expected_prefix}.trauma.roll"
    assert expected in span_names, f"Expected span {expected!r} for slug={slug!r}; got {span_names}"
    # No other *.trauma.roll span with a different prefix must be emitted.
    wrong_prefix = [n for n in span_names if n.endswith(".trauma.roll") and n != expected]
    assert not wrong_prefix, (
        f"Unexpected trauma.roll spans with wrong prefix for slug={slug!r}: {wrong_prefix}"
    )


# ---------------------------------------------------------------------------
# Event 2 — system_strain.delta
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug,expected_prefix", _PARAMS)
def test_system_strain_delta_span_prefix(slug: str, expected_prefix: str) -> None:
    """apply_system_strain kind='temporary' emits {expected_prefix}.system_strain.delta.

    Core starts at current=2, max=12; adding 3 gives current=5 which is < max,
    so the strain is applied (applied=True) and the span fires unconditionally.
    """
    mod = get_ruleset_module(slug)
    core = _strain_core()
    exporter, tracer = _exporter()

    mod.apply_system_strain(
        core=core,
        kind="temporary",
        amount=3,
        source="test_drug",
        cfg=_CFG[slug],
        _tracer=tracer,
    )

    span_names = [s.name for s in exporter.get_finished_spans()]
    expected = f"{expected_prefix}.system_strain.delta"
    assert expected in span_names, f"Expected span {expected!r} for slug={slug!r}; got {span_names}"
    wrong_prefix = [n for n in span_names if n.endswith(".system_strain.delta") and n != expected]
    assert not wrong_prefix, (
        f"Unexpected system_strain.delta spans with wrong prefix for slug={slug!r}: {wrong_prefix}"
    )


# ---------------------------------------------------------------------------
# Event 3 — shock.applied
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug,expected_prefix", _PARAMS)
def test_shock_applied_span_prefix(slug: str, expected_prefix: str) -> None:
    """resolve_shock emits {expected_prefix}.shock.applied when the weapon chips.

    Condition: spec.shock > 0 AND target_melee_ac <= spec.shock_ac.
    Here: shock=4, shock_ac=15, target_melee_ac=12 → 12 <= 15 → span fires.
    """
    mod = get_ruleset_module(slug)
    spec = DamageSpec(dice="1d6", shock=4, shock_ac=15)
    exporter, tracer = _exporter()

    mod.resolve_shock(
        spec=spec,
        target_melee_ac=12,  # 12 <= shock_ac=15 → chip applies
        actor="Mook",
        _tracer=tracer,
    )

    span_names = [s.name for s in exporter.get_finished_spans()]
    expected = f"{expected_prefix}.shock.applied"
    assert expected in span_names, f"Expected span {expected!r} for slug={slug!r}; got {span_names}"
    wrong_prefix = [n for n in span_names if n.endswith(".shock.applied") and n != expected]
    assert not wrong_prefix, (
        f"Unexpected shock.applied spans with wrong prefix for slug={slug!r}: {wrong_prefix}"
    )


# ---------------------------------------------------------------------------
# Event 4 — mortal_injury.declared + major_injury.roll
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug,expected_prefix", _PARAMS)
def test_mortal_and_major_injury_span_prefix(slug: str, expected_prefix: str) -> None:
    """resolve_downed with scene_traumatic=True and a save-failing seed emits both spans.

    Strategy: save_target=15, randint(1,20) must return < 15 to fail the save.
    random.Random(1).randint(1,20) == 5 which is < 15 → save fails → both
    mortal_injury.declared and major_injury.roll must fire.

    The major_injury.roll span fires even when save_made=True (always emitted
    when scene_traumatic=True per the resolve_downed implementation), but the
    major_injury ROLL is what we care about here. We use a seed that fails.
    """
    mod = get_ruleset_module(slug)
    core = _downed_core()

    # Verify seed: Random(1).randint(1,20) must be < 15 (save fails).
    _verify_rng = random.Random(1)
    _save_roll = _verify_rng.randint(1, 20)
    assert _save_roll < 15, (
        f"Seed invariant broken: expected randint(1,20) < 15 for save failure, got {_save_roll}"
    )

    exporter, tracer = _exporter()

    mod.resolve_downed(
        core=core,
        save_target=15,
        scene_traumatic=True,  # triggers the save + major_injury path
        cfg=_CFG[slug],
        rng=random.Random(1),
        _tracer=tracer,
    )

    span_names = [s.name for s in exporter.get_finished_spans()]

    expected_mortal = f"{expected_prefix}.mortal_injury.declared"
    expected_major = f"{expected_prefix}.major_injury.roll"

    assert expected_mortal in span_names, (
        f"Expected span {expected_mortal!r} for slug={slug!r}; got {span_names}"
    )
    assert expected_major in span_names, (
        f"Expected span {expected_major!r} for slug={slug!r}; got {span_names}"
    )

    # No other *.mortal_injury.declared or *.major_injury.roll with a different prefix.
    wrong_mortal = [
        n for n in span_names if n.endswith(".mortal_injury.declared") and n != expected_mortal
    ]
    wrong_major = [
        n for n in span_names if n.endswith(".major_injury.roll") and n != expected_major
    ]
    assert not wrong_mortal, (
        f"Unexpected mortal_injury.declared spans with wrong prefix for slug={slug!r}: {wrong_mortal}"
    )
    assert not wrong_major, (
        f"Unexpected major_injury.roll spans with wrong prefix for slug={slug!r}: {wrong_major}"
    )
