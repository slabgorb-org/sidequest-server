"""WWN cast_spell beat-filter arm — TDD for Task 5 (WWN content binding Plan 3).

Gates on ``core.spellcasting`` (SpellcastingState) instead of the B/X
``spell_slots_remaining``/``prepared_spells`` economy.  When
``spellcasting is not None`` the B/X slot gate is completely bypassed.

Acceptance criteria:
1. cast_spell selectable when casts_remaining > 0 and prepared non-empty.
2. casts_remaining == 0 → filtered out; reason "no_slots".
3. prepared == [] → filtered out; reason "unprepared".
4. WWN class that doesn't allow cast_spell → reason "class".
5. Backward-compat: spellcasting=None → B/X behavior unchanged.
"""

from __future__ import annotations

from sidequest.game.beat_filter import beats_available_for, cast_spell_rejection_reason
from sidequest.game.wwn_magic import SpellcastingState
from sidequest.genre.models.character import ClassDef
from sidequest.genre.models.rules import (
    BeatDef,
    BeatKind,
    ConfrontationDef,
    MetricDef,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _beat(id_: str, *, class_filter: list[str] | None = None) -> BeatDef:
    return BeatDef(
        id=id_,
        label=id_,
        kind=BeatKind.strike,
        stat_check="STR",
        class_filter=class_filter,
    )


def _confrontation(beats: list[BeatDef]) -> ConfrontationDef:
    return ConfrontationDef(
        type="combat",
        label="Combat",
        category="combat",
        player_metric=MetricDef(name="m", starting=0, threshold=7),
        opponent_metric=MetricDef(name="m", starting=0, threshold=7),
        beats=beats,
    )


def _wwn_mage() -> ClassDef:
    """A WWN High Mage class that allows cast_spell."""
    return ClassDef(
        id="high_mage",
        display_name="High Mage",
        rpg_role="caster",
        jungian_default="sage",
        prime_requisite="INT",
        minimum_score=9,
        kit_table="high_mage_kit",
        flavor="A scholar of arcane forces.",
        encounter_beat_choices=["cast_spell", "flee"],
    )


def _wwn_warrior() -> ClassDef:
    """A WWN Warrior class that does NOT allow cast_spell."""
    return ClassDef(
        id="warrior",
        display_name="Warrior",
        rpg_role="tank",
        jungian_default="warrior",
        prime_requisite="STR",
        minimum_score=9,
        kit_table="warrior_kit",
        flavor="A blade and board fighter.",
        encounter_beat_choices=["attack", "defend", "flee"],
    )


def _combat_def() -> ConfrontationDef:
    """A combat confrontation with cast_spell restricted to High Mage."""
    return _confrontation(
        [
            _beat("attack"),  # universal (class_filter=None)
            _beat("cast_spell", class_filter=["High Mage"]),
        ]
    )


def _spellcasting(*, casts_remaining: int, prepared: list[str]) -> SpellcastingState:
    return SpellcastingState(
        prepared=prepared,
        casts_remaining=casts_remaining,
        casts_per_day=casts_remaining,
        max_spell_level=1,
    )


# ---------------------------------------------------------------------------
# AC1 — cast_spell selectable when casts_remaining > 0 and prepared non-empty
# ---------------------------------------------------------------------------


def test_wwn_cast_spell_selectable_when_casts_and_prepared() -> None:
    """cast_spell available when casts_remaining > 0 and prepared non-empty."""
    cdef = _combat_def()
    mage = _wwn_mage()
    sc = _spellcasting(casts_remaining=2, prepared=["shatter"])

    out = beats_available_for(
        cdef,
        mage,
        spell_slots_remaining=0.0,  # B/X would block this; WWN arm must ignore it
        spellcasting=sc,
    )
    beat_ids = [b.id for b in out]
    assert "cast_spell" in beat_ids, f"cast_spell must be selectable; got {beat_ids}"


# ---------------------------------------------------------------------------
# AC2 — casts_remaining == 0 → filtered out; reason "no_slots"
# ---------------------------------------------------------------------------


def test_wwn_cast_spell_filtered_when_no_casts_remaining() -> None:
    """casts_remaining == 0 → cast_spell filtered out."""
    cdef = _combat_def()
    mage = _wwn_mage()
    sc = _spellcasting(casts_remaining=0, prepared=["shatter"])

    out = beats_available_for(
        cdef,
        mage,
        spell_slots_remaining=99.0,  # B/X would allow; WWN arm must use sc instead
        spellcasting=sc,
    )
    beat_ids = [b.id for b in out]
    assert "cast_spell" not in beat_ids, f"cast_spell must be filtered; got {beat_ids}"


def test_wwn_cast_spell_rejection_no_casts_remaining() -> None:
    """casts_remaining == 0 → rejection reason 'no_slots'."""
    cdef = _combat_def()
    mage = _wwn_mage()
    sc = _spellcasting(casts_remaining=0, prepared=["shatter"])

    reason = cast_spell_rejection_reason(
        cdef,
        mage,
        spell_slots_remaining=99.0,
        spellcasting=sc,
    )
    assert reason == "no_slots", f"expected 'no_slots'; got {reason!r}"


# ---------------------------------------------------------------------------
# AC3 — prepared == [] → filtered out; reason "unprepared"
# ---------------------------------------------------------------------------


def test_wwn_cast_spell_filtered_when_not_prepared() -> None:
    """prepared == [] → cast_spell filtered out."""
    cdef = _combat_def()
    mage = _wwn_mage()
    sc = _spellcasting(casts_remaining=3, prepared=[])

    out = beats_available_for(
        cdef,
        mage,
        spell_slots_remaining=99.0,
        spellcasting=sc,
    )
    beat_ids = [b.id for b in out]
    assert "cast_spell" not in beat_ids, f"cast_spell must be filtered; got {beat_ids}"


def test_wwn_cast_spell_rejection_unprepared() -> None:
    """prepared == [] → rejection reason 'unprepared'."""
    cdef = _combat_def()
    mage = _wwn_mage()
    sc = _spellcasting(casts_remaining=3, prepared=[])

    reason = cast_spell_rejection_reason(
        cdef,
        mage,
        spell_slots_remaining=99.0,
        spellcasting=sc,
    )
    assert reason == "unprepared", f"expected 'unprepared'; got {reason!r}"


# ---------------------------------------------------------------------------
# AC4 — class that doesn't allow cast_spell → reason "class"
# ---------------------------------------------------------------------------


def test_wwn_cast_spell_rejection_class_not_allowed() -> None:
    """Warrior class does not list cast_spell → reason 'class'."""
    cdef = _combat_def()
    warrior = _wwn_warrior()
    sc = _spellcasting(casts_remaining=3, prepared=["shatter"])

    reason = cast_spell_rejection_reason(
        cdef,
        warrior,
        spell_slots_remaining=99.0,
        spellcasting=sc,
    )
    assert reason == "class", f"expected 'class'; got {reason!r}"


def test_wwn_cast_spell_filtered_for_non_caster_class() -> None:
    """Warrior with full spellcasting state still can't select cast_spell."""
    cdef = _combat_def()
    warrior = _wwn_warrior()
    sc = _spellcasting(casts_remaining=3, prepared=["shatter"])

    out = beats_available_for(
        cdef,
        warrior,
        spell_slots_remaining=0.0,
        spellcasting=sc,
    )
    beat_ids = [b.id for b in out]
    assert "cast_spell" not in beat_ids, f"Warrior must not see cast_spell; got {beat_ids}"


# ---------------------------------------------------------------------------
# AC5 — backward-compat: spellcasting=None → B/X behavior unchanged
# ---------------------------------------------------------------------------


def test_backward_compat_bx_behavior_with_spellcasting_none() -> None:
    """spellcasting=None → existing B/X gate (spell_slots_remaining) governs."""
    from sidequest.genre.models.character import ClassDef as _CD

    bx_mage = _CD(
        id="mage",
        display_name="Mage",
        rpg_role="caster",
        jungian_default="sage",
        prime_requisite="INT",
        minimum_score=9,
        kit_table="mage_kit",
        flavor="-",
        encounter_beat_choices=["cast_spell"],
    )
    bx_cdef = _confrontation([_beat("cast_spell", class_filter=["Mage"])])

    # With slots and spellcasting=None → allowed (B/X default)
    out_with_slots = beats_available_for(
        bx_cdef,
        bx_mage,
        spell_slots_remaining=1.0,
        spellcasting=None,
    )
    assert any(b.id == "cast_spell" for b in out_with_slots), (
        "B/X mage with slots and spellcasting=None must see cast_spell"
    )

    # Without slots and spellcasting=None → blocked (B/X default)
    out_no_slots = beats_available_for(
        bx_cdef,
        bx_mage,
        spell_slots_remaining=0.0,
        spellcasting=None,
    )
    assert not any(b.id == "cast_spell" for b in out_no_slots), (
        "B/X mage without slots and spellcasting=None must NOT see cast_spell"
    )
