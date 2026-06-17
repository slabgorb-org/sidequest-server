"""Fate aspects reach the player sheet (Deliverable B1). Gated on fate_sheet
presence, never on ruleset string."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.fate_sheet import Aspect, FateSheet
from sidequest.game.persistence import GameMode
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.loader import load_genre_pack
from sidequest.protocol.models import CharacterSheetDetails, FateAspectEntry
from sidequest.server import views
from sidequest.server.session_handler import WebSocketSessionHandler, _SessionData

CONTENT_GENRE_PACKS = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


def test_sheet_carries_fate_aspects():
    sheet = CharacterSheetDetails(
        race="Investigator",
        stats={},
        abilities=[],
        backstory="x",
        personality="y",
        fate_aspects=[
            FateAspectEntry(text="Disgraced Pinkerton With a Long Memory", kind="high_concept"),
            FateAspectEntry(text="Can't Leave a Mystery Alone", kind="trouble"),
        ],
    )
    assert [a.kind for a in sheet.fate_aspects] == ["high_concept", "trouble"]


def test_sheet_fate_aspects_default_empty():
    sheet = CharacterSheetDetails(
        race="Human",
        stats={},
        abilities=[],
        backstory="x",
        personality="y",
    )
    assert sheet.fate_aspects == []


# ---------------------------------------------------------------------------
# Wiring test: the REAL views.party_member_from_character projection surfaces
# fate aspects, gated ONLY on fate_sheet presence (never on ruleset string).
# Note the synthetic session binds the caverns_and_claudes (WWN) pack — NOT a
# Fate pack — so a passing assertion proves the gate keys on fate_sheet, not on
# the bound ruleset.
# ---------------------------------------------------------------------------


def _char(name: str, *, fate_sheet: FateSheet | None = None) -> Character:
    return Character(
        core=CreatureCore(
            name=name,
            description="d",
            personality="p",
            inventory=Inventory(),
            hp=HpPool(current=10, max=10, base_max=10),
            fate_sheet=fate_sheet,
        ),
        backstory=f"{name}'s tale.",
        char_class="Delver",
        race="Human",
    )


def _sd(player_id: str, player_name: str, characters: list[Character]) -> _SessionData:
    return _SessionData(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        player_name=player_name,
        player_id=player_id,
        snapshot=GameSnapshot(
            genre_slug="caverns_and_claudes",
            world_slug="mawdeep",
            turn_manager=TurnManager(interaction=1),
            characters=list(characters),
        ),
        repository=MagicMock(),
        dungeon_repository=MagicMock(),
        telemetry_sink=MagicMock(),
        genre_pack=load_genre_pack(CONTENT_GENRE_PACKS / "caverns_and_claudes"),
        orchestrator=MagicMock(),
        mode=GameMode.MULTIPLAYER,
    )


def test_party_member_projection_surfaces_fate_aspects() -> None:
    sheet = FateSheet(skills={"Fight": 3})
    sheet.aspects.append(Aspect(text="Disgraced Pinkerton With a Long Memory", kind="high_concept"))
    sheet.aspects.append(Aspect(text="Can't Leave a Mystery Alone", kind="trouble"))
    pc = _char("Vance", fate_sheet=sheet)
    sd = _sd("p:vance", "Vance", [pc])

    handler = WebSocketSessionHandler(save_dir=Path("/tmp/sq-test-saves"))
    member = views.party_member_from_character(handler, sd, pc, "p:vance", "Vance")

    assert member.sheet is not None
    assert [(a.text, a.kind) for a in member.sheet.fate_aspects] == [
        ("Disgraced Pinkerton With a Long Memory", "high_concept"),
        ("Can't Leave a Mystery Alone", "trouble"),
    ]


def test_party_member_projection_empty_without_fate_sheet() -> None:
    pc = _char("Solo", fate_sheet=None)
    sd = _sd("p:solo", "Solo", [pc])

    handler = WebSocketSessionHandler(save_dir=Path("/tmp/sq-test-saves"))
    member = views.party_member_from_character(handler, sd, pc, "p:solo", "Solo")

    assert member.sheet is not None
    assert member.sheet.fate_aspects == []
