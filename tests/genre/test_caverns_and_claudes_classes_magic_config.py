"""Story 47-10 AC2 — Mage/Cleric ship magic_config in classes.yaml.

The cnc-bx work added `magic_access: innate_v1` to Mage and Cleric. This
story adds the `magic_config` block (tradition, slot tables, save DC stat,
turn_undead) that `seed_learned_v1_state` reads at session init to populate
known_spells, prepared_spells, and per-level slot ledger bars.

These tests load the live caverns_and_claudes/classes.yaml from the content
repo and assert each caster class declares the contract `seed_learned_v1_state`
expects. Failures here mean the content side hasn't shipped yet.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sidequest.genre.models.character import ClassDef

CONTENT_ROOT = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"
CC_CLASSES = CONTENT_ROOT / "caverns_and_claudes" / "classes.yaml"


@pytest.fixture
def cc_classes():
    if not CC_CLASSES.is_file():
        pytest.skip("caverns_and_claudes classes.yaml not found")
    with CC_CLASSES.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or []
    return [ClassDef.model_validate(item) for item in raw]


def _by_id(classes, class_id: str):
    for c in classes:
        if c.id == class_id:
            return c
    return None


def test_mage_declares_magic_config_with_arcane_tradition(cc_classes):
    mage = _by_id(cc_classes, "mage")
    assert mage is not None, "Mage class must exist in classes.yaml"
    assert mage.magic_config is not None, (
        "Mage must declare magic_config — seed_learned_v1_state requires it "
        "to populate known_spells from the arcane catalog"
    )
    assert mage.magic_config.tradition == "arcane"


def test_mage_magic_config_starting_known_spells(cc_classes):
    mage = _by_id(cc_classes, "mage")
    assert mage.magic_config.starting_known_spells == 3, (
        "C&C deviation from B/X canon: Magic-User starts with 3 known L1 "
        "spells (B/X canon = 2). The Sünden Mage gets the Detect Magic / "
        "Read Magic / Sleep package as a guild-outsider's working baseline "
        "— three workings give the L1 Mage one daily-cast option even "
        "after Read Magic burns the morning slot. (The catalog ships 12; "
        "3 is what the Mage memorizes / has in spellbook on day 1.)"
    )


def test_mage_magic_config_save_dc_stat_is_int(cc_classes):
    mage = _by_id(cc_classes, "mage")
    assert mage.magic_config.save_dc_stat == "INT", "Mage save DCs scale on INT (B/X-aligned)"


def test_mage_magic_config_turn_undead_false(cc_classes):
    mage = _by_id(cc_classes, "mage")
    assert mage.magic_config.turn_undead is False, (
        "Turn Undead is a Cleric class-special — Mage must declare turn_undead: false"
    )


def test_mage_magic_config_has_l1_slot_row(cc_classes):
    mage = _by_id(cc_classes, "mage")
    table = mage.magic_config.slots_by_class_level
    assert "1" in table, (
        f"Mage slots_by_class_level must have a row for class level 1; got keys {list(table.keys())!r}"
    )
    row = table["1"]
    assert "1" in row, f"Mage class L1 row must have an entry for spell level 1; got {row!r}"
    assert row["1"] >= 1, f"Mage class L1 must grant at least 1 L1 slot; got {row['1']}"


def test_cleric_declares_magic_config_with_divine_tradition(cc_classes):
    cleric = _by_id(cc_classes, "cleric")
    assert cleric is not None, "Cleric class must exist in classes.yaml"
    assert cleric.magic_config is not None, (
        "Cleric must declare magic_config to expose Turn Undead + divine "
        "tradition + WIS-keyed save DCs"
    )
    assert cleric.magic_config.tradition == "divine"


def test_cleric_magic_config_save_dc_stat_is_wis(cc_classes):
    cleric = _by_id(cc_classes, "cleric")
    assert cleric.magic_config.save_dc_stat == "WIS"


def test_cleric_magic_config_turn_undead_true(cc_classes):
    cleric = _by_id(cc_classes, "cleric")
    assert cleric.magic_config.turn_undead is True, (
        "Cleric Turn Undead is a class-special — magic_config.turn_undead "
        "must be true so the runtime can wire the action button"
    )


def test_fighter_and_thief_have_no_magic_config(cc_classes):
    fighter = _by_id(cc_classes, "fighter")
    thief = _by_id(cc_classes, "thief")
    assert fighter is not None and thief is not None
    assert fighter.magic_config is None, "Fighter is a non-caster"
    assert thief.magic_config is None, "Thief is a non-caster"


def test_mage_and_cleric_keep_innate_v1_magic_access(cc_classes):
    """The dual-plugin pivot: magic_access stays at innate_v1 for both casters
    (cnc-bx ship). magic_config is the data-side; magic_access is the surface-side.
    They must NOT migrate to learned_v1 — the spec was amended 2026-05-09 to
    keep innate_v1 as the player-facing surface."""
    mage = _by_id(cc_classes, "mage")
    cleric = _by_id(cc_classes, "cleric")
    assert mage.magic_access == "innate_v1", (
        f"Mage magic_access must be 'innate_v1' (dual-plugin pivot); got {mage.magic_access!r}"
    )
    assert cleric.magic_access == "innate_v1", (
        f"Cleric magic_access must be 'innate_v1'; got {cleric.magic_access!r}"
    )
