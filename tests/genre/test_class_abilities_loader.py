"""Story 2026-05-10 — class mechanical surface.

Loader-level checks for the new `abilities` key on ClassDef and the
`taunt` beat for Fighter.
"""

from __future__ import annotations

from pathlib import Path

from sidequest.genre.loader import load_genre_pack

GENRE_ROOT = Path(__file__).parents[2] / "../sidequest-content/genre_packs"


def test_caverns_and_claudes_loads_with_committed_blow_beat():
    """WWN port: the loader resolves a class's encounter_beat_choices against
    the rules.yaml beat pool. The Warrior declares 'committed_blow' (the WWN
    all-in strike), and rules.yaml's combat confrontation defines it."""
    pack = load_genre_pack(GENRE_ROOT.resolve() / "caverns_and_claudes")
    warrior = next(c for c in pack.classes if c.id == "warrior")
    assert "committed_blow" in warrior.encounter_beat_choices, (
        "Warrior must declare 'committed_blow' in encounter_beat_choices"
    )
    all_beat_ids = {b.id for cd in pack.rules.confrontations for b in cd.beats}
    assert "committed_blow" in all_beat_ids, "rules.yaml must declare a 'committed_blow' beat"


def test_class_def_parses_abilities_key():
    """A class with abilities: yields a list of ClassAbilityDef entries."""
    from sidequest.genre.models.character import ClassAbilityDef, ClassDef

    cd = ClassDef.model_validate(
        {
            "id": "cleric",
            "display_name": "Cleric",
            "rpg_role": "healer",
            "jungian_default": "caregiver",
            "prime_requisite": "WIS",
            "minimum_score": 9,
            "kit_table": "cleric_kit",
            "encounter_beat_choices": ["attack", "defend", "flee", "turn_undead"],
            "abilities": [
                {
                    "name": "Turn Undead",
                    "genre_description": "He raises the symbol; the unliving recoil.",
                    "mechanical_effect": "2d6 vs HD; loud; fails on intelligent unliving.",
                    "involuntary": False,
                }
            ],
        }
    )
    assert len(cd.abilities) == 1
    assert isinstance(cd.abilities[0], ClassAbilityDef)
    assert cd.abilities[0].name == "Turn Undead"
    assert cd.abilities[0].involuntary is False


def test_class_def_default_empty_abilities():
    """Absent abilities: → empty list. Mage path."""
    from sidequest.genre.models.character import ClassDef

    cd = ClassDef.model_validate(
        {
            "id": "mage",
            "display_name": "Mage",
            "rpg_role": "control",
            "jungian_default": "magician",
            "prime_requisite": "INT",
            "minimum_score": 9,
            "kit_table": "mage_kit",
            "encounter_beat_choices": ["attack", "defend", "flee", "cast_spell"],
        }
    )
    assert cd.abilities == []


def test_caverns_classes_have_signature_abilities():
    """WWN port: the 3-chassis Callings carry their signature abilities.
    Warrior gets the WWN Warrior pair (Killing Blow + Veteran's Luck, gated on
    warrior: true); Expert reads the room (Read the Ledger); Mage reads the
    worked stone."""
    pack = load_genre_pack(GENRE_ROOT.resolve() / "caverns_and_claudes")

    by_id = {c.id: c for c in pack.classes}
    warrior, expert, mage = by_id["warrior"], by_id["expert"], by_id["mage"]

    warrior_abilities = {a.name for a in warrior.abilities}
    assert warrior_abilities == {"Killing Blow", "Veteran's Luck"}
    assert len(expert.abilities) == 1 and expert.abilities[0].name == "Read the Ledger"
    assert len(mage.abilities) == 1 and mage.abilities[0].name == "Read the Worked Stone"

    # Prose is non-empty (no {writer agent fills} placeholder).
    for c in (warrior, expert, mage):
        for ability in c.abilities:
            assert ability.genre_description and "{writer agent" not in ability.genre_description, (
                f"{c.id} ability {ability.name!r} genre_description still has placeholder text"
            )
            assert ability.mechanical_effect, (
                f"{c.id} ability {ability.name!r} mechanical_effect blank"
            )


def test_blank_genre_description_raises():
    """Empty genre_description fails loud, not silent."""
    import pytest

    from sidequest.genre.models.character import ClassDef

    with pytest.raises(Exception) as exc:
        ClassDef.model_validate(
            {
                "id": "cleric",
                "display_name": "Cleric",
                "rpg_role": "healer",
                "jungian_default": "caregiver",
                "prime_requisite": "WIS",
                "minimum_score": 9,
                "kit_table": "cleric_kit",
                "encounter_beat_choices": ["attack"],
                "abilities": [
                    {
                        "name": "Turn Undead",
                        "genre_description": "",  # blank — must fail
                        "mechanical_effect": "2d6 vs HD",
                    }
                ],
            }
        )
    # Error message should mention which field is blank.
    msg = str(exc.value).lower()
    assert "genre_description" in msg or "blank" in msg, (
        f"Expected error to mention genre_description or 'blank', got: {exc.value}"
    )
