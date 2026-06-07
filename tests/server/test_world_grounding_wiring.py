"""Story 24-10 RED tests — _SessionData / TurnContext / ToolContext
world-grounding field propagation.

This file is the structural-wiring mirror of
``test_turn_context_sdk_wiring.py`` (which proved the lore_store /
monster_manual Phase-E seam). Story 24-10 plumbs three new fields along
exactly the same seam:

    weather_state       : WeatherState | None
    world_demographics  : dict | None
    world_calendar      : dict | None

Three levels of evidence, smallest to largest:

  1. ``_SessionData`` dataclass carries the three fields with default
     ``None`` (so a session with no grounding stays loud-not-silent).
  2. ``TurnContext`` carries the three fields with default ``None``, and
     ``_build_turn_context`` plumbs them from sd → tc (the per-turn carrier
     story context calls out at ``orchestrator.py:405``).
  3. The production ``_run_narration_turn_sdk`` path at
     ``orchestrator.py:3259`` constructs a ``ToolContext`` whose
     ``weather_state`` / ``world_demographics`` / ``world_calendar`` come
     from the ``TurnContext`` — i.e. the change is reachable from
     ``run_narration_turn``, not just present on the dataclass.

The wiring test (level 3) is the load-bearing AC per the story risk note —
"the most common failure mode for this kind of story is shipping #1–#5 and
skipping #6/#7/#8". This file's level-3 test is the structural half of #6.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# Importing the tools package wires the 26 adapters onto default_registry.
import sidequest.agents.tools  # noqa: F401
from sidequest.agents.anthropic_sdk_client import AnthropicSdkClient
from sidequest.agents.orchestrator import Orchestrator, TurnContext
from sidequest.agents.tool_registry import ToolContext, default_registry
from sidequest.agents.tooling_protocol import ToolResultBlock, ToolUseBlock
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.game.weather import WeatherState
from sidequest.genre.loader import load_genre_pack
from sidequest.server.session_handler import _build_turn_context, _SessionData
from tests._helpers.session_room import room_for

CONTENT_GENRE_PACKS = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------


def _weather_state() -> WeatherState:
    return WeatherState(
        zone="glen_floor",
        season="autumn",
        condition="hill-fog",
        temperature_c=9.4,
        precipitation=False,
        special_event=None,
        effects=[],
        seed=42,
    )


_DEMOGRAPHICS: dict[str, Any] = {
    "world": "glenross",
    "version": "0.1.0",
    "parish": {"name": "Glenross"},
    "recurring_cast": ["Mrs Cameron"],
}

_CALENDAR: dict[str, Any] = {
    "world": "glenross",
    "version": "0.1.0",
    "current_date": {"year": 1908, "month": "October", "day": 19},
}


def _make_snapshot() -> GameSnapshot:
    return GameSnapshot(
        genre_slug="tea_and_murder",
        world_slug="glenross",
        turn_manager=TurnManager(interaction=3),
    )


def _build_sd_with_grounding() -> _SessionData:
    """Build a _SessionData populated with the three grounding fields.

    All other refs are MagicMocks — this test family proves field
    propagation, not deeper semantics. The genre_pack IS the real
    tea_and_murder pack so the cultures/encounter machinery TurnContext
    builds against has a real surface (the SDK path's prompt-builder
    inspects it)."""
    snap = _make_snapshot()
    pack = load_genre_pack(CONTENT_GENRE_PACKS / snap.genre_slug)
    sd = _SessionData(
        genre_slug=snap.genre_slug,
        world_slug=snap.world_slug,
        player_name="Alice",
        player_id="player:alice",
        snapshot=snap,
        repository=MagicMock(),
        dungeon_repository=MagicMock(),
        telemetry_sink=MagicMock(),
        genre_pack=pack,
        orchestrator=MagicMock(),
    )
    sd.repository.recent_narrative.return_value = []
    sd.game_slug = "2026-05-21-tea_and_murder_glenross-1"
    # The three load-bearing fields under test (24-10 ACs 4 + 5):
    sd.weather_state = _weather_state()
    sd.world_demographics = _DEMOGRAPHICS
    sd.world_calendar = _CALENDAR
    sd._room = room_for(snap, slug="glenross")
    return sd


# ---------------------------------------------------------------------------
# Level 1 — _SessionData carries the three grounding fields
# ---------------------------------------------------------------------------


def test_session_data_has_weather_state_field_with_default_none() -> None:
    """A pack with no weather.yaml MUST land in a session where
    ``sd.weather_state`` is None, not missing-attribute-error. Default-
    None on the dataclass keeps the "no grounding declared" branch
    legitimate."""
    sd_fields = {f.name: f for f in fields(_SessionData)}
    assert "weather_state" in sd_fields, (
        "_SessionData missing 'weather_state' field — Story 24-10 AC4 "
        "requires the session handler to carry the WeatherState produced "
        "at bootstrap so ToolContext can stamp it on every turn"
    )


def test_session_data_has_world_demographics_field_with_default_none() -> None:
    sd_fields = {f.name: f for f in fields(_SessionData)}
    assert "world_demographics" in sd_fields, (
        "_SessionData missing 'world_demographics' field — Story 24-10 AC4"
    )


def test_session_data_has_world_calendar_field_with_default_none() -> None:
    sd_fields = {f.name: f for f in fields(_SessionData)}
    assert "world_calendar" in sd_fields, (
        "_SessionData missing 'world_calendar' field — Story 24-10 AC4"
    )


# ---------------------------------------------------------------------------
# Level 2 — TurnContext carries the three fields + _build_turn_context plumbs
# ---------------------------------------------------------------------------


def test_turn_context_defaults_grounding_fields_to_none() -> None:
    """A bare TurnContext with no grounding kwargs must leave the three
    fields as None — same default-None semantics as lore_store. This is
    what makes ``ctx.weather_state is not None`` a meaningful guard in
    ``get_world_grounding`` (and in emit_weather_used_span)."""
    ctx = TurnContext(character_name="Alice")
    assert ctx.weather_state is None, (
        "TurnContext default weather_state must be None — Story 24-10 AC4"
    )
    assert ctx.world_demographics is None, (
        "TurnContext default world_demographics must be None — Story 24-10 AC4"
    )
    assert ctx.world_calendar is None, (
        "TurnContext default world_calendar must be None — Story 24-10 AC4"
    )


def test_turn_context_accepts_grounding_kwargs() -> None:
    """Constructing TurnContext with all three grounding kwargs must
    succeed and round-trip the values verbatim. (No coercion, no copy —
    pass-through references so the underlying generator/yaml dict
    identity is preserved across the per-turn pipeline.)"""
    weather = _weather_state()
    ctx = TurnContext(
        character_name="Alice",
        weather_state=weather,
        world_demographics=_DEMOGRAPHICS,
        world_calendar=_CALENDAR,
    )
    assert ctx.weather_state is weather
    assert ctx.world_demographics is _DEMOGRAPHICS
    assert ctx.world_calendar is _CALENDAR


def test_build_turn_context_propagates_grounding_from_session_data() -> None:
    """``_build_turn_context`` MUST copy ``sd.weather_state`` /
    ``sd.world_demographics`` / ``sd.world_calendar`` onto the
    TurnContext. Without this, even a correctly-bootstrapped session
    handler hands the narrator a TurnContext with ``weather_state=None``
    every turn — the per-turn carrier is the seam where the wiring breaks
    silently."""
    sd = _build_sd_with_grounding()

    ctx = _build_turn_context(sd, room=sd._room)

    assert ctx.weather_state is sd.weather_state, (
        "weather_state not plumbed from sd → TurnContext — ToolContext "
        "would see weather_state=None and the narrator would improvise"
    )
    assert ctx.world_demographics is sd.world_demographics, (
        "world_demographics not plumbed from sd → TurnContext"
    )
    assert ctx.world_calendar is sd.world_calendar, (
        "world_calendar not plumbed from sd → TurnContext"
    )


# ---------------------------------------------------------------------------
# Level 3 — production SDK path (orchestrator.py:3259) wires ToolContext
# ---------------------------------------------------------------------------


@dataclass
class _Usage:
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class _TextBlock:
    type: str
    text: str


@dataclass
class _Resp:
    content: list[Any]
    stop_reason: str
    usage: _Usage
    model: str


class _Msgs:
    def __init__(self, responses: list[_Resp]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _Resp:
        self.calls.append(kwargs)
        return self._responses.pop(0)


class _Sdk:
    def __init__(self, responses: list[_Resp]) -> None:
        self.messages = _Msgs(responses)


class _FakeRegistry:
    def compose_split(self, agent_name: str) -> tuple[str, str]:
        return ("system text", "user text")

    def compose_split_by_zone(self, agent_name: str):
        from sidequest.agents.prompt_framework.types import AttentionZone

        return ({AttentionZone.Primacy: "system text"}, "user text")

    def registry(self, agent_name: str) -> list:
        # Story 60-2: the SDK path emits an enriched prompt_assembled post-call
        # that reads registry sections; this stub registers none.
        return []


async def _run_sdk_and_capture_tool_ctx(
    monkeypatch: pytest.MonkeyPatch,
    context: TurnContext,
    tool_name: str = "get_world_grounding",
    tool_input: dict[str, Any] | None = None,
) -> ToolContext:
    """Drive ``run_narration_turn`` through the SDK path with a fake SDK
    that emits a single tool_use block + a closing text response. Capture
    the ToolContext the production code constructed at orchestrator.py:3259.

    Mirrors ``test_turn_context_sdk_wiring.py::_run_sdk_and_capture_ctx`` —
    same protocol shape, different tool name. Reused so future grounding
    extensions land here too."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    captured: dict[str, ToolContext] = {}

    async def _spy_dispatch(block: ToolUseBlock, ctx: ToolContext) -> ToolResultBlock:
        captured["ctx"] = ctx
        return ToolResultBlock(tool_use_id=block.id, content="ok", is_error=False)

    sdk = _Sdk(
        responses=[
            _Resp(
                content=[
                    type(
                        "TU",
                        (),
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": tool_name,
                            "input": tool_input
                            or {"include": ["weather", "demographics", "calendar"]},
                        },
                    )()
                ],
                stop_reason="tool_use",
                usage=_Usage(input_tokens=120, output_tokens=10),
                model="claude-sonnet-4-6",
            ),
            _Resp(
                content=[_TextBlock(type="text", text="The fog hangs low.")],
                stop_reason="end_turn",
                usage=_Usage(input_tokens=130, output_tokens=14),
                model="claude-sonnet-4-6",
            ),
        ]
    )
    client = AnthropicSdkClient(sdk=sdk)
    orch = Orchestrator(client=client)

    monkeypatch.setattr(default_registry, "dispatch", _spy_dispatch)

    async def _fake_build_prompt(
        self: Orchestrator, action: str, ctx: TurnContext
    ) -> tuple[str, _FakeRegistry]:
        return ("prompt-text", _FakeRegistry())

    monkeypatch.setattr(Orchestrator, "build_narrator_prompt", _fake_build_prompt)

    await orch.run_narration_turn("look around", context)
    assert "ctx" in captured, "tool_dispatch never fired — cannot assert ToolContext wiring"
    return captured["ctx"]


@pytest.mark.asyncio
async def test_sdk_path_builds_toolcontext_with_grounding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mandatory wiring test for AC5: prove
    ``_run_narration_turn_sdk`` (orchestrator.py:3259) passes
    ``weather_state`` / ``world_demographics`` / ``world_calendar`` from
    TurnContext into the ToolContext the registry dispatches with. Failure
    here is the exact Pattern-1 bug story 24-10 exists to close: every
    component green, integration seam unowned, narrator gets `None`."""
    weather = _weather_state()
    ctx = TurnContext(
        character_name="Alice",
        world_id="glenross",
        session_id="2026-05-21-tea_and_murder_glenross-1",
        turn_number=3,
        repository=MagicMock(),
        weather_state=weather,
        world_demographics=_DEMOGRAPHICS,
        world_calendar=_CALENDAR,
    )

    tool_ctx = await _run_sdk_and_capture_tool_ctx(monkeypatch, ctx)

    assert tool_ctx.weather_state is weather, (
        "ToolContext.weather_state is not the TurnContext.weather_state — "
        "orchestrator.py:3259 omits the kwarg. get_world_grounding would "
        "return weather=None and the narrator confabulates climate (Pattern-1 "
        "the very bug 24-10 closes)."
    )
    assert tool_ctx.world_demographics is _DEMOGRAPHICS, (
        "ToolContext.world_demographics not wired — narrator improvises demographics"
    )
    assert tool_ctx.world_calendar is _CALENDAR, (
        "ToolContext.world_calendar not wired — narrator improvises calendar"
    )


@pytest.mark.asyncio
async def test_sdk_path_leaves_toolcontext_grounding_none_when_unwired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Symmetric negative: when TurnContext carries no grounding (the
    legitimate caverns_and_claudes case), ToolContext.weather_state /
    world_demographics / world_calendar MUST stay None. No silent
    fallback to a default WeatherState — that would defeat the 24-7
    lie-detector spans which gate on `is not None`."""
    ctx = TurnContext(
        character_name="Alice",
        world_id="mawdeep",
        session_id="adhoc",
        turn_number=1,
        repository=MagicMock(),
    )

    tool_ctx = await _run_sdk_and_capture_tool_ctx(monkeypatch, ctx)

    assert tool_ctx.weather_state is None, (
        "ToolContext.weather_state silently populated despite TurnContext "
        "carrying None — that's a silent fallback (CLAUDE.md violation) "
        "and would mask the wiring bug 24-10 is closing"
    )
    assert tool_ctx.world_demographics is None
    assert tool_ctx.world_calendar is None
