from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import ValidationError

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
    # chips 2 (shock), AC ceiling 15 (shock_ac) — the two numbers are decoupled.
    spec = DamageSpec(dice="1d8", shock=2, shock_ac=15)
    # target Melee AC 13 <= shock_ac 15 → the AC-13 street mook now takes the chip
    assert _MOD.resolve_shock(spec=spec, target_melee_ac=13) == 2


def test_shock_skipped_when_ac_above_rating():
    spec = DamageSpec(dice="1d8", shock=2, shock_ac=12)
    # 15 > shock_ac 12 → skipped
    assert _MOD.resolve_shock(spec=spec, target_melee_ac=15) == 0


def test_shock_emits_span_only_when_applied():
    spec = DamageSpec(dice="1d8", shock=3, shock_ac=10)
    exporter, tracer = _exporter()
    # 8 <= shock_ac 10 → applies, span fires
    _MOD.resolve_shock(spec=spec, target_melee_ac=8, actor="Mook", _tracer=tracer)
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "cwn.shock.applied"
    # 12 > shock_ac 10 → skipped, emits nothing
    _MOD.resolve_shock(spec=spec, target_melee_ac=12, actor="Mook", _tracer=tracer)
    assert len(exporter.get_finished_spans()) == 1


def test_shock_without_ceiling_fails_loud():
    # No Silent Fallbacks: shock>0 with no shock_ac is a content error.
    with pytest.raises(ValidationError):
        DamageSpec(dice="1d8", shock=2)


def test_shock_span_carries_ceiling():
    spec = DamageSpec(dice="1d8", shock=2, shock_ac=15)
    exporter, tracer = _exporter()
    _MOD.resolve_shock(spec=spec, target_melee_ac=13, actor="Mook", _tracer=tracer)
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert dict(spans[0].attributes or {})["shock_ac"] == 15
