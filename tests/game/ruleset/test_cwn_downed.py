from __future__ import annotations

import random

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.creature_core import CreatureCore
from sidequest.game.ruleset.cwn import CwnRulesetModule
from sidequest.game.status import StatusSeverity
from sidequest.genre.models.rules import CwnConfig, TraumaConfig

_AMAP = {
    "STRENGTH": "Brawn", "DEXTERITY": "Reflex", "CONSTITUTION": "Body",
    "INTELLIGENCE": "Tech", "WISDOM": "Instinct", "CHARISMA": "Cool",
}
_CFG = CwnConfig(attribute_map=_AMAP, trauma=TraumaConfig(mortal_injury_rounds=6))
_MOD = CwnRulesetModule()


def _core() -> CreatureCore:
    return CreatureCore(name="Jax", description="runner", personality="cool")


def _exporter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


def test_non_traumatic_downed_is_mortal_only():
    core = _core()
    r = _MOD.resolve_downed(core=core, save_target=10, scene_traumatic=False,
                            cfg=_CFG, rng=random.Random(1))
    assert r is not None
    assert r.mortal is True
    assert r.major is False
    assert any("Mortal Injury" in s.text for s in core.statuses)
    assert all(s.severity == StatusSeverity.Scar for s in core.statuses if "Mortal" in s.text)


def test_traumatic_downed_save_made_no_major():
    core = _core()
    rng = random.Random()
    rng.randint = lambda a, b: 20  # d20 save roll = 20, beats target
    r = _MOD.resolve_downed(core=core, save_target=10, scene_traumatic=True,
                            cfg=_CFG, rng=rng)
    assert r.mortal is True
    assert r.major is False
    assert r.save_made is True
    assert not any("Major Injury" in s.text for s in core.statuses)


def test_traumatic_downed_save_failed_rolls_major():
    core = _core()
    seq = iter([1, 9])  # d20 save (=1, fails target 10); then d12 major roll (=9)
    rng = random.Random()
    rng.randint = lambda a, b: next(seq)
    r = _MOD.resolve_downed(core=core, save_target=10, scene_traumatic=True,
                            cfg=_CFG, rng=rng)
    assert r.mortal is True
    assert r.major is True
    assert r.save_made is False
    assert r.major_roll == 9
    assert any("Major Injury" in s.text for s in core.statuses)


def test_downed_emits_spans():
    core = _core()
    seq = iter([1, 12])  # fail save, roll 12 (instant death)
    rng = random.Random()
    rng.randint = lambda a, b: next(seq)
    exporter, tracer = _exporter()
    _MOD.resolve_downed(core=core, save_target=10, scene_traumatic=True,
                        cfg=_CFG, rng=rng, _tracer=tracer)
    names = [s.name for s in exporter.get_finished_spans()]
    assert "cwn.mortal_injury.declared" in names
    assert "cwn.major_injury.roll" in names
