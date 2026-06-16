"""ADR-143 Task 11 — skills/foci appear in the PARTY_STATUS sheet dict.

DD-6: skills/foci ride members[].sheet (CharacterSheetDetails), NOT a new
top-level payload.  Two cases:

1. A WWN character with populated skills + foci — the sheet carries them.
2. A non-WWN / legacy character with empty skills + foci — the sheet carries
   empty collections, not None (protocol consistency).

The wiring chain exercised:
    Character.skills / Character.foci
    → party_member_from_character (views.py)
    → CharacterSheetDetails.skills / .foci
    → model_dump() — the wire shape the client state-mirror receives
"""

from __future__ import annotations

from unittest.mock import MagicMock

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.persistence import GameMode
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.server.session_handler import _SessionData
from sidequest.server.views import party_member_from_character


def _make_sd(character: Character) -> _SessionData:
    """Minimal synthetic _SessionData sufficient for party_member_from_character."""
    genre_pack = MagicMock()
    genre_pack.classes = []
    genre_pack.inventory = None
    genre_pack.worlds = {}
    genre_pack.rules.survivability_pool_label = None
    genre_pack.progression.wealth_tiers = []

    snapshot = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        turn_manager=TurnManager(interaction=1),
        characters=[character],
    )

    return _SessionData(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        player_name="Wiring Player",
        player_id="p1",
        snapshot=snapshot,
        repository=MagicMock(),
        dungeon_repository=MagicMock(),
        telemetry_sink=MagicMock(),
        genre_pack=genre_pack,
        orchestrator=MagicMock(),
        mode=GameMode.SOLO,
    )


def _make_character(
    *,
    skills: dict[str, int] | None = None,
    foci: list[str] | None = None,
) -> Character:
    return Character(
        core=CreatureCore(
            name="Aldren",
            description="A seasoned delver.",
            personality="Cautious.",
            inventory=Inventory(),
            hp=HpPool(current=8, max=10, base_max=10),
        ),
        backstory="Born in the caverns.",
        char_class="Warrior",
        race="Human",
        skills=skills if skills is not None else {},
        foci=foci if foci is not None else [],
    )


# ---------------------------------------------------------------------------
# Case 1: WWN character with populated skills + foci
# ---------------------------------------------------------------------------


def test_sheet_carries_skills_for_wwn_character() -> None:
    """party_member_from_character: populated skills appear in sheet dict."""
    character = _make_character(skills={"Sneak": 1, "Exert": 0}, foci=["Die Hard"])
    sd = _make_sd(character)

    member = party_member_from_character(
        MagicMock(),
        sd,
        character,
        player_id="p1",
        player_name="Wiring Player",
    )

    assert member.sheet is not None
    assert member.sheet.skills == {"Sneak": 1, "Exert": 0}, (
        f"Expected skills dict on sheet, got {member.sheet.skills!r}"
    )


def test_sheet_carries_foci_for_wwn_character() -> None:
    """party_member_from_character: populated foci appear in sheet dict."""
    character = _make_character(skills={"Sneak": 1}, foci=["Die Hard", "Alert"])
    sd = _make_sd(character)

    member = party_member_from_character(
        MagicMock(),
        sd,
        character,
        player_id="p1",
        player_name="Wiring Player",
    )

    assert member.sheet is not None
    assert member.sheet.foci == ["Die Hard", "Alert"], (
        f"Expected foci list on sheet, got {member.sheet.foci!r}"
    )


def test_serialized_sheet_carries_skills_and_foci() -> None:
    """model_dump() — the wire shape the WS state-mirror sends — carries
    skills and foci under sheet.skills / sheet.foci."""
    character = _make_character(skills={"Connect": 0, "Notice": 1}, foci=["Wanderer"])
    sd = _make_sd(character)

    member = party_member_from_character(
        MagicMock(),
        sd,
        character,
        player_id="p1",
        player_name="Wiring Player",
    )

    dumped = member.model_dump()
    sheet_data = dumped["sheet"]
    assert "skills" in sheet_data, f"'skills' key missing from serialized sheet: {sorted(sheet_data)}"
    assert "foci" in sheet_data, f"'foci' key missing from serialized sheet: {sorted(sheet_data)}"
    assert sheet_data["skills"] == {"Connect": 0, "Notice": 1}
    assert sheet_data["foci"] == ["Wanderer"]


# ---------------------------------------------------------------------------
# Case 2: non-WWN / legacy character — empty collections, not None
# ---------------------------------------------------------------------------


def test_sheet_skills_empty_for_non_wwn_character() -> None:
    """Non-WWN characters have empty skills dict — must be {} not None."""
    character = _make_character()  # default skills={}
    sd = _make_sd(character)

    member = party_member_from_character(
        MagicMock(),
        sd,
        character,
        player_id="p1",
        player_name="Wiring Player",
    )

    assert member.sheet is not None
    assert member.sheet.skills == {}, (
        f"Non-WWN sheet.skills must be empty dict, got {member.sheet.skills!r}"
    )


def test_sheet_foci_empty_for_non_wwn_character() -> None:
    """Non-WWN characters have empty foci list — must be [] not None."""
    character = _make_character()  # default foci=[]
    sd = _make_sd(character)

    member = party_member_from_character(
        MagicMock(),
        sd,
        character,
        player_id="p1",
        player_name="Wiring Player",
    )

    assert member.sheet is not None
    assert member.sheet.foci == [], (
        f"Non-WWN sheet.foci must be empty list, got {member.sheet.foci!r}"
    )
