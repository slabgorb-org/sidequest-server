"""CWN system-strain span — driven through the real ruleset method (ADR-142).

Migrated off the deleted ``cwn_system_strain_delta_span`` per-slug emitter: the
WN lethality stack now emits ``{slug}.system_strain.delta`` from the core's
slug-parameterized ``system_strain_delta_span(ruleset=...)`` (spans/wn.py). This
test drives ``get_ruleset_module("cwn").apply_system_strain(...)`` with an
in-memory exporter and pins the routed span name + attribute VALUES the old
emitter test asserted, so we keep the value coverage the slug-name net in
``tests/game/ruleset/test_142_wn_lethality_spans.py`` does not check.
"""

from __future__ import annotations

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.creature_core import CreatureCore
from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.system_strain import SystemStrainPool
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


def _exporter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


def test_strain_span_is_routed():
    assert "cwn.system_strain.delta" in SPAN_ROUTES
    assert SPAN_ROUTES["cwn.system_strain.delta"].component == "cwn"


def test_strain_span_emits_attributes():
    exporter, tracer = _exporter()
    core = CreatureCore(
        name="Jax",
        description="runner",
        personality="cool",
        system_strain=SystemStrainPool(current=0, max=14, permanent=0),
    )

    # actor on the span is derived from core.name ("Jax").
    get_ruleset_module("cwn").apply_system_strain(
        core=core,
        kind="temporary",
        amount=3,
        source="cyberarm_install",
        cfg=CwnConfig(attribute_map=_AMAP),
        _tracer=tracer,
    )

    spans = [s for s in exporter.get_finished_spans() if s.name == "cwn.system_strain.delta"]
    assert len(spans) == 1
    attrs = dict(spans[0].attributes or {})
    assert attrs["actor"] == "Jax"
    assert attrs["source"] == "cyberarm_install"
    assert attrs["amount"] == 3
    assert attrs["new_total"] == 3
    assert attrs["max"] == 14
    assert attrs["applied"] is True
