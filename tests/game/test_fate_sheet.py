from __future__ import annotations

from sidequest.game.fate_sheet import (
    CONSEQUENCE_VALUES,
    Aspect,
    Consequence,
    FateSheet,
    StressBox,
    StressTrack,
    Stunt,
)


def test_default_sheet_has_srd_baseline():
    sheet = FateSheet()
    # SRD baseline: refresh 3, fate points start at refresh.
    assert sheet.refresh == 3
    assert sheet.fate_points == 3
    # Two stress tracks, each two boxes valued 1 and 2.
    assert [b.value for b in sheet.stress["physical"].boxes] == [1, 2]
    assert [b.value for b in sheet.stress["mental"].boxes] == [1, 2]
    assert all(not b.checked for b in sheet.stress["physical"].boxes)
    # Four consequence slots, SRD shift-values, all open.
    assert [c.level for c in sheet.consequences] == ["mild", "moderate", "severe", "extreme"]
    assert [c.value for c in sheet.consequences] == [2, 4, 6, 8]
    assert all(c.aspect is None for c in sheet.consequences)


def test_consequence_values_table():
    assert CONSEQUENCE_VALUES == {"mild": 2, "moderate": 4, "severe": 6, "extreme": 8}


def test_all_aspects_includes_filled_consequences_only():
    sheet = FateSheet(
        aspects=[
            Aspect(text="Last Honest Cop in Vice", kind="high_concept"),
            Aspect(text="Can't Resist a Sob Story", kind="trouble"),
        ]
    )
    # An open consequence slot contributes no aspect.
    assert [a.text for a in sheet.all_aspects()] == [
        "Last Honest Cop in Vice",
        "Can't Resist a Sob Story",
    ]
    # Fill the mild slot — it now surfaces as an aspect.
    sheet.consequences[0].aspect = Aspect(
        text="Cracked Ribs", kind="consequence", free_invokes=1
    )
    assert [a.text for a in sheet.all_aspects()] == [
        "Last Honest Cop in Vice",
        "Can't Resist a Sob Story",
        "Cracked Ribs",
    ]


def test_models_reject_unknown_fields():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Aspect(text="x", kind="trouble", bogus=1)  # extra=forbid
    with pytest.raises(ValidationError):
        Stunt(name="x", bogus=1)
    with pytest.raises(ValidationError):
        StressBox(value=1, bogus=1)
    with pytest.raises(ValidationError):
        StressTrack(boxes=[], bogus=1)
    with pytest.raises(ValidationError):
        Consequence(level="mild", value=2, bogus=1)
    with pytest.raises(ValidationError):
        FateSheet(bogus=1)
