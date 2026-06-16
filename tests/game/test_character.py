"""Tests for sidequest.game.character.

Port of tests in sidequest_game::character (character.rs mod tests).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.game.ability import AbilitySource
from sidequest.game.character import AbilityDefinition, Character, KnownFact
from sidequest.game.creature_core import (
    CreatureCore,
    HpPool,
    Inventory,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_test_character() -> Character:
    """Helper to build a valid Character for testing. Mirrors Rust test_character()."""
    return Character(
        core=CreatureCore(
            name="Thorn Ironhide",
            description="A scarred dwarf warrior",
            personality="Gruff but loyal",
            level=3,
            xp=0,
            inventory=Inventory(),
            statuses=[],
            hp=HpPool(current=10, max=10, base_max=10),
            acquired_advancements=[],
        ),
        backstory="Raised in the iron mines",
        narrative_state="Exploring the wastes",
        hooks=["nemesis: The Warden"],
        char_class="Fighter",
        race="Dwarf",
        pronouns="he/him",
        stats={
            "STR": 16,
            "DEX": 10,
            "CON": 14,
            "INT": 8,
            "WIS": 12,
            "CHA": 6,
        },
        abilities=[],
        known_facts=[],
        affinities=[],
        is_friendly=True,
        resolved_archetype=None,
        archetype_provenance=None,
    )


# ---------------------------------------------------------------------------
# Combatant trait equivalents
# ---------------------------------------------------------------------------


def test_combatant_name():
    """Rust: combatant_name"""
    c = make_test_character()
    assert c.name() == "Thorn Ironhide"


def test_combatant_hp():
    """Rust: combatant_edge (renamed hp, ADR-114)"""
    c = make_test_character()
    assert c.hp() == c.core.hp.current


def test_combatant_max_edge():
    """Rust: combatant_max_edge"""
    c = make_test_character()
    assert c.max_hp() == c.core.hp.max


def test_combatant_level():
    """Rust: combatant_level"""
    c = make_test_character()
    assert c.level() == 3


def test_combatant_not_broken_at_full_edge():
    """Rust: combatant_not_broken_at_full_edge"""
    c = make_test_character()
    assert not c.is_broken()


def test_combatant_broken_at_zero_edge():
    """Rust: combatant_broken_at_zero_edge"""
    c = make_test_character()
    c.core.hp.current = 0
    assert c.is_broken()


# ---------------------------------------------------------------------------
# Edge delta
# ---------------------------------------------------------------------------


def test_apply_damage_via_edge():
    """Rust: apply_damage_via_edge"""
    c = make_test_character()
    before = c.core.hp.current
    c.core.hp.apply_delta(-3)
    assert c.core.hp.current == before - 3


def test_damage_floored_at_zero():
    """Rust: damage_floored_at_zero"""
    c = make_test_character()
    c.core.hp.apply_delta(-1000)
    assert c.core.hp.current == 0


# ---------------------------------------------------------------------------
# JSON round-trip
# ---------------------------------------------------------------------------


def test_json_roundtrip():
    """Rust: json_roundtrip"""
    c = make_test_character()
    json_str = c.model_dump_json()
    back = Character.model_validate_json(json_str)
    assert back.core.name == "Thorn Ironhide"
    assert back.core.hp.base_max == c.core.hp.base_max
    assert back.core.level == 3


def test_blank_backstory_rejected():
    """Rust: blank_name_rejected_in_json — Python equivalent for backstory."""
    with pytest.raises(ValidationError):
        Character(
            core=CreatureCore(
                name="X",
                description="Y",
                personality="Z",
                inventory=Inventory(),
                statuses=[],
                hp=HpPool(current=10, max=10, base_max=10),
            ),
            backstory="",
            char_class="Fighter",
            race="Dwarf",
        )


def test_blank_char_class_rejected():
    with pytest.raises(ValidationError):
        Character(
            core=CreatureCore(
                name="X",
                description="Y",
                personality="Z",
                inventory=Inventory(),
                statuses=[],
                hp=HpPool(current=10, max=10, base_max=10),
            ),
            backstory="A fine backstory",
            char_class="",
            race="Dwarf",
        )


# ---------------------------------------------------------------------------
# Field validation
# ---------------------------------------------------------------------------


def test_nonblank_fields_validated():
    """Rust: nonblank_fields_validated — core name/description/personality."""
    from sidequest.game.creature_core import CreatureCore

    with pytest.raises(ValidationError):
        CreatureCore(
            name="",
            description="y",
            personality="z",
            inventory=Inventory(),
            statuses=[],
            hp=HpPool(current=10, max=10, base_max=10),
        )
    with pytest.raises(ValidationError):
        CreatureCore(
            name="x",
            description="   ",
            personality="z",
            inventory=Inventory(),
            statuses=[],
            hp=HpPool(current=10, max=10, base_max=10),
        )
    # valid
    cc = CreatureCore(
        name="valid",
        description="desc",
        personality="calm",
        inventory=Inventory(),
        statuses=[],
        hp=HpPool(current=10, max=10, base_max=10),
    )
    assert cc.name == "valid"


# ---------------------------------------------------------------------------
# Optional/deferred fields
# ---------------------------------------------------------------------------


def test_affinities_default_empty():
    c = make_test_character()
    assert c.affinities == []


def test_known_facts_default_empty():
    c = make_test_character()
    assert c.known_facts == []


def test_abilities_default_empty():
    c = make_test_character()
    assert c.abilities == []


def test_is_friendly_default_true():
    c = make_test_character()
    assert c.is_friendly is True


def test_resolved_archetype_optional():
    c = make_test_character()
    assert c.resolved_archetype is None


def test_character_with_known_facts():
    c = make_test_character()
    c.known_facts.append(KnownFact(content="The Warden is in the mines"))
    assert len(c.known_facts) == 1
    json_str = c.model_dump_json()
    back = Character.model_validate_json(json_str)
    assert back.known_facts[0].content == "The Warden is in the mines"


def test_character_with_abilities():
    c = make_test_character()
    c.abilities.append(
        AbilityDefinition(
            name="Iron Will",
            genre_description="An iron resolve that resists breaking",
            mechanical_effect="Reduce edge damage by 1",
            source=AbilitySource.Class,
        )
    )
    assert len(c.abilities) == 1
    json_str = c.model_dump_json()
    back = Character.model_validate_json(json_str)
    assert back.abilities[0].name == "Iron Will"


def test_edge_fraction_full():
    c = make_test_character()
    assert c.hp_fraction() == 1.0


def test_edge_fraction_half():
    c = make_test_character()
    c.core.hp.current = c.core.hp.max // 2
    assert abs(c.hp_fraction() - 0.5) < 0.01


def test_edge_fraction_zero_max_returns_zero():
    """Rust-verbatim: ``Combatant::edge_fraction`` returns ``0.0`` when
    ``max_edge == 0`` (NOT ``1.0``). Drift fixed in story 42-1."""
    c = make_test_character()
    c.core.hp.max = 0
    c.core.hp.current = 0
    assert c.hp_fraction() == 0.0
