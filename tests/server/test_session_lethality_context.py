"""Wiring: session handler loads genre pack's lethality_policy onto TurnContext.

Group C Task 11. Confirms the bridge between GenrePack (Task 4) and
Orchestrator.build_narrator_prompt (Task 10) actually runs on the real
session-handler code path — not just unit-tested in isolation.

CLAUDE.md: "Every Test Suite Needs a Wiring Test."
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.loader import load_genre_pack
from sidequest.server.session_handler import _build_turn_context, _SessionData

CONTENT_GENRE_PACKS = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


def test_build_turn_context_populates_lethality_policy_from_pack():
    """A real caverns pack → TurnContext.lethality_policy is the pack's policy."""
    pack = load_genre_pack(CONTENT_GENRE_PACKS / "caverns_and_claudes")
    assert pack.lethality_policy is not None  # Task 4 wired it.

    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="sunken_keep",
        location="Main Hall",
        turn_manager=TurnManager(interaction=1),
    )
    sd = _SessionData(
        genre_slug="caverns_and_claudes",
        world_slug="sunken_keep",
        player_name="TestHero",
        player_id="player:TestHero",
        snapshot=snap,
        store=MagicMock(),
        genre_pack=pack,
        orchestrator=MagicMock(),
    )

    ctx = _build_turn_context(sd)

    assert ctx.lethality_policy is not None
    assert ctx.lethality_policy.genre_key == "caverns_and_claudes"
    assert ctx.lethality_policy.verdicts_on_zero_hp.pc == "humiliated"


def test_build_turn_context_populates_empty_cores_when_no_pcs_or_npcs():
    """No characters, no NPCs → empty dicts (not None), arbiter becomes a no-op."""
    pack = load_genre_pack(CONTENT_GENRE_PACKS / "caverns_and_claudes")
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="sunken_keep",
        location="Main Hall",
        turn_manager=TurnManager(interaction=1),
    )
    sd = _SessionData(
        genre_slug="caverns_and_claudes",
        world_slug="sunken_keep",
        player_name="TestHero",
        player_id="player:TestHero",
        snapshot=snap,
        store=MagicMock(),
        genre_pack=pack,
        orchestrator=MagicMock(),
    )
    ctx = _build_turn_context(sd)
    assert ctx.pc_cores_by_player == {}
    assert ctx.npc_cores_by_name == {}
