from __future__ import annotations

import random

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.ruleset.cwn import CwnRulesetModule
from sidequest.game.ruleset.native import NativeRulesetModule
from sidequest.game.ruleset.swn import SwnRulesetModule
from sidequest.genre.models.inventory import DamageSpec
from sidequest.genre.models.rules import CwnConfig, TraumaConfig

_AMAP = {
    "STRENGTH": "Brawn", "DEXTERITY": "Reflex", "CONSTITUTION": "Body",
    "INTELLIGENCE": "Tech", "WISDOM": "Instinct", "CHARISMA": "Cool",
}
_CFG = CwnConfig(attribute_map=_AMAP, trauma=TraumaConfig(default_trauma_target=6))
_MOD = CwnRulesetModule()


def _exporter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


def test_base_modules_passthrough_damage():
    spec = DamageSpec(dice="1d6")
    for mod in (NativeRulesetModule(), SwnRulesetModule()):
        r = mod.resolve_trauma(spec=spec, base_total=5, cfg=None, rng=random.Random(1))
        assert r.final_total == 5
        assert r.traumatic is False


def test_no_trauma_die_is_passthrough():
    spec = DamageSpec(dice="1d6")  # no trauma_die
    r = _MOD.resolve_trauma(spec=spec, base_total=5, cfg=_CFG, rng=random.Random(1))
    assert r.final_total == 5
    assert r.traumatic is False


def test_traumatic_hit_multiplies_by_rating():
    spec = DamageSpec(dice="2d6", trauma_die="1d6", trauma_rating=3)
    rng = random.Random()
    rng.randint = lambda a, b: 6  # type: ignore[method-assign]
    r = _MOD.resolve_trauma(spec=spec, base_total=7, cfg=_CFG, rng=rng)
    assert r.traumatic is True
    assert r.trauma_roll == 6
    assert r.trauma_target == 6
    assert r.final_total == 21  # 7 * 3


def test_non_traumatic_roll_below_target():
    spec = DamageSpec(dice="2d6", trauma_die="1d6", trauma_rating=3)
    rng = random.Random()
    rng.randint = lambda a, b: 2  # below target 6
    r = _MOD.resolve_trauma(spec=spec, base_total=7, cfg=_CFG, rng=rng)
    assert r.traumatic is False
    assert r.final_total == 7


def test_weapon_trauma_target_override():
    spec = DamageSpec(dice="1d6", trauma_die="1d6", trauma_rating=2, trauma_target=4)
    rng = random.Random()
    rng.randint = lambda a, b: 4  # meets the weapon's override target 4
    r = _MOD.resolve_trauma(spec=spec, base_total=4, cfg=_CFG, rng=rng)
    assert r.trauma_target == 4
    assert r.traumatic is True
    assert r.final_total == 8


def test_trauma_emits_span():
    spec = DamageSpec(dice="1d6", trauma_die="1d6", trauma_rating=2)
    rng = random.Random()
    rng.randint = lambda a, b: 6
    exporter, tracer = _exporter()
    _MOD.resolve_trauma(spec=spec, base_total=4, cfg=_CFG, rng=rng, actor="Mook", _tracer=tracer)
    spans = exporter.get_finished_spans()
    assert spans[0].name == "cwn.trauma.roll"
    assert dict(spans[0].attributes or {})["traumatic"] is True
