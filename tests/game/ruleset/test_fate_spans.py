"""OTEL wiring net for the Fate module (ADR-144 F1a).

Drives the real registered FateRulesetModule.resolve_action and asserts the
fate.action_resolved span fired with the resolved math. Exporter pattern matches
test_142_wn_lethality_spans.py: a local InMemorySpanExporter passed in as
_tracer so spans land locally regardless of global provider state.
"""

from __future__ import annotations

import random

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.ruleset.fate_resolution import Opposition


def _exporter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


def test_resolve_action_emits_fate_action_resolved_span():
    exporter, tracer = _exporter()
    module = get_ruleset_module("fate")  # production resolution path

    module.resolve_action(
        skill_rating=3,
        opposition=Opposition(value=2, kind="passive"),
        rng=random.Random(1),
        actor="Sleuth",
        _tracer=tracer,
    )

    spans = exporter.get_finished_spans()
    names = [s.name for s in spans]
    assert "fate.action_resolved" in names

    span = next(s for s in spans if s.name == "fate.action_resolved")
    assert span.attributes["actor"] == "Sleuth"
    assert span.attributes["skill_rating"] == 3
    assert span.attributes["opposition"] == 2
    assert span.attributes["opposition_kind"] == "passive"
    # Deterministic seed-1 math (4dF = -1,1,-1,0 → roll_total -1; +skill 3 → ladder 2
    # vs opposition 2 → 0 shifts → Tie). Exact-value asserts so the lie detector
    # catches a miscomputed outcome, not just a present field.
    assert span.attributes["ladder_total"] == 2
    assert span.attributes["shifts"] == 0
    assert span.attributes["tier"] == "Tie"
