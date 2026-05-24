"""Narrator pre-prompt includes magic context when state is present.

Adapted from plan lines 3831-3866: the real prompt-assembly surface is
``Orchestrator.build_narrator_prompt`` (async method), not a standalone
``build_narrator_prompt`` function. Tests use TurnContext.magic_state to
inject state without going through the full GameSnapshot convenience wrapper.
"""

from __future__ import annotations

import json
from typing import Any

from sidequest.agents.claude_client import ClaudeClient
from sidequest.agents.narrator import NARRATOR_OUTPUT_ONLY
from sidequest.agents.orchestrator import Orchestrator, TurnContext
from sidequest.magic.models import (
    HardLimit,
    LedgerBarSpec,
    WorldKnowledge,
    WorldMagicConfig,
)
from sidequest.magic.state import MagicState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_world_config() -> WorldMagicConfig:
    """Minimal Coyote Star config sufficient for context_builder tests."""
    return WorldMagicConfig(
        world_slug="coyote_star",
        genre_slug="space_opera",
        allowed_sources=["innate", "item_based"],
        active_plugins=["innate_v1", "item_legacy_v1"],
        intensity=0.25,
        world_knowledge=WorldKnowledge(primary="classified", local_register="folkloric"),
        visibility={"primary": "feared", "local_register": "dismissed"},
        hard_limits=[
            HardLimit(id="psionics_never_decisive", description="no decisive psionic outcomes")
        ],
        cost_types=["sanity", "notice"],
        ledger_bars=[
            LedgerBarSpec(
                id="sanity",
                scope="character",
                direction="down",
                range=(0.0, 1.0),
                threshold_low=0.40,
                consequence_on_low_cross="auto-fire The Bleeding-Through",
                starts_at_chargen=1.0,
            ),
        ],
        narrator_register="feared and folkloric",
    )


def _make_canned_client() -> ClaudeClient:
    """ClaudeClient whose subprocess always returns a minimal canned response."""

    async def spawn_fn(command: str, *args: str, env: Any = None, **kwargs: Any):
        class FakeProcess:
            returncode = 0

            async def communicate(self):
                payload = {
                    "result": "**The Silence**\n\nNothing stirs.\n\n```game_patch\n{}\n```",
                    "session_id": "test-session-001",
                    "usage": {"input_tokens": 10, "output_tokens": 20},
                }
                return json.dumps(payload).encode(), b""

            def kill(self):
                pass

            async def wait(self):
                return 0

        return FakeProcess()

    return ClaudeClient(spawn_fn=spawn_fn)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_narrator_pre_prompt_contains_magic_context_when_state_present():
    """build_narrator_prompt includes the magic block when TurnContext.magic_state is set."""
    config = _make_world_config()
    state = MagicState.from_config(config)
    state.add_character("sira_mendes")

    orch = Orchestrator(client=_make_canned_client())
    context = TurnContext(
        character_name="sira_mendes",
        magic_state=state,
    )
    prompt, _ = await orch.build_narrator_prompt("reach out with your mind", context)
    assert "ACTIVE MAGIC CONTEXT" in prompt
    assert "allowed_sources" in prompt


async def test_narrator_pre_prompt_omits_magic_context_when_state_absent():
    """build_narrator_prompt omits the magic block when TurnContext.magic_state is None."""
    orch = Orchestrator(client=_make_canned_client())
    context = TurnContext(
        character_name="kael",
        magic_state=None,
    )
    prompt, _ = await orch.build_narrator_prompt("look around", context)
    assert "ACTIVE MAGIC CONTEXT" not in prompt


def test_narrator_output_doc_mentions_magic_working():
    """NARRATOR_OUTPUT_ONLY documents magic-effect recording.

    Story 61-9 / ADR-101 amendment: the SDK narrator records magic
    workings by CALLING ``apply_spell_effect`` (a tool); the legacy
    ``magic_working`` sidecar field is gone. The CRITICAL MAGIC RULE
    and the innate-magic plugin gate must still appear in the prose so
    the narrator gates the tool call on plugin state.
    """
    assert "apply_spell_effect" in NARRATOR_OUTPUT_ONLY
    assert "CRITICAL MAGIC RULE" in NARRATOR_OUTPUT_ONLY
    assert "innate_v1" in NARRATOR_OUTPUT_ONLY
