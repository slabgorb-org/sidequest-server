"""The sheet projection carries Character.appearance to the wire (Story 126-5)."""
from sidequest.protocol.models import CharacterSheetDetails


def test_character_sheet_details_has_appearance_default():
    # Construct with the existing required fields plus appearance.
    sheet = CharacterSheetDetails(
        race="Human",
        stats={},
        abilities=[],
        backstory="An ex-ratcatcher.",
        personality="Wary",
        appearance="Tall, soot-stained, missing a tooth.",
    )
    assert sheet.appearance == "Tall, soot-stained, missing a tooth."


def test_character_sheet_details_appearance_defaults_empty():
    sheet = CharacterSheetDetails(
        race="Human", stats={}, abilities=[], backstory="x", personality="y",
    )
    assert sheet.appearance == ""
