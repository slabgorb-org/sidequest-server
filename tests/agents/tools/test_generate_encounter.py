"""Tests for the generate_encounter tool — Phase C Task 26.

GENERATE tool. The ``encountergen`` CLI exists at
``sidequest/cli/encountergen/encountergen.py`` with ``generate_enemy(...)``
plus a ``main(argv)`` entry, but its internals require RNG, filesystem
paths, and culture data not threaded through the tool boundary in v1.
The v1 tool reserves the namespace, records the narrator's request in
OTEL (``encountergen_wired=False``), and returns a fatal ToolResult.error
so the narrator cannot confabulate phantom combatants on an empty seed.
Tests cover the fail-loud behavior, validator boundaries, optional
hints, OTEL attribute emission, and anti-confabulation wiring.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

from sidequest.agents.narrator_perception_filter import NarratorPerceptionFilter
from sidequest.agents.tool_registry import (
    ToolContext,
    ToolResult,
    ToolResultStatus,
    default_registry,
)
from sidequest.agents.tooling_protocol import ToolUseBlock
from sidequest.agents.tools import generate_encounter as _generate_encounter_module  # noqa: F401
from sidequest.game.persistence import SqliteStore

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _store() -> SqliteStore:
    s = SqliteStore.open_in_memory()
    s.initialize()
    return s


def _make_ctx() -> ToolContext:
    return ToolContext(
        world_id="w",
        session_id="s",
        perspective_pc="Alice",
        turn_number=1,
        store=_store(),
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
    )


async def _call(arguments: dict, ctx: ToolContext) -> ToolResult:
    registered = default_registry._tools["generate_encounter"]
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, ctx)


def _otel(ctx: ToolContext) -> dict[str, Any]:
    span = cast(MagicMock, ctx.otel_span)
    return {call.args[0]: call.args[1] for call in span.set_attribute.call_args_list}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_generate_encounter_is_registered() -> None:
    assert "generate_encounter" in default_registry.list_names()


# ---------------------------------------------------------------------------
# Fail-loud behaviour — valid args must produce ERROR_FATAL, not phantom data
# ---------------------------------------------------------------------------


async def test_happy_path_returns_fatal_error_not_phantom_data() -> None:
    """Valid args must now yield a fatal error — no phantom combatants allowed."""
    ctx = _make_ctx()
    r = await _call(
        {
            "genre": "low_fantasy",
            "difficulty": 3,
            "terrain": "cavern",
            "theme": "ambush",
        },
        ctx,
    )

    assert r.status is ToolResultStatus.ERROR_FATAL
    assert r.payload is None
    assert r.message is not None
    assert len(r.message) > 0
    # Guard the anti-confabulation directive itself, not just message presence.
    assert "Do not narrate" in r.message


async def test_otel_attrs_set_on_happy_path() -> None:
    """OTEL attributes record narrator intent even though the call returns an error."""
    ctx = _make_ctx()
    await _call(
        {
            "genre": "neon_dystopia",
            "difficulty": 4,
            "terrain": "rooftop",
            "theme": "patrol",
        },
        ctx,
    )

    recorded = _otel(ctx)
    assert recorded["tool.encgen.genre"] == "neon_dystopia"
    assert recorded["tool.encgen.difficulty"] == 4
    assert recorded["tool.encgen.terrain"] == "rooftop"
    assert recorded["tool.encgen.theme"] == "patrol"
    assert recorded["tool.encgen.combatant_count"] == 0
    assert recorded["tool.encgen.encountergen_wired"] is False


async def test_optional_terrain_and_theme_otel_attrs_empty_string() -> None:
    """Omitted optional hints produce empty-string OTEL attrs; result is still fatal error."""
    ctx = _make_ctx()
    r = await _call({"genre": "space_opera"}, ctx)

    assert r.status is ToolResultStatus.ERROR_FATAL
    assert r.payload is None

    recorded = _otel(ctx)
    assert recorded["tool.encgen.terrain"] == ""
    assert recorded["tool.encgen.theme"] == ""
    assert recorded["tool.encgen.genre"] == "space_opera"
    assert recorded["tool.encgen.difficulty"] == 2


# ---------------------------------------------------------------------------
# Difficulty boundary — all five tiers reach the handler (which errors fatally)
# ---------------------------------------------------------------------------


async def test_all_five_difficulty_tiers_accepted() -> None:
    """All five tiers pass validator; each reaches the handler and returns ERROR_FATAL."""
    for difficulty in (1, 2, 3, 4, 5):
        ctx = _make_ctx()
        r = await _call({"genre": "low_fantasy", "difficulty": difficulty}, ctx)
        assert r.status is ToolResultStatus.ERROR_FATAL


# ---------------------------------------------------------------------------
# Anti-confabulation wiring test — dispatch path produces model-visible hard error
# ---------------------------------------------------------------------------


async def test_valid_call_dispatched_through_registry_yields_is_error_true() -> None:
    """Prove that a valid call reaching the real handler surfaces as is_error=True.

    This is the mandatory wiring test: the narrator physically cannot receive
    a phantom-success result from generate_encounter.
    """
    ctx = _make_ctx()
    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-anti-confab",
            name="generate_encounter",
            arguments={"genre": "low_fantasy", "difficulty": 2},
        ),
        ctx,
    )
    assert out.is_error is True
    assert out.content.startswith("ERROR:")


async def test_difficulty_zero_is_validator_error() -> None:
    ctx = _make_ctx()
    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-diff-zero",
            name="generate_encounter",
            arguments={"genre": "low_fantasy", "difficulty": 0},
        ),
        ctx,
    )
    assert out.is_error is True
    assert "argument validation failed" in out.content


async def test_difficulty_six_is_validator_error() -> None:
    ctx = _make_ctx()
    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-diff-six",
            name="generate_encounter",
            arguments={"genre": "low_fantasy", "difficulty": 6},
        ),
        ctx,
    )
    assert out.is_error is True
    assert "argument validation failed" in out.content


async def test_empty_genre_is_validator_error() -> None:
    ctx = _make_ctx()
    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-empty-genre",
            name="generate_encounter",
            arguments={"genre": "", "difficulty": 2},
        ),
        ctx,
    )
    assert out.is_error is True
    assert "argument validation failed" in out.content
