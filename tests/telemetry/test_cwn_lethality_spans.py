"""CWN lethality spans — driven through the real ruleset methods (ADR-142).

Migrated off the deleted per-slug ``cwn_*`` emitters: the WN lethality stack now
emits ``cwn.{trauma.roll,shock.applied,mortal_injury.declared,major_injury.roll}``
from the core's slug-parameterized emitters (spans/wn.py). This test drives the
real ``get_ruleset_module("cwn")`` methods with an in-memory exporter and pins the
routed span names + the attribute VALUES the old emitter tests asserted (traumatic,
final, shock_ac, roll) — coverage the slug-name net in
``tests/game/ruleset/test_142_wn_lethality_spans.py`` does not check.
"""

from __future__ import annotations

import random

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.creature_core import CreatureCore
from sidequest.game.ruleset import get_ruleset_module
from sidequest.genre.models.inventory import DamageSpec
from sidequest.genre.models.rules import CwnConfig
from sidequest.telemetry.spans._core import SPAN_ROUTES

_AMAP = {
    "STRENGTH": "Brawn",
    "CONSTITUTION": "Body",
    "DEXTERITY": "Reflex",
    "INTELLIGENCE": "Tech",
    "WISDOM": "Instinct",
    "CHARISMA": "Cool",
}
_CFG = CwnConfig(attribute_map=_AMAP)


def _exporter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


def _named(exporter, name: str):
    return [s for s in exporter.get_finished_spans() if s.name == name]


def test_all_four_spans_are_routed():
    for name in (
        "cwn.trauma.roll",
        "cwn.shock.applied",
        "cwn.mortal_injury.declared",
        "cwn.major_injury.roll",
    ):
        assert name in SPAN_ROUTES
        assert SPAN_ROUTES[name].component == "cwn"


def test_trauma_span_emits():
    exporter, tracer = _exporter()
    # Random(42).randint(1,6) == 6 meets the default trauma_target=6 → traumatic.
    get_ruleset_module("cwn").resolve_trauma(
        spec=DamageSpec(dice="2d6", trauma_die="1d6", trauma_rating=3),
        base_total=7,
        cfg=_CFG,
        rng=random.Random(42),
        actor="Mook",
        _tracer=tracer,
    )
    spans = _named(exporter, "cwn.trauma.roll")
    assert len(spans) == 1
    attrs = dict(spans[0].attributes or {})
    assert attrs["traumatic"] is True
    assert attrs["final"] == 21  # base 7 * rating 3


def test_shock_span_emits():
    exporter, tracer = _exporter()
    # shock=2, shock_ac=15, target_melee_ac=8 → 8 <= 15 → chip applies.
    get_ruleset_module("cwn").resolve_shock(
        spec=DamageSpec(dice="1d6", shock=2, shock_ac=15),
        target_melee_ac=8,
        actor="Mook",
        _tracer=tracer,
    )
    spans = _named(exporter, "cwn.shock.applied")
    assert len(spans) == 1
    assert dict(spans[0].attributes or {})["shock_ac"] == 15


def test_mortal_and_major_span_emit():
    exporter, tracer = _exporter()
    core = CreatureCore(name="Jax", description="brave", personality="grim")
    # Random(1).randint(1,20) == 5 < 15 → save fails → both spans fire.
    get_ruleset_module("cwn").resolve_downed(
        core=core,
        save_target=15,
        scene_traumatic=True,
        cfg=_CFG,
        rng=random.Random(1),
        _tracer=tracer,
    )
    assert _named(exporter, "cwn.mortal_injury.declared")
    major = _named(exporter, "cwn.major_injury.roll")
    assert len(major) == 1
    # rng order: save roll (5, fails the DC-15 save) is consumed first, then the
    # major-injury TABLE roll (10) — the table roll is what major_injury.roll reports.
    assert dict(major[0].attributes or {})["roll"] == 10
    assert dict(major[0].attributes or {})["save_made"] is False
