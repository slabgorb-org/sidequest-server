from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.creature_core import CreatureCore
from sidequest.game.ruleset.cwn import CwnRulesetModule
from sidequest.game.system_strain import SystemStrainPool
from sidequest.genre.models.rules import CwnConfig

_AMAP = {
    "STRENGTH": "Brawn", "DEXTERITY": "Reflex", "CONSTITUTION": "Body",
    "INTELLIGENCE": "Tech", "WISDOM": "Instinct", "CHARISMA": "Cool",
}
_CFG = CwnConfig(attribute_map=_AMAP)
_MOD = CwnRulesetModule()


def _core(current=0, max=12, permanent=0) -> CreatureCore:
    return CreatureCore(
        name="Jax", description="runner", personality="cool",
        system_strain=SystemStrainPool(current=current, max=max, permanent=permanent),
    )


def _exporter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


def test_temporary_within_max_applies():
    core = _core(current=2, max=12)
    r = _MOD.apply_system_strain(core=core, kind="temporary", amount=3, source="adrenal_boost", cfg=_CFG)
    assert r.applied is True
    assert r.current == 5
    assert r.delta == 3
    assert core.system_strain.current == 5


def test_temporary_over_max_is_refused():
    core = _core(current=10, max=12)
    r = _MOD.apply_system_strain(core=core, kind="temporary", amount=5, source="overclock", cfg=_CFG)
    assert r.applied is False
    assert r.delta == 0
    assert r.current == 10
    assert core.system_strain.current == 10
    assert "max" in r.reason


def test_permanent_raises_floor_and_current():
    core = _core(current=1, max=12, permanent=0)
    r = _MOD.apply_system_strain(core=core, kind="permanent", amount=2, source="cyberarm", cfg=_CFG)
    assert r.applied is True
    assert core.system_strain.permanent == 2
    assert core.system_strain.current == 3


def test_permanent_install_over_max_is_refused():
    core = _core(current=11, max=12, permanent=4)
    r = _MOD.apply_system_strain(core=core, kind="permanent", amount=3, source="reflex_wires", cfg=_CFG)
    assert r.applied is False
    assert core.system_strain.permanent == 4
    assert core.system_strain.current == 11


def test_permanent_removal_lowers_floor_and_clamps_current():
    core = _core(current=5, max=12, permanent=5)
    r = _MOD.apply_system_strain(core=core, kind="permanent", amount=-2, source="explant", cfg=_CFG)
    assert r.applied is True
    assert core.system_strain.permanent == 3
    assert core.system_strain.current == 3


def test_rest_recovers_down_to_permanent_floor():
    core = _core(current=6, max=12, permanent=2)
    r = _MOD.apply_system_strain(core=core, kind="rest", amount=1, source="night_rest", cfg=_CFG)
    assert r.applied is True
    assert core.system_strain.current == 5
    r2 = _MOD.apply_system_strain(core=core, kind="rest", amount=10, source="long_rest", cfg=_CFG)
    assert core.system_strain.current == 2


def test_first_aid_uses_config_cost():
    core = _core(current=0, max=12)
    r = _MOD.apply_system_strain(core=core, kind="first_aid", amount=99, source="medkit", cfg=_CFG)
    assert r.applied is True
    assert r.delta == 1
    assert core.system_strain.current == 1


def test_missing_strain_pool_fails_loud():
    core = CreatureCore(name="Jax", description="x", personality="y")
    with pytest.raises(ValueError, match="system_strain"):
        _MOD.apply_system_strain(core=core, kind="temporary", amount=1, source="x", cfg=_CFG)


def test_unknown_kind_fails_loud():
    core = _core()
    with pytest.raises(ValueError, match="kind"):
        _MOD.apply_system_strain(core=core, kind="bogus", amount=1, source="x", cfg=_CFG)


def test_emits_otel_on_apply_and_on_refusal():
    exporter, tracer = _exporter()
    core = _core(current=10, max=12)
    _MOD.apply_system_strain(core=core, kind="temporary", amount=1, source="ok", cfg=_CFG, _tracer=tracer)
    _MOD.apply_system_strain(core=core, kind="temporary", amount=9, source="too_much", cfg=_CFG, _tracer=tracer)
    spans = exporter.get_finished_spans()
    assert [s.name for s in spans] == ["cwn.system_strain.delta", "cwn.system_strain.delta"]
    applied_flags = [dict(s.attributes or {})["applied"] for s in spans]
    assert applied_flags == [True, False]
