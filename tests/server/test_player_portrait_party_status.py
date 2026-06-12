"""party_member_from_character resolves a picked portrait_ref to its R2 URL.

This is the wiring test for the 2026-06-12 player-portrait fix: the stored
``Character.portrait_ref`` must surface as ``PartyMember.portrait_url`` through
the real emit path, not stay hardcoded ``None``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.persistence import GameMode
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.loader import load_genre_pack
from sidequest.server import views
from sidequest.server.asset_urls import resolve_player_portrait_url
from sidequest.server.session_handler import _SessionData

CONTENT_GENRE_PACKS = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


def _char(name: str, portrait_ref: str | None) -> Character:
    return Character(
        core=CreatureCore(
            name=name,
            description="d",
            personality="p",
            inventory=Inventory(),
            hp=HpPool(current=10, max=10, base_max=10),
        ),
        backstory=f"{name}'s tale.",
        char_class="Delver",
        race="Human",
        portrait_ref=portrait_ref,
    )


def _sd(character: Character) -> _SessionData:
    return _SessionData(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        player_name="P",
        player_id="player:1",
        snapshot=GameSnapshot(
            genre_slug="caverns_and_claudes",
            world_slug="mawdeep",
            turn_manager=TurnManager(interaction=1),
            characters=[character],
        ),
        repository=MagicMock(),
        dungeon_repository=MagicMock(),
        telemetry_sink=MagicMock(),
        genre_pack=load_genre_pack(CONTENT_GENRE_PACKS / "caverns_and_claudes"),
        orchestrator=MagicMock(),
        mode=GameMode.MULTIPLAYER,
    )


def test_portrait_ref_resolves_to_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SIDEQUEST_ASSET_BASE_URL", raising=False)
    character = _char("Kael", "delver_human_a1")
    pm = views.party_member_from_character(
        MagicMock(), _sd(character), character, player_id="player:1", player_name="P"
    )
    assert pm.portrait_url == resolve_player_portrait_url(
        "caverns_and_claudes", "mawdeep", "delver_human_a1"
    )
    assert pm.portrait_url is not None


def test_no_portrait_ref_yields_none() -> None:
    character = _char("Kael", None)
    pm = views.party_member_from_character(
        MagicMock(), _sd(character), character, player_id="player:1", player_name="P"
    )
    assert pm.portrait_url is None


def test_rest_picker_url_matches_helper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The /api/chargen/portraits route builds each portrait_url via the same
    resolve_player_portrait_url helper as the PARTY_STATUS emit path — drive the
    real HTTP route and assert URL equality so the two can never drift."""
    monkeypatch.delenv("SIDEQUEST_ASSET_BASE_URL", raising=False)
    from fastapi.testclient import TestClient

    from sidequest.server.app import create_app

    genre, world = "caverns_and_claudes", "beneath_sunden"
    app = create_app(save_dir=tmp_path, genre_pack_search_paths=[CONTENT_GENRE_PACKS])
    client = TestClient(app)

    resp = client.get(f"/api/chargen/portraits/{genre}/{world}")
    assert resp.status_code == 200
    portraits = resp.json()["portraits"]
    assert portraits, "beneath_sunden should ship player_picker portraits"

    for entry in portraits:
        assert entry["portrait_url"] == resolve_player_portrait_url(
            genre, world, entry["slug"]
        )
