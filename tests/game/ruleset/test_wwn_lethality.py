from __future__ import annotations

import random

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.creature_core import CreatureCore
from sidequest.game.ruleset.wwn import WwnRulesetModule
from sidequest.game.system_strain import SystemStrainPool
from sidequest.genre.models.inventory import DamageSpec
from sidequest.genre.models.rules import WwnConfig

_EH_AMAP = {
    "STRENGTH": "Strength",
    "DEXTERITY": "Agility",
    "CONSTITUTION": "Endurance",
    "INTELLIGENCE": "Insight",
    "WISDOM": "Spirit",
    "CHARISMA": "Harmony",
}
_W = WwnRulesetModule()


def _core(current=0, max=12, permanent=0) -> CreatureCore:
    return CreatureCore(
        name="Lin",
        description="wanderer",
        personality="stoic",
        system_strain=SystemStrainPool(current=current, max=max, permanent=permanent),
    )


def _exporter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


def test_wwn_shock_chips_on_low_ac_target():
    spec = DamageSpec(dice="1d6", shock=2, shock_ac=15)
    assert _W.resolve_shock(spec=spec, target_melee_ac=13, actor="Lin") == 2


def test_wwn_shock_skips_high_ac_target():
    spec = DamageSpec(dice="1d6", shock=2, shock_ac=15)
    assert _W.resolve_shock(spec=spec, target_melee_ac=16, actor="Lin") == 0


def test_wwn_shock_emits_span_only_when_applied():
    spec = DamageSpec(dice="1d6", shock=2, shock_ac=15)
    exporter, tracer = _exporter()
    # 13 <= shock_ac 15 → chips, span fires
    assert _W.resolve_shock(spec=spec, target_melee_ac=13, actor="Lin", _tracer=tracer) == 2
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "wwn.shock.applied"
    assert dict(spans[0].attributes or {})["shock_ac"] == 15
    # 16 > shock_ac 15 → skipped, emits nothing
    assert _W.resolve_shock(spec=spec, target_melee_ac=16, actor="Lin", _tracer=tracer) == 0
    assert len(exporter.get_finished_spans()) == 1


def test_wwn_trauma_multiplies_on_threshold():
    cfg = WwnConfig(attribute_map=_EH_AMAP)
    spec = DamageSpec(dice="1d8", trauma_die="1d6", trauma_rating=3, trauma_target=2)
    rng = random.Random()
    rng.randint = lambda a, b: 4  # 1d6 trauma roll = 4 >= target 2 -> traumatic
    result = _W.resolve_trauma(spec=spec, base_total=5, cfg=cfg, rng=rng, actor="Lin")
    assert result.traumatic is True
    assert result.final_total == 15  # 5 * 3


def test_wwn_trauma_identity_without_die():
    cfg = WwnConfig(attribute_map=_EH_AMAP)
    spec = DamageSpec(dice="1d8")  # no trauma_die
    result = _W.resolve_trauma(spec=spec, base_total=5, cfg=cfg, rng=random.Random(1))
    assert result.traumatic is False
    assert result.final_total == 5


def test_wwn_trauma_emits_span():
    cfg = WwnConfig(attribute_map=_EH_AMAP)
    spec = DamageSpec(dice="1d8", trauma_die="1d6", trauma_rating=3, trauma_target=2)
    rng = random.Random()
    rng.randint = lambda a, b: 4  # 1d6 trauma roll = 4 >= target 2 -> traumatic
    exporter, tracer = _exporter()
    _W.resolve_trauma(spec=spec, base_total=5, cfg=cfg, rng=rng, actor="Lin", _tracer=tracer)
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "wwn.trauma.roll"
    attrs = dict(spans[0].attributes or {})
    assert attrs["traumatic"] is True
    assert attrs["target"] == 2
    assert attrs["final"] == 15


def test_wwn_system_strain_applies_and_emits_span():
    cfg = WwnConfig(attribute_map=_EH_AMAP)
    core = _core(current=2, max=12)
    exporter, tracer = _exporter()
    r = _W.apply_system_strain(
        core=core, kind="temporary", amount=3, source="exertion", cfg=cfg, _tracer=tracer
    )
    assert r.applied is True
    assert core.system_strain.current == 5
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "wwn.system_strain.delta"
    attrs = dict(spans[0].attributes or {})
    assert attrs["new_total"] == 5
    assert attrs["applied"] is True


def test_wwn_downed_declares_mortal_injury():
    cfg = WwnConfig(attribute_map=_EH_AMAP)
    core = _core()
    result = _W.resolve_downed(
        core=core, save_target=15, scene_traumatic=False, cfg=cfg, rng=random.Random(1)
    )
    assert result.mortal is True
    assert any("Mortal Injury" in s.text for s in core.statuses)


def test_wwn_downed_emits_mortal_injury_span():
    cfg = WwnConfig(attribute_map=_EH_AMAP)
    core = _core()
    exporter, tracer = _exporter()
    _W.resolve_downed(
        core=core,
        save_target=15,
        scene_traumatic=False,
        cfg=cfg,
        rng=random.Random(1),
        _tracer=tracer,
    )
    names = [s.name for s in exporter.get_finished_spans()]
    assert "wwn.mortal_injury.declared" in names
    # No traumatic hit this scene → no major-injury roll span.
    assert "wwn.major_injury.roll" not in names


def test_wwn_traumatic_downed_rolls_major_and_emits_spans():
    cfg = WwnConfig(attribute_map=_EH_AMAP)
    core = _core()
    seq = iter([1, 9])  # d20 save = 1 (fails target 15); then d12 major roll = 9
    rng = random.Random()
    rng.randint = lambda a, b: next(seq)
    exporter, tracer = _exporter()
    result = _W.resolve_downed(
        core=core, save_target=15, scene_traumatic=True, cfg=cfg, rng=rng, _tracer=tracer
    )
    assert result.mortal is True
    assert result.major is True
    assert result.save_made is False
    assert result.major_roll == 9
    assert any("Major Injury" in s.text for s in core.statuses)
    names = [s.name for s in exporter.get_finished_spans()]
    assert "wwn.mortal_injury.declared" in names
    assert "wwn.major_injury.roll" in names


def test_wwn_downed_non_wwn_cfg_fails_loud():
    core = _core()
    with pytest.raises(ValueError):
        _W.resolve_downed(
            core=core, save_target=15, scene_traumatic=False, cfg=None, rng=random.Random(1)
        )
