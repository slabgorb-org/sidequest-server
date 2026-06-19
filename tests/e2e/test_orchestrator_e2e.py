"""End-to-end wiring test — narrator turn with Caverns & Claudes genre pack.

Wiring gate: verifies the full narration pipeline is connected end-to-end:
  load_genre_pack → build TurnContext → Orchestrator.run_narration_turn
  → NarrationTurnResult with narration text and extracted game_patch fields.

No live Claude CLI calls — subprocess is mocked via ClaudeClient(spawn_fn=...).
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from sidequest.agents.claude_client import ClaudeClient
from sidequest.agents.orchestrator import (
    NarrationTurnResult,
    Orchestrator,
    TurnContext,
)
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.session import GameSnapshot
from sidequest.genre.loader import DEFAULT_GENRE_PACK_SEARCH_PATHS, load_genre_pack

# ---------------------------------------------------------------------------
# Locate Caverns & Claudes genre pack
# ---------------------------------------------------------------------------


def _find_caverns_pack_dir() -> pathlib.Path | None:
    """Search for the caverns_and_claudes genre pack.

    Checks DEFAULT_GENRE_PACK_SEARCH_PATHS first, then falls back to the
    well-known absolute path for the oq-1 checkout.
    """
    for base in DEFAULT_GENRE_PACK_SEARCH_PATHS:
        candidate = base / "caverns_and_claudes"
        if candidate.is_dir():
            return candidate

    # Absolute fallback for the oq-1 checkout structure
    abs_candidate = pathlib.Path(
        "/Users/keithavery/Projects/oq-1/sidequest-content/genre_packs/caverns_and_claudes"
    )
    if abs_candidate.is_dir():
        return abs_candidate

    return None


CAVERNS_PACK_DIR = _find_caverns_pack_dir()
SKIP_REASON = "caverns_and_claudes genre pack not found in search paths"


# ---------------------------------------------------------------------------
# Fake subprocess helpers
# ---------------------------------------------------------------------------


class FakeProcess:
    def __init__(self, stdout: bytes, returncode: int = 0) -> None:
        self._stdout = stdout
        self._stderr = b""
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    def kill(self) -> None:
        pass

    async def wait(self) -> int:
        return self.returncode


def make_canned_narrator_spawn(narration_text: str, session_id: str = "e2e-session-001"):
    """Return a spawn_fn that produces the given canned narration response."""

    async def spawn_fn(command: str, *args: str, env: Any = None, **kwargs: Any) -> FakeProcess:
        payload = {
            "result": narration_text,
            "session_id": session_id,
            "usage": {"input_tokens": 150, "output_tokens": 80},
        }
        return FakeProcess(stdout=json.dumps(payload).encode())

    return spawn_fn


# ---------------------------------------------------------------------------
# Test session builder
# ---------------------------------------------------------------------------


def build_minimal_test_session(genre_slug: str = "caverns_and_claudes") -> GameSnapshot:
    """Build a minimal GameSnapshot for testing."""
    core = CreatureCore(
        name="Kael",
        description="A weathered adventurer",
        personality="Cautious",
        level=1,
        xp=0,
        inventory=Inventory(),
        statuses=[],
        hp=HpPool(current=10, max=10, base_max=10),
    )
    character = Character(
        core=core,
        backstory="A former soldier turned treasure hunter.",
        char_class="Fighter",
        race="Human",
        is_friendly=True,
    )
    return GameSnapshot(
        genre_slug=genre_slug,
        world_slug="base",
        characters=[character],
        location="The Entrance Hall",
        time_of_day="dusk",
        atmosphere="foreboding",
        current_region="dungeon",
    )


# ---------------------------------------------------------------------------
# Wiring gate test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.skipif(CAVERNS_PACK_DIR is None, reason=SKIP_REASON)
async def test_narrator_turn_end_to_end_with_caverns_claudes():
    """Wiring gate: player action → narrator → NARRATION content + game_patch emitted.

    This test exercises the full Phase 1 pipeline:
      1. Load the caverns_and_claudes genre pack
      2. Build a minimal test session (GameSnapshot)
      3. Call run_narration_turn() with a mocked ClaudeClient
      4. Assert narration text is present and game_patch fields are extracted

    Per CLAUDE.md "Every Test Suite Needs a Wiring Test" — this verifies
    run_narration_turn is importable, callable, and returns the expected shape.
    """
    assert CAVERNS_PACK_DIR is not None

    pack = load_genre_pack(CAVERNS_PACK_DIR)
    # Genre slug is the directory name (not stored in PackMeta)
    genre_slug = CAVERNS_PACK_DIR.name
    session = build_minimal_test_session(genre_slug)

    canned_narration = (
        "**The Entrance Hall**\n\n"
        "Torchlight flickers across stone walls slick with moisture. "
        "To the north, a passage descends into darkness. "
        "Somewhere below, something drips.\n\n"
        "```game_patch\n"
        '{"location": "The Entrance Hall", '
        '"footnotes": [{"summary": "The dungeon entrance is cold and damp", "category": "Place", "is_new": true}], '
        '"visual_scene": {"subject": "Stone corridor lit by torchlight", "tier": "landscape", "mood": "ominous", "tags": ["location"]}}\n'
        "```"
    )

    client = ClaudeClient(spawn_fn=make_canned_narrator_spawn(canned_narration))

    context = TurnContext(
        character_name="Kael",
        genre=genre_slug,
        genre_prompts=pack.prompts,
        current_location=session.location or "The Entrance Hall",
        state_summary="{}",
    )
    orch = Orchestrator(client=client)
    result = await orch.run_narration_turn("look around", context)

    # Core narration assertions
    assert isinstance(result, NarrationTurnResult)
    assert result.narration, "narration must be non-empty"
    assert (
        "The Entrance Hall" in result.narration
        or "torchlight" in result.narration.lower()
        or result.narration.strip()
    )
    assert not result.is_degraded

    # game_patch extraction assertions. location is tool-owned and still flows.
    assert result.location == "The Entrance Hall"
    # Story 151-5 (ADR-150 step 4, cutover II): footnotes + visual_scene (with
    # npcs_present + scene_mood) are RETIRED from the narrator game_patch — the
    # post-narration extractor produces them now and the WS handler's merge seams
    # source them onto the result, AFTER this orchestrator turn. They surface empty
    # at the orchestrator level.
    assert result.footnotes == []
    assert result.visual_scene is None

    # OTEL / telemetry fields
    assert result.agent_name == "narrator"
    assert result.agent_duration_ms is not None
    assert result.token_count_in == 150
    assert result.token_count_out == 80
    assert result.prompt_text is not None
    assert result.raw_response_text is not None

    # Genre pack was applied — narrator voice from prompts.yaml was injected
    assert pack.prompts is not None
    assert pack.prompts.narrator, "caverns_and_claudes must have narrator prompt"
    # Verify genre voice appears in the assembled prompt
    assert pack.prompts.narrator in result.prompt_text


@pytest.mark.asyncio
@pytest.mark.skipif(CAVERNS_PACK_DIR is None, reason=SKIP_REASON)
async def test_narrator_turn_e2e_combat_rules_injected_when_in_encounter():
    """Wiring gate: encounter rules appear in prompt when in_encounter=True."""
    assert CAVERNS_PACK_DIR is not None

    pack = load_genre_pack(CAVERNS_PACK_DIR)
    genre_slug = CAVERNS_PACK_DIR.name
    canned = (
        "**The Chamber**\n\nA goblin lunges!\n\n"
        '```game_patch\n{"beat_selections": [{"actor": "Kael", "beat_id": "attack", "target": "Goblin"}]}\n```'
    )
    client = ClaudeClient(spawn_fn=make_canned_narrator_spawn(canned))

    context = TurnContext(
        character_name="Kael",
        genre=genre_slug,
        genre_prompts=pack.prompts,
        in_combat=True,
        in_encounter=True,
        current_location="The Chamber",
        state_summary="{}",
    )
    orch = Orchestrator(client=client)
    result = await orch.run_narration_turn("attack the goblin", context)

    # Encounter rules were injected
    assert result.prompt_text is not None
    assert "COMBAT NARRATION RULES" in result.prompt_text

    # Beat selections extracted
    assert len(result.beat_selections) == 1
    assert result.beat_selections[0].actor == "Kael"
    assert result.beat_selections[0].beat_id == "attack"


@pytest.mark.asyncio
@pytest.mark.skipif(CAVERNS_PACK_DIR is None, reason=SKIP_REASON)
async def test_narrator_turn_e2e_degraded_on_claude_failure():
    """Wiring gate: ADR-005 graceful degradation — Claude failure → degraded result."""
    assert CAVERNS_PACK_DIR is not None

    pack = load_genre_pack(CAVERNS_PACK_DIR)
    genre_slug = CAVERNS_PACK_DIR.name
    session = build_minimal_test_session(genre_slug)

    async def failing_spawn(command: str, *args: str, **kwargs: Any) -> Any:
        raise RuntimeError("Simulated Claude CLI failure")

    client = ClaudeClient(spawn_fn=failing_spawn)

    context = TurnContext(
        character_name="Kael",
        genre=genre_slug,
        genre_prompts=pack.prompts,
        current_location=session.location or "The Entrance Hall",
        state_summary="{}",
    )
    orch = Orchestrator(client=client)
    result = await orch.run_narration_turn("look around", context)

    assert result.is_degraded
    assert result.narration  # degraded narration is still present
    assert "The Entrance Hall" in result.narration or result.narration.strip()
