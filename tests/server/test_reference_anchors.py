"""URL builders for reference-page anchors.

The builders are pure: in -> URL string. They use the shared slugify so the
emitted URLs match the renderer's anchor ids exactly. Unknown pack/world is
not the builders' concern — they're called only after the caller knows the
session's pack/world are loaded.
"""

from __future__ import annotations

import pytest

from sidequest.server.reference_anchors import (
    build_lore_url,
    build_rules_url,
    reference_url_for_ability,
    reference_url_for_class,
    reference_url_for_journal_entry,
    reference_url_for_location_entity,
)


def test_build_rules_url_class() -> None:
    assert build_rules_url("tea_and_murder", "class", "Burglar") == (
        "/reference/rules/tea_and_murder#class-burglar"
    )


def test_build_rules_url_class_signature() -> None:
    assert build_rules_url("tea_and_murder", "class", "Burglar", "signature", "Cosh & Run") == (
        "/reference/rules/tea_and_murder#class-burglar-signature-cosh-run"
    )


def test_build_lore_url_culture() -> None:
    assert build_lore_url("tea_and_murder", "glenross", "culture", "Thornberry") == (
        "/reference/lore/tea_and_murder/glenross#culture-thornberry"
    )


def test_build_lore_url_legend() -> None:
    assert build_lore_url("tea_and_murder", "glenross", "legend", "The Rending") == (
        "/reference/lore/tea_and_murder/glenross#legend-the-rending"
    )


def test_reference_url_for_class_ability_returns_url() -> None:
    url = reference_url_for_ability(
        pack="tea_and_murder",
        source="Class",
        ability_name="Cosh",
        owning_class_name="Burglar",
    )
    assert url == "/reference/rules/tea_and_murder#class-burglar-signature-cosh"


def test_reference_url_for_non_class_ability_returns_none() -> None:
    assert (
        reference_url_for_ability(
            pack="tea_and_murder",
            source="Race",
            ability_name="Keen Senses",
            owning_class_name=None,
        )
        is None
    )


def test_reference_url_for_class_ability_without_owner_returns_none() -> None:
    """If we don't know which class owns the ability, we cannot link."""
    assert (
        reference_url_for_ability(
            pack="tea_and_murder",
            source="Class",
            ability_name="Cosh",
            owning_class_name=None,
        )
        is None
    )


def test_reference_url_for_class() -> None:
    assert reference_url_for_class(pack="tea_and_murder", class_name="Burglar") == (
        "/reference/rules/tea_and_murder#class-burglar"
    )


def test_reference_url_for_journal_entry_lore_dispatches_legend() -> None:
    """Lore category falls back to history once legends miss; here it hits legend."""
    url = reference_url_for_journal_entry(
        pack="tea_and_murder",
        world="glenross",
        category="Lore",
        content="The Rending",
        legend_names=("The Rending", "The Hollow Pact"),
        history_entries=(),
    )
    assert url == "/reference/lore/tea_and_murder/glenross#legend-the-rending"


def test_reference_url_for_journal_entry_lore_falls_back_to_history() -> None:
    url = reference_url_for_journal_entry(
        pack="tea_and_murder",
        world="glenross",
        category="Lore",
        content="Founding of Glenross",
        legend_names=(),
        history_entries=("Founding of Glenross",),
    )
    assert url == "/reference/lore/tea_and_murder/glenross#history-founding-of-glenross"


def test_reference_url_for_journal_entry_place() -> None:
    url = reference_url_for_journal_entry(
        pack="tea_and_murder",
        world="glenross",
        category="Place",
        content="The Vicarage",
        legend_names=(),
        history_entries=(),
        location_names=("The Vicarage",),
    )
    assert url == "/reference/lore/tea_and_murder/glenross#location-the-vicarage"


@pytest.mark.parametrize("category", ["Person", "Quest"])
def test_reference_url_for_journal_entry_person_quest_returns_none(category: str) -> None:
    """Person -> npcs.yaml is excluded; Quest has no rendered yaml. Plain text."""
    assert (
        reference_url_for_journal_entry(
            pack="tea_and_murder",
            world="glenross",
            category=category,
            content="Aunt Pemberton",
            legend_names=(),
            history_entries=(),
        )
        is None
    )


def test_reference_url_for_journal_entry_unknown_entity_returns_none() -> None:
    assert (
        reference_url_for_journal_entry(
            pack="tea_and_murder",
            world="glenross",
            category="Place",
            content="Nowhere",
            legend_names=(),
            history_entries=(),
            location_names=(),
        )
        is None
    )


def test_reference_url_for_location_entity_hits() -> None:
    url = reference_url_for_location_entity(
        pack="tea_and_murder",
        world="glenross",
        entity_name="The Vicarage",
        known_location_names=("The Vicarage",),
    )
    assert url == "/reference/lore/tea_and_murder/glenross#location-the-vicarage"


def test_reference_url_for_location_entity_miss_returns_none() -> None:
    assert (
        reference_url_for_location_entity(
            pack="tea_and_murder",
            world="glenross",
            entity_name="A Bush",
            known_location_names=("The Vicarage",),
        )
        is None
    )
