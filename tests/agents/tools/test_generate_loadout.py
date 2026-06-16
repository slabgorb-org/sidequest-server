"""Tests for the generate_loadout tool — Phase C Task 25.

GENERATE tool. ``sidequest.cli.loadoutgen`` is a placeholder per ADR-082
— the Python port hasn't ported the Rust prototype's loadoutgen CLI
yet. The v1 tool reserves the namespace, records the narrator's
request in OTEL (``loadoutgen_wired=False``), and returns a fatal
ToolResult.error so the narrator cannot confabulate phantom items on
an empty loadout. Tests cover the fail-loud behavior, validator
boundaries, OTEL attribute emission, and anti-confabulation wiring.
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
from sidequest.agents.tools import generate_loadout as _generate_loadout_module  # noqa: F401
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
    registered = default_registry._tools["generate_loadout"]
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, ctx)


def _otel(ctx: ToolContext) -> dict[str, Any]:
    span = cast(MagicMock, ctx.otel_span)
    return {call.args[0]: call.args[1] for call in span.set_attribute.call_args_list}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_generate_loadout_is_registered() -> None:
    assert "generate_loadout" in default_registry.list_names()


# ---------------------------------------------------------------------------
# Fail-loud behaviour — valid args must produce ERROR_FATAL, not phantom data
# ---------------------------------------------------------------------------


async def test_happy_path_returns_fatal_error_not_phantom_data() -> None:
    """Valid args must now yield a fatal error — no phantom items allowed."""
    ctx = _make_ctx()
    r = await _call({"archetype": "fighter", "tier": 2, "genre": "low_fantasy"}, ctx)

    assert r.status is ToolResultStatus.ERROR_FATAL
    assert r.payload is None
    assert r.message is not None
    assert len(r.message) > 0
    # Guard the anti-confabulation directive itself, not just message presence.
    assert "Do not narrate" in r.message


async def test_otel_attrs_set_on_happy_path() -> None:
    """OTEL attributes record narrator intent even though the call returns an error."""
    ctx = _make_ctx()
    await _call({"archetype": "rogue", "tier": 3, "genre": "neon_dystopia"}, ctx)

    recorded = _otel(ctx)
    assert recorded["tool.loadout.archetype"] == "rogue"
    assert recorded["tool.loadout.tier"] == 3
    assert recorded["tool.loadout.genre"] == "neon_dystopia"
    assert recorded["tool.loadout.item_count"] == 0
    assert recorded["tool.loadout.loadoutgen_wired"] is False


async def test_optional_genre_otel_attr_is_empty_string() -> None:
    """Omitted genre produces empty-string OTEL attr; result is still fatal error."""
    ctx = _make_ctx()
    r = await _call({"archetype": "scout"}, ctx)

    assert r.status is ToolResultStatus.ERROR_FATAL
    assert r.payload is None

    recorded = _otel(ctx)
    assert recorded["tool.loadout.genre"] == ""
    assert recorded["tool.loadout.archetype"] == "scout"
    assert recorded["tool.loadout.tier"] == 1


# ---------------------------------------------------------------------------
# Tier boundary — all five tiers reach the handler (which errors fatally)
# ---------------------------------------------------------------------------


async def test_all_five_tiers_accepted() -> None:
    """All five tiers pass validator; each reaches the handler and returns ERROR_FATAL."""
    for tier in (1, 2, 3, 4, 5):
        ctx = _make_ctx()
        r = await _call({"archetype": "fighter", "tier": tier}, ctx)
        assert r.status is ToolResultStatus.ERROR_FATAL


# ---------------------------------------------------------------------------
# Anti-confabulation wiring test — dispatch path produces model-visible hard error
# ---------------------------------------------------------------------------


async def test_valid_call_dispatched_through_registry_yields_is_error_true() -> None:
    """Prove that a valid call reaching the real handler surfaces as is_error=True.

    This is the mandatory wiring test: the narrator physically cannot receive
    a phantom-success result from generate_loadout.
    """
    ctx = _make_ctx()
    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-anti-confab",
            name="generate_loadout",
            arguments={"archetype": "fighter", "tier": 1},
        ),
        ctx,
    )
    assert out.is_error is True
    assert out.content.startswith("ERROR:")


async def test_tier_zero_is_validator_error() -> None:
    ctx = _make_ctx()
    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-tier-zero",
            name="generate_loadout",
            arguments={"archetype": "fighter", "tier": 0},
        ),
        ctx,
    )
    assert out.is_error is True
    assert "argument validation failed" in out.content


async def test_tier_six_is_validator_error() -> None:
    ctx = _make_ctx()
    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-tier-six",
            name="generate_loadout",
            arguments={"archetype": "fighter", "tier": 6},
        ),
        ctx,
    )
    assert out.is_error is True
    assert "argument validation failed" in out.content


async def test_empty_archetype_is_validator_error() -> None:
    ctx = _make_ctx()
    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-empty-arch",
            name="generate_loadout",
            arguments={"archetype": "", "tier": 1},
        ),
        ctx,
    )
    assert out.is_error is True
    assert "argument validation failed" in out.content
