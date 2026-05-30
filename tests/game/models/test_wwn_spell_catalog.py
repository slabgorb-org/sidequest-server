"""Tests for WwnSpell + WwnSpellCatalog content model (Task 3).

Covers:
- WwnSpell parses from dict
- to_cast_input() yields CastInput with matching id/level/save/damage_die/damage_per_level
- No-save utility spell has save=None
- WwnSpellCatalog rejects duplicate ids
- load_wwn_spell_catalog round-trips from a tmp YAML file
- WwnSpellCatalog.get() raises KeyError on a missing id (fail loud)
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from sidequest.game.wwn_magic import CastInput
from sidequest.genre.models.wwn_spell import (
    WwnSpell,
    WwnSpellCatalog,
    load_wwn_spell_catalog,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_spell(**overrides: object) -> dict:
    """Minimal valid WwnSpell dict, with optional overrides."""
    base: dict = {
        "id": "burning_hands",
        "name": "Burning Hands",
        "level": 1,
        "save": "evasion",
        "damage_die": "1d6",
        "damage_per_level": False,
        "genre_description": "Jets of flame scorch all before you.",
        "mechanical_effect": "Deal fire damage; evasion save halves.",
        "range": "near",
        "target": "area",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# WwnSpell unit tests
# ---------------------------------------------------------------------------


class TestWwnSpell:
    def test_parse_from_dict(self) -> None:
        """WwnSpell accepts all fields from a valid dict."""
        spell = WwnSpell.model_validate(make_spell())
        assert spell.id == "burning_hands"
        assert spell.name == "Burning Hands"
        assert spell.level == 1

    def test_to_cast_input_fields_match(self) -> None:
        """to_cast_input() yields a CastInput with id/level/save/damage_die/damage_per_level."""
        spell = WwnSpell.model_validate(
            make_spell(
                id="frost_bolt",
                level=2,
                save="mental",
                damage_die="2d8",
                damage_per_level=True,
            )
        )
        ci: CastInput = spell.to_cast_input()
        assert isinstance(ci, CastInput)
        assert ci.id == "frost_bolt"
        assert ci.level == 2
        assert ci.save == "mental"
        assert ci.damage_die == "2d8"
        assert ci.damage_per_level is True

    def test_no_save_utility_spell_has_save_none(self) -> None:
        """A no-save utility spell produces a CastInput with save=None."""
        spell = WwnSpell.model_validate(
            make_spell(
                id="detect_magic",
                save=None,
                damage_die=None,
            )
        )
        ci = spell.to_cast_input()
        assert ci.save is None
        assert ci.damage_die is None

    def test_damage_per_level_defaults_false(self) -> None:
        data = make_spell()
        data.pop("damage_per_level")
        spell = WwnSpell.model_validate(data)
        assert spell.damage_per_level is False
        assert spell.to_cast_input().damage_per_level is False

    def test_extra_fields_are_forbidden(self) -> None:
        """extra='forbid' rejects unknown keys."""
        with pytest.raises(ValidationError):
            WwnSpell.model_validate(make_spell(unknown_key="oops"))

    def test_save_category_all_valid_literals(self) -> None:
        """All four SaveCategory literals are accepted by WwnSpell."""
        for cat in ("physical", "evasion", "mental", "luck"):
            spell = WwnSpell.model_validate(make_spell(save=cat))
            ci = spell.to_cast_input()
            assert ci.save == cat

    def test_to_cast_input_returns_cast_input_instance(self) -> None:
        spell = WwnSpell.model_validate(make_spell())
        assert type(spell.to_cast_input()) is CastInput


# ---------------------------------------------------------------------------
# WwnSpellCatalog unit tests
# ---------------------------------------------------------------------------


class TestWwnSpellCatalog:
    def test_catalog_accepts_valid_spells(self) -> None:
        catalog = WwnSpellCatalog.model_validate(
            {
                "version": "1.0",
                "spells": [make_spell(id="a"), make_spell(id="b")],
            }
        )
        assert len(catalog.spells) == 2

    def test_catalog_rejects_duplicate_ids(self) -> None:
        """Duplicate spell ids must raise at catalog validation time."""
        with pytest.raises(Exception, match="duplicate"):
            WwnSpellCatalog.model_validate(
                {
                    "version": "1.0",
                    "spells": [make_spell(id="dupe"), make_spell(id="dupe")],
                }
            )

    def test_get_returns_matching_spell(self) -> None:
        catalog = WwnSpellCatalog.model_validate(
            {
                "version": "1.0",
                "spells": [make_spell(id="alpha"), make_spell(id="beta")],
            }
        )
        spell = catalog.get("alpha")
        assert spell.id == "alpha"

    def test_get_raises_key_error_on_missing_id(self) -> None:
        """get() must raise KeyError on a missing id — no silent None returns."""
        catalog = WwnSpellCatalog.model_validate(
            {
                "version": "1.0",
                "spells": [make_spell(id="only_spell")],
            }
        )
        with pytest.raises(KeyError):
            catalog.get("nonexistent_spell")

    def test_empty_catalog_get_raises(self) -> None:
        catalog = WwnSpellCatalog.model_validate({"version": "1.0", "spells": []})
        with pytest.raises(KeyError):
            catalog.get("anything")

    def test_version_defaults_to_1_0(self) -> None:
        catalog = WwnSpellCatalog.model_validate({"spells": []})
        assert catalog.version == "1.0"


# ---------------------------------------------------------------------------
# load_wwn_spell_catalog round-trip
# ---------------------------------------------------------------------------


class TestLoadWwnSpellCatalog:
    def test_round_trip_from_yaml(self, tmp_path: Path) -> None:
        """load_wwn_spell_catalog reads a YAML file and returns a valid catalog."""
        yaml_text = textwrap.dedent(
            """\
            version: "1.0"
            spells:
              - id: fire_darts
                name: Fire Darts
                level: 1
                save: evasion
                damage_die: "1d4"
                damage_per_level: false
                genre_description: Tiny bolts of fire streak toward your enemies.
                mechanical_effect: Deal fire damage; evasion save halves.
                range: near
                target: single
              - id: mage_shield
                name: Mage Shield
                level: 1
                save: null
                damage_die: null
                genre_description: A shimmering barrier absorbs harm.
                mechanical_effect: Reduce next damage taken by caster level.
            """
        )
        yaml_file = tmp_path / "elemental_harmony_spells.yaml"
        yaml_file.write_text(yaml_text, encoding="utf-8")

        catalog = load_wwn_spell_catalog(yaml_file)

        assert isinstance(catalog, WwnSpellCatalog)
        assert len(catalog.spells) == 2
        assert catalog.version == "1.0"

        fire = catalog.get("fire_darts")
        assert fire.name == "Fire Darts"
        assert fire.level == 1
        assert fire.save == "evasion"
        assert fire.damage_die == "1d4"
        assert fire.damage_per_level is False

        ci = fire.to_cast_input()
        assert ci.id == "fire_darts"
        assert ci.level == 1
        assert ci.save == "evasion"
        assert ci.damage_die == "1d4"

        shield = catalog.get("mage_shield")
        assert shield.save is None
        assert shield.to_cast_input().save is None

    def test_round_trip_rejects_duplicate_ids_in_yaml(self, tmp_path: Path) -> None:
        yaml_text = textwrap.dedent(
            """\
            version: "1.0"
            spells:
              - id: twin_spell
                name: Twin Spell A
                level: 1
                genre_description: First.
                mechanical_effect: Effect A.
              - id: twin_spell
                name: Twin Spell B
                level: 2
                genre_description: Second.
                mechanical_effect: Effect B.
            """
        )
        yaml_file = tmp_path / "bad_catalog.yaml"
        yaml_file.write_text(yaml_text, encoding="utf-8")

        with pytest.raises(Exception, match="duplicate"):
            load_wwn_spell_catalog(yaml_file)
