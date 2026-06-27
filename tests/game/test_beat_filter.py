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


# ---------------------------------------------------------------------------
# Story 106-4 Part C — inventory beat-scan (Keith priority)
# ---------------------------------------------------------------------------
# A confrontation beat menu must scan the actor's carried inventory and surface
# transient "use_item:<slug>" beats for usable consumables (a heal potion in a
# fight). Gated to hp_depletion combat — drinking a heal potion in a chase /
# social cdef is meaningless, so item beats only appear where the effect lands.


def _hp_combat(beats):
    return ConfrontationDef(
        type="combat",
        label="C",
        category="combat",
        win_condition="hp_depletion",
        player_metric=MetricDef(name="m", starting=0, threshold=7),
        opponent_metric=MetricDef(name="m", starting=0, threshold=7),
        beats=beats,
        opponent_default_stats={
            "STR": 10,
            "DEX": 10,
            "CON": 10,
            "INT": 10,
            "WIS": 10,
            "CHA": 10,
            "hp": 10,
            "armor_class": 10,
            "dexterity": 10,
        },
    )


def _potion(name="Potion of Mending", *, heal="1d6+2", tags=("consumable", "healing")):
    return {"name": name, "category": "consumable", "tags": list(tags), "heal_amount": heal}


def test_heal_consumable_surfaces_use_item_beat_in_hp_combat():
    from sidequest.game.beat_filter import is_item_use_beat

    cd = _hp_combat([_beat("strike")])
    fighter = _class("Fighter", ["strike"])
    out = beats_available_for(cd, fighter, spell_slots_remaining=0.0, inventory_items=[_potion()])
    ids = [b.id for b in out]
    # the authored strike stays, plus exactly one transient item-use beat
    assert "strike" in ids
    item_beats = [b for b in out if is_item_use_beat(b.id)]
    assert len(item_beats) == 1
    assert item_beats[0].label == "Drink Potion of Mending"


def test_no_item_beat_when_no_inventory_passed():
    # Backward compat: existing callers that omit inventory_items get no item
    # beats and the pre-106-4 shape, byte-for-byte.
    cd = _hp_combat([_beat("strike")])
    fighter = _class("Fighter", ["strike"])
    out = beats_available_for(cd, fighter, spell_slots_remaining=0.0)
    assert [b.id for b in out] == ["strike"]


def test_no_item_beat_outside_hp_depletion_combat():
    from sidequest.game.beat_filter import is_item_use_beat

    chase = ConfrontationDef(
        type="chase",
        label="Corridor Pursuit",
        category="movement",
        player_metric=MetricDef(name="separation", starting=0, threshold=7),
        opponent_metric=MetricDef(name="pursuit", starting=0, threshold=7),
        beats=[_beat("sprint")],
    )
    fighter = _class("Fighter", ["sprint"])
    out = beats_available_for(
        chase, fighter, spell_slots_remaining=0.0, inventory_items=[_potion()]
    )
    assert not any(is_item_use_beat(b.id) for b in out)


def test_non_heal_consumable_does_not_surface_item_beat():
    from sidequest.game.beat_filter import is_item_use_beat

    cd = _hp_combat([_beat("strike")])
    fighter = _class("Fighter", ["strike"])
    rations = {"name": "Day's Rations", "category": "consumable", "tags": ["consumable"]}
    out = beats_available_for(cd, fighter, spell_slots_remaining=0.0, inventory_items=[rations])
    assert not any(is_item_use_beat(b.id) for b in out)


def test_non_consumable_with_heal_amount_does_not_surface():
    # A heal_amount on a non-consumable (mis-tagged) must NOT become a beat —
    # the consume lane only removes genuine single-use items.
    from sidequest.game.beat_filter import is_item_use_beat

    cd = _hp_combat([_beat("strike")])
    fighter = _class("Fighter", ["strike"])
    weird = {"name": "Healing Idol", "category": "tool", "tags": ["tool"], "heal_amount": "1d6"}
    out = beats_available_for(cd, fighter, spell_slots_remaining=0.0, inventory_items=[weird])
    assert not any(is_item_use_beat(b.id) for b in out)


def test_duplicate_consumables_collapse_to_one_beat():
    # Two identical Potions of Mending -> one "Drink" beat (using it consumes
    # one stack member; the menu shouldn't show the same action twice).
    from sidequest.game.beat_filter import is_item_use_beat

    cd = _hp_combat([_beat("strike")])
    fighter = _class("Fighter", ["strike"])
    out = beats_available_for(
        cd,
        fighter,
        spell_slots_remaining=0.0,
        inventory_items=[_potion(), _potion()],
    )
    item_beats = [b for b in out if is_item_use_beat(b.id)]
    assert len(item_beats) == 1


def test_item_use_beat_id_roundtrips_to_item_name():
    # The dispatch resolves which item to consume by matching the slug in the
    # beat id back against the inventory — the mapping must be stable.
    from sidequest.game.beat_filter import item_slug, item_use_beat_id

    assert item_use_beat_id("Potion of Mending") == "use_item:potion_of_mending"
    assert item_slug("Potion of Mending (Greater)") == "potion_of_mending_greater"


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


# ---------------------------------------------------------------------------
# WN-binding player action menu (sq-playtest 2026-06-27, ADR-143 / epic 108).
#
# 108-3 strips cdef.beats to [] for a WWN hp_depletion combat (the WN engine owns
# the round). The RESOLUTION path (wn_round.py / dice.py, stories 108-8 / 152-1)
# already SYNTHESIZES the WN action beats — attack + Total Defense / Fighting
# Withdrawal / Run — when committed. But the SELECTION MENU only ever synthesized
# cast_spell (the caster twin), so a non-caster Warrior in WWN combat saw an EMPTY
# attack menu — only inventory "Drink <potion>" beats — and could never commit an
# attack: the combat soft-lock the playtest hit. These pin the missing menu twin.
# ---------------------------------------------------------------------------

_WN_MENU_IDS = ["attack", "total_defense", "fighting_withdrawal", "run"]


def test_wn_binding_synthesizes_action_menu_when_cdef_beats_empty():
    # WWN hp_depletion combat with cdef.beats == [] (108-3) + a NON-caster class
    # whose encounter_beat_choices don't enumerate the WN actions → the menu still
    # offers the full WWN SRD 2.4.4 action set, attack first.
    cd = _hp_combat([])
    warrior = _class("Warrior", ["sprint"])  # no WN action in encounter_beat_choices
    out = beats_available_for(cd, warrior, spell_slots_remaining=0.0, is_wn_binding=True)
    ids = [b.id for b in out]
    assert ids == _WN_MENU_IDS, "WN binding offers attack + defensive/move actions, attack first"
    attack = next(b for b in out if b.id == "attack")
    assert attack.label == "Attack"


def test_wn_action_menu_appended_before_item_use_beats():
    # The synthesized WN actions lead the menu; a carried heal consumable still
    # appears, sorted to the end (so Attack is never buried under inventory).
    from sidequest.game.beat_filter import is_item_use_beat

    cd = _hp_combat([])
    warrior = _class("Warrior", ["sprint"])
    out = beats_available_for(
        cd, warrior, spell_slots_remaining=0.0, inventory_items=[_potion()], is_wn_binding=True
    )
    ids = [b.id for b in out]
    assert ids[:4] == _WN_MENU_IDS
    assert any(is_item_use_beat(i) for i in ids)
    assert ids.index("attack") < next(i for i, b in enumerate(out) if is_item_use_beat(b.id))


def test_native_binding_does_not_synthesize_wn_actions():
    # Backward compat: a NATIVE (non-WN) pack — is_wn_binding default False — never
    # synthesizes the WN action beats. An empty-beats cdef stays an empty menu.
    cd = _hp_combat([])
    warrior = _class("Warrior", ["sprint"])
    out = beats_available_for(cd, warrior, spell_slots_remaining=0.0)
    assert [b.id for b in out] == []


def test_no_wn_action_synthesis_outside_hp_depletion():
    # A WN-bound NON-combat confrontation (chase) must not grow an "Attack" beat —
    # the WN action set is a melee/hp_depletion affordance only (mirrors item-use).
    chase = ConfrontationDef(
        type="chase",
        label="Corridor Pursuit",
        category="movement",
        player_metric=MetricDef(name="separation", starting=0, threshold=7),
        opponent_metric=MetricDef(name="pursuit", starting=0, threshold=7),
        beats=[],
    )
    warrior = _class("Warrior", ["sprint"])
    out = beats_available_for(chase, warrior, spell_slots_remaining=0.0, is_wn_binding=True)
    assert [b.id for b in out] == []
