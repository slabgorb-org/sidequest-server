"""Group C end-to-end: a turn with a PC at zero hp produces paired
must_narrate / must_not_narrate directives in the captured narrator prompt,
and the policy's tone text is the one that shows up — proving the per-pack
lethality_policy.yaml routes all the way through to the narrator.

The narrator's LLM call is canned; the assertion is on the prompt the
orchestrator would send, retrieved from Orchestrator.build_narrator_prompt
(the same call that _execute_narration_turn makes downstream of the real
session handler).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sidequest.agents.claude_client import ClaudeClient
from sidequest.agents.orchestrator import (
    Orchestrator,
)
from sidequest.game.character import Character
from sidequest.game.creature_core import (
    CreatureCore,
    HpPool,
    Inventory,
)
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.loader import load_genre_pack
from sidequest.protocol.dispatch import DispatchPackage, PlayerDispatch
from sidequest.server.session_handler import _build_turn_context, _SessionData
from tests.agents.test_orchestrator import make_spawn_fn

pytestmark = pytest.mark.asyncio

CONTENT_GENRE_PACKS = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


def _character(name: str, edge_current: int) -> Character:
    core = CreatureCore(
        name=name,
        description="d",
        personality="p",
        inventory=Inventory(),
        hp=HpPool(current=edge_current, max=10, base_max=10),
    )
    return Character(
        core=core,
        backstory="A test hero.",
        char_class="Delver",
        race="Human",
    )


def _snapshot(genre_slug: str, world_slug: str, character: Character) -> GameSnapshot:
    return GameSnapshot(
        genre_slug=genre_slug,
        world_slug=world_slug,
        location="Test location",
        turn_manager=TurnManager(interaction=1),
        characters=[character],
    )


def _session(genre_slug: str, world_slug: str, character: Character) -> _SessionData:
    return _SessionData(
        genre_slug=genre_slug,
        world_slug=world_slug,
        player_name="Alice",
        player_id="player:alice",
        snapshot=_snapshot(genre_slug, world_slug, character),
        store=MagicMock(),
        genre_pack=load_genre_pack(CONTENT_GENRE_PACKS / genre_slug),
        orchestrator=MagicMock(),
    )


def _dispatch_package() -> DispatchPackage:
    """Group B decomposer output — empty but valid (arbiter fills lethality)."""
    return DispatchPackage(
        turn_id="t1",
        per_player=[PlayerDispatch(player_id="player:alice", raw_action="swing")],
        cross_player=[],
        confidence_global=1.0,
    )


async def test_zero_edge_pc_in_mutant_wasteland_injects_permadeath_directives():
    """mutant_wasteland policy text ('wasteland is indifferent') reaches the prompt."""
    sd = _session("mutant_wasteland", "flickering_reach", _character("Alice", edge_current=0))
    ctx = _build_turn_context(sd)
    ctx.dispatch_package = _dispatch_package()

    orch = Orchestrator(client=ClaudeClient(spawn_fn=make_spawn_fn("narration")))
    prompt, _ = await orch.build_narrator_prompt(
        "block the beast",
        ctx,
    )

    assert "must_narrate" in prompt
    assert "must_not_narrate" in prompt
    # Per-pack must_narrate surfaces:
    assert "wasteland is indifferent" in prompt or "genre-true terms" in prompt
    # Per-pack must_not_narrate surfaces:
    assert "miraculous rescues" in prompt


async def test_zero_edge_pc_in_caverns_injects_comedic_directives():
    """caverns_and_claudes policy text ('slapstick') reaches the prompt."""
    sd = _session("caverns_and_claudes", "mawdeep", _character("Alice", edge_current=0))
    ctx = _build_turn_context(sd)
    ctx.dispatch_package = _dispatch_package()

    orch = Orchestrator(client=ClaudeClient(spawn_fn=make_spawn_fn("narration")))
    prompt, _ = await orch.build_narrator_prompt(
        "retreat",
        ctx,
    )
    # Comedic verdict — "humiliated" — with one-liner + slapstick cues:
    assert "one-liner" in prompt or "slapstick" in prompt
    # Must-not text surfaces too:
    assert "permadeath" in prompt or "eulogy" in prompt


async def test_no_lethality_directives_when_character_above_zero_edge():
    """Healthy PC — policy is loaded but the arbiter fires zero verdicts."""
    pass  # no longer importing placeholder_edge_pool (ADR-114)
    sd = _session("mutant_wasteland", "flickering_reach", _character("Alice", edge_current=7))
    ctx = _build_turn_context(sd)
    ctx.dispatch_package = _dispatch_package()

    orch = Orchestrator(client=ClaudeClient(spawn_fn=make_spawn_fn("narration")))
    prompt, _ = await orch.build_narrator_prompt(
        "explore",
        ctx,
    )
    assert "wasteland is indifferent" not in prompt
    assert "miraculous rescues" not in prompt


async def test_adventurer_fallback_name_flows_through_turn_context():
    """When the world-materialization fallback names the PC 'Adventurer'
    (no chargen-supplied name reached the snapshot), `_build_turn_context`
    must surface that literal name as ``character_name`` rather than
    silently substituting another value. Pins Story 45-4's
    ``ChapterCharacter.name`` empty-string semantics end-to-end through
    the narrator-context layer.
    """
    sd = _SessionData(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        player_name="Adventurer",
        player_id="player:adventurer",
        snapshot=GameSnapshot(
            genre_slug="caverns_and_claudes",
            world_slug="mawdeep",
            location="Test",
            turn_manager=TurnManager(interaction=1),
            characters=[_character("Adventurer", edge_current=10)],
        ),
        store=MagicMock(),
        genre_pack=load_genre_pack(CONTENT_GENRE_PACKS / "caverns_and_claudes"),
        orchestrator=MagicMock(),
    )
    ctx = _build_turn_context(sd)
    assert ctx.character_name == "Adventurer"
