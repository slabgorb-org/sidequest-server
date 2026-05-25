"""Task 8 — NarrativeSheet exposes raw HP numbers alongside the health band.

ADR-040 (No Raw Stats) is amended by ADR-114 §8: the lethality number (current
HP / max HP) IS shown in the narrative sheet because Sebastien and Jade want
legible lethality and the dice overlay already shows damage rolling. This is the
ONLY raw stat exception — no other numbers are exposed here.
"""

from __future__ import annotations

from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.game.narrative_sheet import NarrativeSheet, build_narrative_sheet


def _core(cur: int, mx: int) -> CreatureCore:
    return CreatureCore(
        name="Pilot",
        description="A grizzled star pilot.",
        personality="Taciturn but reliable.",
        hp=HpPool(current=cur, max=mx, base_max=mx),
    )


def test_sheet_exposes_raw_hp_number_alongside_band():
    """hp_current and hp_max are present; health_band is also present (additive)."""
    sheet = build_narrative_sheet(_core(5, 8))
    assert sheet.hp_current == 5
    assert sheet.hp_max == 8
    # 5/8 = 0.625 → fraction > 0.50 → "wounded"
    assert sheet.health_band == "wounded"


def test_sheet_hp_full_is_unwounded():
    """Full HP → unwounded band."""
    sheet = build_narrative_sheet(_core(8, 8))
    assert sheet.hp_current == 8
    assert sheet.hp_max == 8
    assert sheet.health_band == "unwounded"


def test_sheet_hp_zero_is_down():
    """Zero HP → down band; hp_current is 0, not negative-clamped."""
    sheet = build_narrative_sheet(_core(0, 8))
    assert sheet.hp_current == 0
    assert sheet.hp_max == 8
    assert sheet.health_band == "down"


def test_sheet_hp_low_is_bloodied():
    """2/8 = 0.25 → boundary: fraction <= 0.25 → staggering (>0); verify bloodied threshold."""
    # 3/8 = 0.375 → bloodied (>0.25, <=0.5)
    sheet = build_narrative_sheet(_core(3, 8))
    assert sheet.health_band == "bloodied"


def test_sheet_hp_staggering():
    """1/8 = 0.125 → staggering (>0, <=0.25)."""
    sheet = build_narrative_sheet(_core(1, 8))
    assert sheet.health_band == "staggering"


def test_sheet_result_is_narrative_sheet_instance():
    """Builder returns a NarrativeSheet; model is not a bare dict."""
    sheet = build_narrative_sheet(_core(5, 10))
    assert isinstance(sheet, NarrativeSheet)


def test_health_band_field_is_additive_not_replacement():
    """Both health_band AND hp_current/hp_max are present — band is not replaced."""
    sheet = build_narrative_sheet(_core(6, 8))
    assert hasattr(sheet, "health_band")
    assert hasattr(sheet, "hp_current")
    assert hasattr(sheet, "hp_max")
    # band is non-empty string
    assert isinstance(sheet.health_band, str)
    assert len(sheet.health_band) > 0
