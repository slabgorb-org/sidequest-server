"""Story 118-5 (ADR-144 F3e) — the ``refuse_compel`` ruleset primitive (RED).

F2b shipped ``accept_compel`` (earn +1, ``fate.compel.accepted``). The compel
accept/refuse round-trip needs its other half: refusing a compel PAYS one fate
point to decline (Fate SRD) and emits a ``fate.compel.refused`` span so the
GM-panel lie-detector sees the decline, not merely the offer. Refusal at zero
fate points is impossible per the SRD and must FAIL LOUD (No Silent Fallbacks) —
you cannot decline for free, and a rejected refusal must neither drive the
balance negative nor log a phantom decline (validate-before-mutate-before-emit,
the same posture ``invoke_aspect`` already holds).

FAIL today: ``FateRulesetModule`` has no ``refuse_compel`` method and the
``fate.compel.refused`` span does not exist.
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.fate_sheet import FateSheet
from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.ruleset.fate import FateEconomyError


def _otel():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


def _names(exporter):
    return [s.name for s in exporter.get_finished_spans()]


def test_refuse_compel_spends_one_point_and_emits_refused_span():
    module = get_ruleset_module("fate")  # production resolution path
    sheet = FateSheet(fate_points=2)
    exporter, tracer = _otel()

    after = module.refuse_compel(
        sheet=sheet, aspect_text="Owes the Mob a Favor", actor="Sleuth", _tracer=tracer
    )

    assert after == 1, "refusing a compel pays exactly one fate point (SRD)"
    assert sheet.fate_points == 1
    names = _names(exporter)
    assert "fate.compel.refused" in names, "refuse must emit the GM-panel decline span"
    # The spend rides the shared fate-point delta span, exactly as accept's earn does.
    assert "fate.fate_point.delta" in names


def test_refuse_compel_span_carries_actor_and_aspect():
    module = get_ruleset_module("fate")
    sheet = FateSheet(fate_points=1)
    exporter, tracer = _otel()

    module.refuse_compel(
        sheet=sheet, aspect_text="Last Honest Cop in Vega", actor="Vance", _tracer=tracer
    )

    span = next(s for s in exporter.get_finished_spans() if s.name == "fate.compel.refused")
    attrs = dict(span.attributes or {})
    assert attrs["actor"] == "Vance"
    assert attrs["aspect"] == "Last Honest Cop in Vega"


def test_refuse_compel_at_zero_points_raises_and_does_not_emit():
    # Fate SRD: declining a compel costs a fate point. At zero you cannot refuse —
    # fail loud (No Silent Fallbacks). Validate-before-mutate-before-emit, so a
    # rejected refusal neither drives the balance negative nor logs a phantom decline.
    module = get_ruleset_module("fate")
    sheet = FateSheet(fate_points=0)
    exporter, tracer = _otel()

    with pytest.raises(FateEconomyError):
        module.refuse_compel(
            sheet=sheet, aspect_text="Owes the Mob a Favor", actor="Sleuth", _tracer=tracer
        )

    assert sheet.fate_points == 0, "a rejected refusal must not drive fate points negative"
    assert "fate.compel.refused" not in _names(exporter), (
        "no decline span may fire when the refusal was rejected (no Illusionism)"
    )
