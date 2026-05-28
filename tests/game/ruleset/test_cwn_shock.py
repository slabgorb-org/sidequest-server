from __future__ import annotations

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.ruleset.cwn import CwnRulesetModule
from sidequest.genre.models.inventory import DamageSpec

_MOD = CwnRulesetModule()


def _exporter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


def test_no_shock_field_is_zero():
    spec = DamageSpec(dice="1d6")  # shock defaults 0
    assert _MOD.resolve_shock(spec=spec, target_melee_ac=8) == 0


def test_shock_applies_when_ac_at_or_below_rating():
    spec = DamageSpec(dice="1d8", shock=2)  # chips 2, AC ceiling 2 (ceiling == shock)
    # target AC 2 <= shock 2 → applies
    assert _MOD.resolve_shock(spec=spec, target_melee_ac=2) == 2


def test_shock_skipped_when_ac_above_rating():
    spec = DamageSpec(dice="1d8", shock=2)
    assert _MOD.resolve_shock(spec=spec, target_melee_ac=5) == 0


def test_shock_emits_span_only_when_applied():
    spec = DamageSpec(dice="1d8", shock=3)
    exporter, tracer = _exporter()
    _MOD.resolve_shock(spec=spec, target_melee_ac=3, actor="Mook", _tracer=tracer)
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "cwn.shock.applied"
    # a skipped shock emits nothing
    _MOD.resolve_shock(spec=spec, target_melee_ac=9, actor="Mook", _tracer=tracer)
    assert len(exporter.get_finished_spans()) == 1
