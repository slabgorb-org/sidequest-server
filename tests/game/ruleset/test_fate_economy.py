from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.fate_sheet import Aspect, FateSheet
from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.ruleset.fate import FateEconomyError


def _exporter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


def _names(exporter):
    return [s.name for s in exporter.get_finished_spans()]


def test_spend_decrements_and_emits_delta_span():
    module = get_ruleset_module("fate")  # production resolution path
    sheet = FateSheet(fate_points=3)
    exporter, tracer = _exporter()

    after = module.spend_fate_point(sheet=sheet, reason="invoke", actor="Sleuth", _tracer=tracer)

    assert after == 2
    assert sheet.fate_points == 2
    assert "fate.fate_point.delta" in _names(exporter)
    span = next(s for s in exporter.get_finished_spans() if s.name == "fate.fate_point.delta")
    assert span.attributes["reason"] == "invoke"
    assert span.attributes["before"] == 3
    assert span.attributes["after"] == 2


def test_spend_with_zero_points_fails_loud():
    module = get_ruleset_module("fate")
    sheet = FateSheet(fate_points=0)
    with pytest.raises(FateEconomyError):
        module.spend_fate_point(sheet=sheet, reason="invoke", actor="Sleuth")


def test_earn_increments_and_emits_delta_span():
    module = get_ruleset_module("fate")
    sheet = FateSheet(fate_points=1)
    exporter, tracer = _exporter()

    after = module.earn_fate_point(sheet=sheet, reason="concede", actor="Sleuth", _tracer=tracer)

    assert after == 2
    assert "fate.fate_point.delta" in _names(exporter)


def test_refresh_raises_to_refresh_value_not_lowers():
    module = get_ruleset_module("fate")
    # Below refresh → rises to refresh.
    low = FateSheet(refresh=3, fate_points=1)
    assert module.refresh_fate_points(sheet=low, actor="Sleuth") == 3
    assert low.fate_points == 3
    # Above refresh (banked from compels) → keeps the higher total (SRD: refresh
    # never reduces fate points).
    high = FateSheet(refresh=3, fate_points=5)
    assert module.refresh_fate_points(sheet=high, actor="Sleuth") == 5


def test_invoke_uses_free_invocation_before_spending():
    module = get_ruleset_module("fate")
    sheet = FateSheet(
        fate_points=2,
        aspects=[Aspect(text="High Ground", kind="situation", free_invokes=1)],
    )
    exporter, tracer = _exporter()

    bonus = module.invoke_aspect(
        sheet=sheet, aspect_text="High Ground", mode="bonus", actor="Sleuth", _tracer=tracer
    )

    assert bonus == 2  # +2 for a bonus invoke
    assert sheet.fate_points == 2  # free invoke spent no fate point
    assert sheet.aspects[0].free_invokes == 0  # the free invoke was consumed
    span = next(s for s in exporter.get_finished_spans() if s.name == "fate.aspect.invoked")
    assert span.attributes["free"] is True


def test_invoke_without_free_invocation_spends_a_fate_point():
    module = get_ruleset_module("fate")
    sheet = FateSheet(
        fate_points=2,
        aspects=[Aspect(text="Dogged", kind="character", free_invokes=0)],
    )
    exporter, tracer = _exporter()

    bonus = module.invoke_aspect(
        sheet=sheet, aspect_text="Dogged", mode="bonus", actor="Sleuth", _tracer=tracer
    )

    assert bonus == 2
    assert sheet.fate_points == 1  # paid one fate point
    # Paid-invoke lie-detector path: both spans fire with the paid signature.
    names = _names(exporter)
    assert "fate.aspect.invoked" in names
    assert "fate.fate_point.delta" in names  # the fate-point spend emits a delta
    invoked = next(s for s in exporter.get_finished_spans() if s.name == "fate.aspect.invoked")
    assert invoked.attributes["free"] is False
    assert invoked.attributes["fate_points_after"] == 1


def test_invoke_unknown_mode_fails_loud():
    module = get_ruleset_module("fate")
    sheet = FateSheet(
        fate_points=2,
        aspects=[Aspect(text="High Ground", kind="situation", free_invokes=1)],
    )
    with pytest.raises(FateEconomyError):
        module.invoke_aspect(
            sheet=sheet, aspect_text="High Ground", mode="sideways", actor="Sleuth"
        )
    # Validate-before-mutate: an invalid mode burns nothing.
    assert sheet.fate_points == 2
    assert sheet.aspects[0].free_invokes == 1


def test_invoke_reroll_returns_zero():
    module = get_ruleset_module("fate")
    sheet = FateSheet(
        fate_points=2,
        aspects=[Aspect(text="Dogged", kind="character", free_invokes=0)],
    )
    bonus = module.invoke_aspect(sheet=sheet, aspect_text="Dogged", mode="reroll", actor="Sleuth")
    assert bonus == 0  # the reroll itself is the caller's job (F1c)
    assert sheet.fate_points == 1  # the economy effect still happened — spent the point


def test_offer_compel_emits_span():
    module = get_ruleset_module("fate")
    exporter, tracer = _exporter()

    module.offer_compel(aspect_text="Owes the Mob a Favor", actor="Sleuth", _tracer=tracer)

    assert "fate.compel.offered" in _names(exporter)
    span = next(s for s in exporter.get_finished_spans() if s.name == "fate.compel.offered")
    assert span.attributes["actor"] == "Sleuth"
    assert span.attributes["aspect"] == "Owes the Mob a Favor"


def test_invoke_unknown_aspect_fails_loud():
    module = get_ruleset_module("fate")
    sheet = FateSheet(fate_points=2)
    with pytest.raises(FateEconomyError):
        module.invoke_aspect(sheet=sheet, aspect_text="Nonexistent", mode="bonus", actor="x")


def test_accept_compel_earns_a_point_and_emits_both_spans():
    module = get_ruleset_module("fate")
    sheet = FateSheet(fate_points=1)
    exporter, tracer = _exporter()

    after = module.accept_compel(
        sheet=sheet, aspect_text="Owes the Mob a Favor", actor="Sleuth", _tracer=tracer
    )

    assert after == 2
    names = _names(exporter)
    assert "fate.compel.accepted" in names
    assert "fate.fate_point.delta" in names  # the earn rides the delta span too


def test_mark_stress_checks_the_named_box_and_emits_span():
    module = get_ruleset_module("fate")
    sheet = FateSheet()
    exporter, tracer = _exporter()

    absorbed = module.mark_stress(
        sheet=sheet, track="physical", box_value=2, actor="Sleuth", _tracer=tracer
    )

    assert absorbed == 2  # a value-2 box absorbs 2 shifts
    assert sheet.stress["physical"].boxes[1].checked is True
    assert sheet.stress["physical"].boxes[0].checked is False
    assert "fate.stress.applied" in _names(exporter)
    span = next(s for s in exporter.get_finished_spans() if s.name == "fate.stress.applied")
    assert span.attributes["track"] == "physical"
    assert span.attributes["box_value"] == 2


def test_mark_stress_already_checked_fails_loud():
    module = get_ruleset_module("fate")
    sheet = FateSheet()
    module.mark_stress(sheet=sheet, track="physical", box_value=1, actor="Sleuth")
    with pytest.raises(FateEconomyError):
        module.mark_stress(sheet=sheet, track="physical", box_value=1, actor="Sleuth")


def test_mark_stress_unknown_track_or_box_fails_loud():
    module = get_ruleset_module("fate")
    sheet = FateSheet()
    with pytest.raises(FateEconomyError):
        module.mark_stress(sheet=sheet, track="spiritual", box_value=1, actor="x")
    with pytest.raises(FateEconomyError):
        module.mark_stress(sheet=sheet, track="physical", box_value=9, actor="x")


def test_take_consequence_fills_slot_becomes_aspect_with_free_invoke():
    module = get_ruleset_module("fate")
    sheet = FateSheet()
    exporter, tracer = _exporter()

    absorbed = module.take_consequence(
        sheet=sheet,
        level="moderate",
        aspect_text="Dislocated Shoulder",
        actor="Sleuth",
        _tracer=tracer,
    )

    assert absorbed == 4  # moderate absorbs 4 shifts
    moderate = next(c for c in sheet.consequences if c.level == "moderate")
    assert moderate.aspect is not None
    assert moderate.aspect.text == "Dislocated Shoulder"
    assert moderate.aspect.kind == "consequence"
    assert moderate.aspect.free_invokes == 1  # SRD: free invoke for the attacker
    # The filled consequence now surfaces in all_aspects().
    assert "Dislocated Shoulder" in [a.text for a in sheet.all_aspects()]
    assert "fate.consequence.taken" in _names(exporter)
    span = next(s for s in exporter.get_finished_spans() if s.name == "fate.consequence.taken")
    assert span.attributes["level"] == "moderate"
    assert span.attributes["aspect"] == "Dislocated Shoulder"


def test_take_consequence_already_filled_fails_loud():
    module = get_ruleset_module("fate")
    sheet = FateSheet()
    module.take_consequence(sheet=sheet, level="mild", aspect_text="Bruised", actor="x")
    with pytest.raises(FateEconomyError):
        module.take_consequence(sheet=sheet, level="mild", aspect_text="Again", actor="x")
