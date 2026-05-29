"""Wiring: LethalityArbiter is actually invoked on the real session-handler path.

CLAUDE.md: "Every Test Suite Needs a Wiring Test." Tasks 5–13 each proved the
arbiter works in isolation or via the prompt-builder; this test spies on the
live class to confirm `_build_turn_context + Orchestrator.build_narrator_prompt`
(the same two calls `_execute_narration_turn` makes downstream of the real
session handler) actually invoke `LethalityArbiter.arbitrate`.

Does NOT mock the arbiter or the prompt builder. Mocks only the narrator's
Claude subprocess (to keep the test hermetic).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sidequest.agents.claude_client import ClaudeClient
from sidequest.agents.lethality_arbiter import LethalityArbiter
from sidequest.agents.orchestrator import (
    Orchestrator,
)
from sidequest.agents.subsystems import BankResult
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.loader import load_genre_pack
from sidequest.protocol.dispatch import DispatchPackage, PlayerDispatch
from sidequest.server.session_handler import _build_turn_context, _SessionData
from tests.agents.test_orchestrator import make_spawn_fn

pytestmark = pytest.mark.asyncio

CONTENT_GENRE_PACKS = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


async def test_arbiter_is_invoked_on_real_prompt_build_path():
    """Spy on LethalityArbiter.arbitrate — it must be called exactly once."""
    character = Character(
        core=CreatureCore(
            name="Alice",
            description="d",
            personality="p",
            inventory=Inventory(),
            hp=HpPool(current=0, max=10, base_max=10),
        ),
        backstory="A test hero.",
        char_class="Delver",
        race="Human",
    )
    sd = _SessionData(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        player_name="Alice",
        player_id="player:alice",
        snapshot=GameSnapshot(
            genre_slug="caverns_and_claudes",
            world_slug="mawdeep",
            location="Test",
            turn_manager=TurnManager(interaction=1),
            characters=[character],
        ),
        repository=MagicMock(),
        dungeon_repository=MagicMock(),
        telemetry_sink=MagicMock(),
        genre_pack=load_genre_pack(CONTENT_GENRE_PACKS / "caverns_and_claudes"),
        orchestrator=MagicMock(),
    )
    ctx = _build_turn_context(sd)
    ctx.dispatch_package = DispatchPackage(
        turn_id="t1",
        per_player=[PlayerDispatch(player_id="player:alice", raw_action="x")],
        cross_player=[],
        confidence_global=1.0,
    )
    ctx.bank_result = BankResult()

    original_arbitrate = LethalityArbiter.arbitrate
    calls: list[object] = []

    def _spy(self, **kwargs):
        calls.append(kwargs)
        return original_arbitrate(self, **kwargs)

    orch = Orchestrator(client=ClaudeClient(spawn_fn=make_spawn_fn("narration")))
    with patch.object(LethalityArbiter, "arbitrate", _spy):
        prompt, _ = await orch.build_narrator_prompt(
            "x",
            ctx,
        )

    assert len(calls) == 1, "arbiter was not invoked on the real prompt-build path"
    # And the arbiter's output ended up in the prompt — not just called but consumed.
    assert "must_narrate" in prompt
