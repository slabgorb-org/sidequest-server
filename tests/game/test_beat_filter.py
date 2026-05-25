"""Tests for beats_available_for: class_filter ∩ encounter_beat_choices ∩ resource gate."""

import pytest

from sidequest.game.beat_filter import beats_available_for
from sidequest.genre.models.character import ClassDef
from sidequest.genre.models.rules import (
    BeatDef,
    BeatKind,
    ConfrontationDef,
    MetricDef,
)


def _beat(id_, *, class_filter=None):
    return BeatDef(
        id=id_, label=id_, kind=BeatKind.strike, stat_check="STR", class_filter=class_filter
    )


def _confrontation(beats):
    return ConfrontationDef(
        type="combat",
        label="C",
        category="combat",
        player_metric=MetricDef(name="m", starting=0, threshold=7),
        opponent_metric=MetricDef(name="m", starting=0, threshold=7),
        beats=beats,
    )


def _class(display_name, choices):
    return ClassDef(
        id=display_name.lower(),
        display_name=display_name,
        rpg_role="tank",
        jungian_default="warrior",
        prime_requisite="STR",
        minimum_score=9,
        kit_table=f"{display_name.lower()}_kit",
        flavor="-",
        encounter_beat_choices=choices,
    )


def test_universal_beats_visible_to_every_class():
    # A universal beat (class_filter=None) is available even when the class's
    # encounter_beat_choices does NOT enumerate it — encounter_beat_choices
    # curates class-specific beats only (see beats_available_for gate docs).
    cd = _confrontation([_beat("attack")])
    fighter = _class("Fighter", ["some_other_beat"])  # attack NOT whitelisted
    out = beats_available_for(cd, fighter, spell_slots_remaining=0.0)
    assert [b.id for b in out] == ["attack"]


def test_class_filter_excludes_other_classes():
    cd = _confrontation([_beat("cleave", class_filter=["Fighter"])])
    mage = _class("Mage", ["cleave"])  # mage's whitelist is wrong but filter still excludes
    out = beats_available_for(cd, mage, spell_slots_remaining=0.0)
    assert out == []


def test_encounter_beat_choices_narrows_class_specific_beats():
    # Gate 2 (encounter_beat_choices) narrows CLASS-SPECIFIC beats: a beat
    # gated to Fighter but absent from the Fighter's whitelist is excluded.
    cd = _confrontation(
        [_beat("cleave", class_filter=["Fighter"]), _beat("parry", class_filter=["Fighter"])]
    )
    fighter = _class("Fighter", ["cleave"])  # parry not whitelisted
    out = beats_available_for(cd, fighter, spell_slots_remaining=0.0)
    assert [b.id for b in out] == ["cleave"]


def test_encounter_beat_choices_does_not_narrow_universal_beats():
    # Regression (playtest 2026-05-25, beneath_sunden MP): a chase
    # confrontation's beats are universal (no class_filter) and no class
    # enumerates them in encounter_beat_choices. Previously gate 2 filtered
    # them ALL out → the per-PC confrontation payload had zero beats → the
    # table soft-locked with nothing to select. Universal beats must reach
    # every class regardless of the (combat-oriented) whitelist.
    chase = ConfrontationDef(
        type="chase",
        label="Corridor Pursuit",
        category="movement",
        player_metric=MetricDef(name="separation", starting=0, threshold=7),
        opponent_metric=MetricDef(name="pursuit", starting=0, threshold=7),
        beats=[_beat("sprint"), _beat("duck_through"), _beat("barricade"), _beat("douse_torch")],
    )
    # Fighter's whitelist is combat-only — lists none of the chase beats.
    fighter = _class("Fighter", ["attack", "defend", "flee", "cleave"])
    out = beats_available_for(chase, fighter, spell_slots_remaining=0.0)
    assert [b.id for b in out] == ["sprint", "duck_through", "barricade", "douse_torch"]


def test_cast_spell_filtered_when_no_slots():
    cd = _confrontation([_beat("cast_spell", class_filter=["Mage"])])
    mage = _class("Mage", ["cast_spell"])
    out = beats_available_for(cd, mage, spell_slots_remaining=0.0)
    assert out == []


def test_cast_spell_visible_when_slot_available():
    cd = _confrontation([_beat("cast_spell", class_filter=["Mage"])])
    mage = _class("Mage", ["cast_spell"])
    out = beats_available_for(cd, mage, spell_slots_remaining=1.0)
    assert [b.id for b in out] == ["cast_spell"]


def test_empty_encounter_beat_choices_raises():
    from sidequest.genre.error import PackError

    cd = _confrontation([_beat("attack")])
    fighter = _class("Fighter", [])
    with pytest.raises(PackError, match="empty encounter_beat_choices"):
        beats_available_for(cd, fighter, spell_slots_remaining=0.0)


# ---------------------------------------------------------------------------
# Story 47-10 — Prepared-list gate (AC4)
# ---------------------------------------------------------------------------
# When a Mage has slots remaining but nothing prepared at the relevant level,
# cast_spell must be filtered out. This is a distinct case from "no slots"
# (case from test_cast_spell_filtered_when_no_slots above) — slots=2,
# prepared={} should reject with reason `rejected_unprepared`, not
# `rejected_no_slots`. The two failure modes need to be distinguishable in
# OTEL for the GM panel.


def test_cast_spell_rejected_when_no_spells_prepared_despite_having_slots():
    """Mage has 2 slots but empty prepared_spells -> cast_spell unselectable."""
    cd = _confrontation([_beat("cast_spell", class_filter=["Mage"])])
    mage = _class("Mage", ["cast_spell"])
    out = beats_available_for(
        cd,
        mage,
        spell_slots_remaining=2.0,
        prepared_spells={},  # NOTHING prepared at any level
    )
    assert out == []


def test_cast_spell_visible_with_spell_prepared_at_l1():
    """Mage has Sleep prepared at L1 plus a slot -> cast_spell available."""
    cd = _confrontation([_beat("cast_spell", class_filter=["Mage"])])
    mage = _class("Mage", ["cast_spell"])
    out = beats_available_for(
        cd,
        mage,
        spell_slots_remaining=2.0,
        prepared_spells={1: ["sleep"]},
    )
    assert [b.id for b in out] == ["cast_spell"]


def test_cast_spell_visible_with_any_level_prepared():
    """Mage with only L2 prepared and slots remaining -> cast_spell available.

    The gate is 'something is prepared at SOME level' — narrator picks the
    actual spell from the prompt context block. This decouples the engine
    from per-beat per-level wiring (deferred to L2+ slot routing follow-up).
    """
    cd = _confrontation([_beat("cast_spell", class_filter=["Mage"])])
    mage = _class("Mage", ["cast_spell"])
    out = beats_available_for(
        cd,
        mage,
        spell_slots_remaining=1.0,
        prepared_spells={2: ["fireball"]},
    )
    assert [b.id for b in out] == ["cast_spell"]


def test_cast_spell_rejected_with_only_empty_level_lists():
    """Mage with prepared_spells={1: []} (level present but empty) -> rejected.

    Edge case: the dict has a key for level 1 but the list is empty. This
    can happen mid-state if all L1 spells were spent and the prep dict
    wasn't pruned.
    """
    cd = _confrontation([_beat("cast_spell", class_filter=["Mage"])])
    mage = _class("Mage", ["cast_spell"])
    out = beats_available_for(
        cd,
        mage,
        spell_slots_remaining=2.0,
        prepared_spells={1: []},
    )
    assert out == []


def test_cast_spell_rejection_distinguishes_slots_from_unprepared():
    """Two distinct rejection reasons must surface to the caller / OTEL.

    The function returns the filtered beat list, but the *reason* for
    filtering must be available to the OTEL caller so the GM panel can
    distinguish 'Mage out of slots' from 'Mage didn't memorize anything'.

    Acceptable surface: a sibling function `cast_spell_rejection_reason(...)`
    or a richer return type. The test asserts the two scenarios are
    distinguishable — exact API shape is the Dev's call.
    """
    from sidequest.game.beat_filter import cast_spell_rejection_reason

    cd = _confrontation([_beat("cast_spell", class_filter=["Mage"])])
    mage = _class("Mage", ["cast_spell"])

    # No slots, has prepared spells -> "no_slots"
    reason_no_slots = cast_spell_rejection_reason(
        cd, mage, spell_slots_remaining=0.0, prepared_spells={1: ["sleep"]}
    )
    assert reason_no_slots == "no_slots"

    # Slots remaining, nothing prepared -> "unprepared"
    reason_unprepared = cast_spell_rejection_reason(
        cd, mage, spell_slots_remaining=2.0, prepared_spells={}
    )
    assert reason_unprepared == "unprepared"

    # Slots remaining and prepared -> None (no rejection)
    reason_ok = cast_spell_rejection_reason(
        cd, mage, spell_slots_remaining=2.0, prepared_spells={1: ["sleep"]}
    )
    assert reason_ok is None


def test_existing_callers_unbroken_when_prepared_spells_omitted():
    """Backward compat: callers that pass only the existing 3 params must
    keep working. The new prepared_spells parameter is optional.

    When prepared_spells is omitted (or None), the gate behaves as before:
    cast_spell is allowed when slots remain. This protects every existing
    caller (narrator.py, orchestrator.py) until the prepared-list wiring
    rolls out repo-wide.

    NOTE: This is an intentional transitional contract. Once all callers
    pass prepared_spells, this test should be flipped to assert that
    omitting prepared_spells with cast_spell in pool raises a TypeError
    or PackError (callers should be required to supply it).
    """
    cd = _confrontation([_beat("cast_spell", class_filter=["Mage"])])
    mage = _class("Mage", ["cast_spell"])
    # No prepared_spells argument — default None — fall back to slot-only gate.
    out = beats_available_for(cd, mage, spell_slots_remaining=1.0)
    assert [b.id for b in out] == ["cast_spell"]
