from __future__ import annotations

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.ruleset.cwn import CwnRulesetModule
from sidequest.game.ruleset.swn import SwnRulesetModule

_CWN = CwnRulesetModule()
_SWN = SwnRulesetModule()


def _exporter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


def test_resolve_hacking_returns_base_plus_alert():
    dc = _CWN.resolve_hacking(
        verb="Run Program", tier="office", base_dc=9, alert_modifier=0, outcome="Success"
    )
    assert dc == 9


def test_alert_modifier_raises_effective_dc():
    # office=9, +2 alert escalation → effective DC 11.
    dc = _CWN.resolve_hacking(
        verb="Run Program", tier="office", base_dc=9, alert_modifier=2, outcome="Fail"
    )
    assert dc == 11


def test_resolve_hacking_emits_span_with_attrs():
    exporter, tracer = _exporter()
    _CWN.resolve_hacking(
        verb="Spoof",
        tier="black_site",
        base_dc=12,
        alert_modifier=1,
        outcome="CritSuccess",
        actor="Rux",
        _tracer=tracer,
    )
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "cwn.hacking.security_check"
    attrs = dict(spans[0].attributes or {})
    assert attrs["tier"] == "black_site"
    assert attrs["base_dc"] == 12
    assert attrs["alert_modifier"] == 1
    assert attrs["effective_dc"] == 13
    assert attrs["verb"] == "Spoof"
    assert attrs["result"] == "CritSuccess"


def test_base_swn_resolve_hacking_no_span():
    # native/swn inherit the base no-emit default: compute the DC, fire nothing.
    exporter, tracer = _exporter()
    dc = _SWN.resolve_hacking(
        verb="Run Program",
        tier="office",
        base_dc=9,
        alert_modifier=3,
        outcome="Success",
        actor="X",
        _tracer=tracer,
    )
    assert dc == 12
    assert exporter.get_finished_spans() == ()
