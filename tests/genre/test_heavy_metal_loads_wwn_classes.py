"""heavy_metal → WWN Story 2 — classes, chargen-magic, and 5e-purge load proof.

RED for story 87-2 (classes & chargen + real magic, epic Story 3 folded in per
Keith 2026-06-05). Design:
docs/superpowers/specs/2026-06-05-heavy-metal-wwn-classes-chargen-design.md
(read the 2026-06-05 AMENDMENT banner — casters ship REAL magic, not Effort-only).

Asserts, on the real bound heavy_metal pack:

  1. ruleset == "wwn" (Story 1, regression guard);
  2. classes.yaml exposes the 5 faithful-WWN classes with correct chassis markers
     (warrior: warrior==True; necromancer/elementalist/pact_born: magic_access=="wwn"
     with FULL wwn_magic; expert: no magic, not warrior);
  3. every class carries saving_throws (the spell-catalog load validator requires it);
  4. each class has >=1 signature ability (ADR-095); no class lists cast_spell as a
     non-caster encounter_beat_choice;
  5. wwn_spell_catalog is present and non-empty (REAL magic — ported WWN High Magic);
  6. every caster's starting_prepared resolves in the catalog;
  7. the Blade-work combat confrontation has a cast_spell beat with class_filter to the
     three caster classes (and only those);
  8. rules.yaml is purged of 5e scaffolding: class_label=="Calling", default_class
     resolves to the Warrior, no 5e class/race/spell names survive, default_race dropped.

Skips cleanly when sidequest-content is not on disk.
"""

from __future__ import annotations

import pytest

from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

# The 5 faithful-WWN classes (spec §3). (id, display_name, prime_requisite,
# is_caster, is_warrior). governing canonical attr for casters checked below.
_EXPECTED_CLASSES = [
    ("warrior", "Warrior", "STR", False, True),
    ("expert", "Expert", "DEX", False, False),
    ("necromancer", "Necromancer", "INT", True, False),
    ("elementalist", "Elementalist", "INT", True, False),
    ("pact_born", "Pact-born", "CHA", True, False),
]
_CASTER_IDS = {"necromancer", "elementalist", "pact_born"}

# 5e scaffolding that MUST NOT survive the port anywhere in rules.yaml.
_5E_CLASS_NAMES = {
    "Fighter", "Ranger", "Rogue", "Cleric", "Druid", "Bard",
    "Barbarian", "Monk", "Wizard", "Warlock", "Sorcerer", "Paladin",
}
_5E_RACE_NAMES = {"Human", "Dwarf", "Elf", "Halfling"}
_WWN_CANONICAL_KEYS = {"STRENGTH", "DEXTERITY", "CONSTITUTION", "INTELLIGENCE", "WISDOM", "CHARISMA"}


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


def _load_heavy_metal():
    from sidequest.genre.loader import load_genre_pack

    try:
        return load_genre_pack(find_pack_path("heavy_metal"))
    except PackNotFound:  # pragma: no cover - environment guard
        pytest.skip("sidequest-content not on disk in this checkout")


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_heavy_metal_classes_are_faithful_wwn_chassis() -> None:
    pack = _load_heavy_metal()

    assert pack.rules is not None
    assert pack.rules.ruleset == "wwn", (
        f"heavy_metal must stay bound ruleset: wwn (Story 1); got {pack.rules.ruleset!r}"
    )

    assert pack.classes is not None, "classes.yaml must be authored (Story 2) — pack.classes is None"
    by_id = {c.id: c for c in pack.classes}
    assert set(by_id) == {e[0] for e in _EXPECTED_CLASSES}, (
        f"heavy_metal must declare exactly the 5 WWN classes "
        f"{sorted(e[0] for e in _EXPECTED_CLASSES)}; got {sorted(by_id)}"
    )

    for cid, display, prime, is_caster, is_warrior in _EXPECTED_CLASSES:
        cls = by_id[cid]
        assert cls.display_name == display, f"{cid} display_name must be {display!r}; got {cls.display_name!r}"
        assert cls.prime_requisite == prime, (
            f"{cid} prime_requisite must be {prime!r} (abbreviation, not flavor name); got {cls.prime_requisite!r}"
        )
        assert cls.rpg_role, f"{cid} must declare a non-empty rpg_role"

        # ADR-095: one signature ability per class.
        assert len(cls.abilities) >= 1, f"{cid} must carry >=1 signature ability (ADR-095); got {len(cls.abilities)}"

        # Story-3-folded-in: saving_throws required on every class once a catalog ships.
        assert cls.saving_throws is not None, (
            f"{cid} must declare saving_throws — the spell-catalog load validator "
            f"requires it on every class once a spell catalog is present"
        )

        # cast_spell is a caster-only beat — never offered as a generic class beat.
        assert "cast_spell" not in cls.encounter_beat_choices, (
            f"{cid} must NOT list cast_spell in encounter_beat_choices — the rules.yaml "
            f"class_filter is the only gate that should offer it"
        )

        if is_warrior:
            assert cls.warrior is True, f"{cid} must set warrior: true (Killing Blow / Veteran's Luck seams)"
        else:
            assert cls.warrior is False, f"{cid} must not set warrior: true"

        if is_caster:
            assert cls.magic_access == "wwn", f"{cid} must set magic_access: wwn; got {cls.magic_access!r}"
            wm = cls.wwn_magic
            assert wm is not None, f"{cid} (caster) must carry a wwn_magic block"
            # REAL magic — NOT Effort-only. Full cast tables + a starting spell list.
            assert wm.effort_sources, f"{cid} wwn_magic must declare effort_sources"
            assert wm.casts_per_day_by_level.get("1"), f"{cid} must declare casts_per_day_by_level['1'] (real caster)"
            assert wm.max_spell_level_by_level.get("1"), f"{cid} must declare max_spell_level_by_level['1']"
            assert wm.prepared_by_level.get("1"), f"{cid} must declare prepared_by_level['1']"
            assert wm.starting_prepared, (
                f"{cid} must declare a non-empty starting_prepared (real spells) — "
                f"this is the change from the superseded Effort-only design"
            )
            for src in wm.effort_sources:
                assert src.governing_attr in _WWN_CANONICAL_KEYS, (
                    f"{cid} effort_source.governing_attr must be a canonical WWN key "
                    f"(e.g. INTELLIGENCE/CHARISMA); got {src.governing_attr!r}"
                )
        else:
            assert cls.magic_access is None, f"{cid} (non-caster) must not set magic_access; got {cls.magic_access!r}"
            assert cls.wwn_magic is None, f"{cid} (non-caster) must not carry wwn_magic"


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_heavy_metal_ships_real_wwn_spell_catalog() -> None:
    pack = _load_heavy_metal()

    cat = pack.wwn_spell_catalog
    assert cat is not None, (
        "wwn_spell_catalog is None — spells_wwn.yaml must be authored (real WWN magic, "
        "epic Story 3 folded into 87-2)"
    )
    assert len(cat.spells) > 0, "spells_wwn.yaml must declare at least one spell"

    catalog_ids = {s.id for s in cat.spells}
    assert pack.classes is not None
    for cls in pack.classes:
        if cls.wwn_magic is None:
            continue
        for spell_id in cls.wwn_magic.starting_prepared:
            assert spell_id in catalog_ids, (
                f"class {cls.id!r} starting_prepared {spell_id!r} not in wwn_spell_catalog "
                f"({sorted(catalog_ids)})"
            )

    # At least one prepared spell must deal damage, so combat casting is real (and the
    # dispatch e2e has a damage spell to fire).
    damage_ids = {s.id for s in cat.spells if s.damage_die}
    prepared = {sid for c in pack.classes if c.wwn_magic for sid in c.wwn_magic.starting_prepared}
    assert prepared & damage_ids, (
        "at least one caster must start with a damage-dealing spell prepared so combat "
        "casting actually ablates HP (mirrors EH's cinder_lance)"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_heavy_metal_blade_work_has_class_filtered_cast_spell() -> None:
    pack = _load_heavy_metal()

    combat = next(
        (c for c in pack.rules.confrontations if c.confrontation_type == "combat"),
        None,
    )
    assert combat is not None, "heavy_metal must expose a 'combat' (Blade-work) confrontation"

    cast_beat = next((b for b in combat.beats if b.id == "cast_spell"), None)
    assert cast_beat is not None, (
        f"Blade-work must add a cast_spell beat (real magic); beats: {sorted(b.id for b in combat.beats)}"
    )
    assert cast_beat.class_filter, "cast_spell must carry a class_filter so the cast gate fires only for casters"

    # The filter must name the three caster classes (by id or display_name) and nothing else.
    by_id = {c.id: c for c in pack.classes}
    caster_labels = {by_id[cid].id for cid in _CASTER_IDS} | {by_id[cid].display_name for cid in _CASTER_IDS}
    noncaster_labels = (
        {c.id for c in pack.classes if c.id not in _CASTER_IDS}
        | {c.display_name for c in pack.classes if c.id not in _CASTER_IDS}
    )
    flt = set(cast_beat.class_filter)
    assert flt <= caster_labels, (
        f"cast_spell class_filter must contain only the caster classes "
        f"{sorted(caster_labels)}; got {sorted(flt)}"
    )
    assert not (flt & noncaster_labels), (
        f"cast_spell class_filter must NOT include non-caster classes; got {sorted(flt)}"
    )
    assert len(flt) == 3, f"cast_spell class_filter must name all three caster traditions; got {sorted(flt)}"


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_heavy_metal_rules_purged_of_5e_scaffolding() -> None:
    pack = _load_heavy_metal()
    rules = pack.rules

    assert rules.class_label == "Calling", f"class_label must be 'Calling'; got {rules.class_label!r}"

    assert rules.default_class is not None, "default_class must be set"
    assert rules.default_class.lower() == "warrior", (
        f"default_class must resolve to the Warrior; got {rules.default_class!r}"
    )

    # 5e class names must not survive in allowed_classes.
    leftover_classes = set(rules.allowed_classes) & _5E_CLASS_NAMES
    assert not leftover_classes, f"5e class names survive in allowed_classes: {sorted(leftover_classes)}"
    # If populated, every allowed_class must be a real WWN class.
    if rules.allowed_classes:
        valid = {c.id for c in pack.classes} | {c.display_name for c in pack.classes}
        unknown = set(rules.allowed_classes) - valid
        assert not unknown, f"allowed_classes names classes not in classes.yaml: {sorted(unknown)}"

    # 5e races + banned-spell list are dropped (races are world-tier; WWN has no ban list).
    leftover_races = set(rules.allowed_races) & _5E_RACE_NAMES
    assert not leftover_races, f"5e race names survive in allowed_races: {sorted(leftover_races)}"
    assert rules.default_race is None, (
        f"default_race must be dropped (races are world-tier; genre cultures.yaml is []); got {rules.default_race!r}"
    )
    assert rules.banned_spells == [], (
        f"the 5e banned_spells list must be dropped (WWN gates by class spell list); got {rules.banned_spells!r}"
    )
