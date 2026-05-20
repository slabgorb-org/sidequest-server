"""Tests for the get_world_grounding tool — Story 24-6 RED.

READ tool. Surfaces three world-grounding sections to the narrator on demand:

* ``weather``        — current :class:`WeatherState` (story 24-5 output).
* ``demographics``   — pack/world demographics dict (story 24-3 content).
* ``calendar``       — pack/world calendar dict (story 24-4 content).

Per **ADR-102** (native tool-use, supersedes ADR-039) and **ADR-103** (free
OTEL via tool registry). Replaces the rejected always-on VALLEY-zone
injection approach from the original 24-6 spec.

Design seam (committed during RED — see TEA Assessment deviation):
    ``ToolContext`` gains three optional, plain-data fields —
    ``weather_state``, ``world_demographics``, ``world_calendar`` —
    populated by the session-handler call site (production wiring is a
    GREEN-phase concern). The tool reads them; if a section is ``None``
    (e.g. ``calendar.yaml`` absent because 24-4 hasn't landed), the
    corresponding payload value is ``None`` AND the dispatch span gets
    a ``tool.grounding.<section>_present`` boolean attribute. **No
    silent fallback** (CLAUDE.md): absence is explicitly signaled both
    in the payload and in OTEL. The narrator can see "calendar not
    configured" and respond accordingly.

This mirrors the lookup_monster pattern (``monster_manual_wired=False``
attr) and the query_scene_state OTEL-sentinel convention.
"""

from __future__ import annotations

import json
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
from sidequest.agents.tools import get_world_grounding as _mod  # noqa: F401
from sidequest.game.weather import WeatherState

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _weather(
    *,
    zone: str = "glen_floor",
    season: str = "autumn",
    condition: str = "overcast",
    temperature_c: float = 12.0,
    precipitation: bool = False,
    special_event: str | None = None,
    effects: list[str] | None = None,
    seed: int = 42,
) -> WeatherState:
    return WeatherState(
        zone=zone,
        season=season,
        condition=condition,
        temperature_c=temperature_c,
        precipitation=precipitation,
        special_event=special_event,
        effects=effects or [],
        seed=seed,
    )


_DEMOGRAPHICS: dict[str, Any] = {
    "version": "0.1.0",
    "world": "glenross",
    "parish": {
        "name": "Glenross",
        "total_population": 412,
        "village_centre_population": 148,
    },
    "recurring_cast": [
        {"id": "minister", "name": "Rev. Aulay"},
        {"id": "postmistress", "name": "Margaret"},
    ],
}

_CALENDAR: dict[str, Any] = {
    "version": "0.1.0",
    "world": "glenross",
    "months": [
        "January",
        "February",
        "October",
        "November",
        "December",
    ],
    "current": {"month": "October", "day": 14, "year": 1908},
}


def _make_ctx(
    *,
    weather: WeatherState | None = None,
    demographics: dict[str, Any] | None = None,
    calendar: dict[str, Any] | None = None,
    store: Any = None,
    perspective_pc: str | None = "Alex",
) -> ToolContext:
    """Build a ToolContext with the new grounding fields populated.

    The new ``weather_state`` / ``world_demographics`` / ``world_calendar``
    kwargs do not exist on ToolContext yet — that is the RED signal Dev
    will resolve in GREEN by extending the dataclass.
    """
    return ToolContext(
        world_id="glenross",
        session_id="s1",
        perspective_pc=perspective_pc,
        turn_number=1,
        store=store if store is not None else MagicMock(),
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
        weather_state=weather,
        world_demographics=demographics,
        world_calendar=calendar,
    )


async def _call(arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """Invoke the registered handler directly (bypass dispatch + perception)."""
    registered = default_registry._tools["get_world_grounding"]
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, ctx)


def _payload(r: ToolResult) -> dict[str, Any]:
    assert r.payload is not None
    return cast(dict[str, Any], r.payload)


# ---------------------------------------------------------------------------
# AC-1: Tool registered
# ---------------------------------------------------------------------------


def test_get_world_grounding_is_registered() -> None:
    """AC-1: tool name appears in the default registry."""
    assert "get_world_grounding" in default_registry.list_names()


def test_get_world_grounding_is_read_category() -> None:
    """Grounding fetches are read-only; must not take the write lock."""
    from sidequest.agents.tool_registry import ToolCategory

    registered = default_registry._tools["get_world_grounding"]
    assert registered.category is ToolCategory.READ


# ---------------------------------------------------------------------------
# AC-2: Default include returns all three sections
# ---------------------------------------------------------------------------


async def test_default_include_returns_all_three_sections() -> None:
    """AC-2: a no-arg call surfaces weather + demographics + calendar."""
    ctx = _make_ctx(weather=_weather(), demographics=_DEMOGRAPHICS, calendar=_CALENDAR)
    r = await _call({}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert set(p["include"]) == {"weather", "demographics", "calendar"}
    assert p["weather"] is not None
    assert p["demographics"] is not None
    assert p["calendar"] is not None


# ---------------------------------------------------------------------------
# Section selection — mirrors query_scene_state include pattern
# ---------------------------------------------------------------------------


async def test_include_weather_only_omits_other_sections() -> None:
    ctx = _make_ctx(weather=_weather(), demographics=_DEMOGRAPHICS, calendar=_CALENDAR)
    r = await _call({"include": ["weather"]}, ctx)
    p = _payload(r)
    assert "weather" in p
    assert "demographics" not in p
    assert "calendar" not in p
    assert p["include"] == ["weather"]


async def test_include_demographics_only() -> None:
    ctx = _make_ctx(weather=_weather(), demographics=_DEMOGRAPHICS, calendar=_CALENDAR)
    r = await _call({"include": ["demographics"]}, ctx)
    p = _payload(r)
    assert "demographics" in p
    assert "weather" not in p
    assert "calendar" not in p


async def test_include_calendar_only() -> None:
    ctx = _make_ctx(weather=_weather(), demographics=_DEMOGRAPHICS, calendar=_CALENDAR)
    r = await _call({"include": ["calendar"]}, ctx)
    p = _payload(r)
    assert "calendar" in p
    assert "weather" not in p
    assert "demographics" not in p


async def test_empty_include_returns_minimal_payload() -> None:
    """include=[] → only the echo field; no section data."""
    ctx = _make_ctx(weather=_weather(), demographics=_DEMOGRAPHICS, calendar=_CALENDAR)
    r = await _call({"include": []}, ctx)
    p = _payload(r)
    assert p == {"include": []}


# ---------------------------------------------------------------------------
# AC-2 shape: weather section reflects WeatherState fields
# ---------------------------------------------------------------------------


async def test_weather_payload_round_trips_weather_state_fields() -> None:
    """The weather section must surface the fields a narrator actually uses.

    The narrator needs zone+season+condition+temperature+precipitation to
    write scene weather; ``seed`` and ``special_event`` are needed by the
    GM panel to audit determinism.
    """
    w = _weather(
        zone="glen_floor",
        season="autumn",
        condition="smirr",
        temperature_c=11.5,
        precipitation=True,
        special_event=None,
        effects=[],
        seed=42,
    )
    ctx = _make_ctx(weather=w)
    r = await _call({"include": ["weather"]}, ctx)
    weather = _payload(r)["weather"]
    assert weather is not None
    assert weather["zone"] == "glen_floor"
    assert weather["season"] == "autumn"
    assert weather["condition"] == "smirr"
    assert abs(weather["temperature_c"] - 11.5) < 1e-9
    assert weather["precipitation"] is True
    assert weather["special_event"] is None
    assert weather["seed"] == 42


async def test_weather_payload_includes_special_event_when_fired() -> None:
    """When a special event fires, both the name and effects surface."""
    w = _weather(
        condition="blizzard",
        special_event="winter_storm",
        effects=["impair_visibility", "ground_travel_halved"],
    )
    ctx = _make_ctx(weather=w)
    r = await _call({"include": ["weather"]}, ctx)
    weather = _payload(r)["weather"]
    assert weather["special_event"] == "winter_storm"
    assert weather["effects"] == ["impair_visibility", "ground_travel_halved"]


# ---------------------------------------------------------------------------
# Missing-data behavior — no silent fallbacks per CLAUDE.md
# ---------------------------------------------------------------------------


async def test_weather_section_is_none_when_unconfigured() -> None:
    """When the session has no weather_state, the payload section is None.

    This is an explicit signal — not a silent fallback. Pairs with the
    OTEL ``tool.grounding.weather_present=False`` attribute below.
    """
    ctx = _make_ctx(weather=None, demographics=_DEMOGRAPHICS, calendar=_CALENDAR)
    r = await _call({"include": ["weather"]}, ctx)
    assert _payload(r)["weather"] is None


async def test_demographics_section_is_none_when_unconfigured() -> None:
    ctx = _make_ctx(weather=_weather(), demographics=None, calendar=_CALENDAR)
    r = await _call({"include": ["demographics"]}, ctx)
    assert _payload(r)["demographics"] is None


async def test_calendar_section_is_none_when_yaml_absent() -> None:
    """Story 24-4 (glenross calendar) is backlog; the tool must not crash
    when its YAML is missing — it returns None and signals absence in OTEL.
    """
    ctx = _make_ctx(weather=_weather(), demographics=_DEMOGRAPHICS, calendar=None)
    r = await _call({"include": ["calendar"]}, ctx)
    assert _payload(r)["calendar"] is None


# ---------------------------------------------------------------------------
# Demographics + calendar surface intact pass-through
# ---------------------------------------------------------------------------


async def test_demographics_payload_carries_parish_and_cast() -> None:
    ctx = _make_ctx(demographics=_DEMOGRAPHICS)
    r = await _call({"include": ["demographics"]}, ctx)
    demo = _payload(r)["demographics"]
    assert demo is not None
    assert demo["parish"]["name"] == "Glenross"
    assert demo["parish"]["total_population"] == 412
    assert any(c["id"] == "minister" for c in demo["recurring_cast"])


async def test_calendar_payload_carries_current_date() -> None:
    ctx = _make_ctx(calendar=_CALENDAR)
    r = await _call({"include": ["calendar"]}, ctx)
    cal = _payload(r)["calendar"]
    assert cal is not None
    assert cal["current"]["month"] == "October"
    assert cal["current"]["year"] == 1908


# ---------------------------------------------------------------------------
# Dispatch (full registry path, post-perception)
# ---------------------------------------------------------------------------


async def test_dispatch_payload_round_trip() -> None:
    """Tool reaches the narrator via the standard dispatch path."""
    ctx = _make_ctx(weather=_weather(), demographics=_DEMOGRAPHICS, calendar=_CALENDAR)
    out = await default_registry.dispatch(
        ToolUseBlock(id="t-disp", name="get_world_grounding", arguments={}),
        ctx,
    )
    assert out.is_error is False
    payload = json.loads(out.content)
    assert "weather" in payload
    assert "demographics" in payload
    assert "calendar" in payload


# ---------------------------------------------------------------------------
# AC-4: OTEL — free via ADR-103 tool registry hook
# ---------------------------------------------------------------------------


async def test_otel_dispatch_span_emitted(otel_capture) -> None:
    """ADR-103: dispatch automatically emits ``tool.read.<name>`` span.

    No bespoke OTEL wiring required in the handler — registration is
    sufficient. This is the AC-4 acceptance test.
    """
    ctx = _make_ctx(weather=_weather(), demographics=_DEMOGRAPHICS, calendar=_CALENDAR)
    out = await default_registry.dispatch(
        ToolUseBlock(id="t-otel", name="get_world_grounding", arguments={}),
        ctx,
    )
    assert out.is_error is False
    spans = otel_capture.get_finished_spans()
    read_spans = [s for s in spans if s.name == "tool.read.get_world_grounding"]
    assert read_spans, f"no dispatch span; got: {[s.name for s in spans]}"
    attrs = dict(read_spans[-1].attributes or {})
    assert attrs.get("tool.name") == "get_world_grounding"
    assert attrs.get("tool.category") == "read"
    assert attrs.get("tool.result_status") == "ok"


async def test_otel_marks_section_presence_attrs(otel_capture) -> None:
    """GM panel needs to see which sections were actually wired this call.

    Boolean attrs let the dashboard distinguish "narrator skipped this
    section" from "session didn't have the data plumbed in". When 24-4
    lands, ``calendar_present`` flips to True without any tool change.
    """
    ctx = _make_ctx(weather=_weather(), demographics=_DEMOGRAPHICS, calendar=None)
    out = await default_registry.dispatch(
        ToolUseBlock(id="t-otel-wired", name="get_world_grounding", arguments={}),
        ctx,
    )
    assert out.is_error is False
    spans = otel_capture.get_finished_spans()
    read_spans = [s for s in spans if s.name == "tool.read.get_world_grounding"]
    assert read_spans
    attrs = dict(read_spans[-1].attributes or {})
    assert attrs.get("tool.grounding.weather_present") is True
    assert attrs.get("tool.grounding.demographics_present") is True
    assert attrs.get("tool.grounding.calendar_present") is False


async def test_otel_marks_all_sections_absent_when_unconfigured(otel_capture) -> None:
    """A fully-unwired session (e.g. session-handler not yet plumbed) is
    observable as three False markers — the GM panel's lie-detector view
    of "narrator called grounding but got nothing".
    """
    ctx = _make_ctx(weather=None, demographics=None, calendar=None)
    out = await default_registry.dispatch(
        ToolUseBlock(id="t-otel-unwired", name="get_world_grounding", arguments={}),
        ctx,
    )
    assert out.is_error is False
    spans = otel_capture.get_finished_spans()
    read_spans = [s for s in spans if s.name == "tool.read.get_world_grounding"]
    assert read_spans
    attrs = dict(read_spans[-1].attributes or {})
    assert attrs.get("tool.grounding.weather_present") is False
    assert attrs.get("tool.grounding.demographics_present") is False
    assert attrs.get("tool.grounding.calendar_present") is False


# ---------------------------------------------------------------------------
# ToolContext extension — RED signal Dev must resolve in GREEN
# ---------------------------------------------------------------------------


def test_tool_context_grounding_fields_default_to_none() -> None:
    """Phase E parallel: production wiring sets these to live values at
    the session-handler ctx-construction site. Constructors that omit
    the fields must keep working (existing 28 tool tests must stay green).
    """
    ctx = ToolContext(
        world_id="w",
        session_id="s",
        perspective_pc="alex",
        turn_number=1,
        store=MagicMock(),
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
    )
    assert ctx.weather_state is None
    assert ctx.world_demographics is None
    assert ctx.world_calendar is None


def test_tool_context_accepts_grounding_kwargs() -> None:
    """Verify the dataclass slot — production wiring will pass live values."""
    w = _weather()
    ctx = ToolContext(
        world_id="w",
        session_id="s",
        perspective_pc="alex",
        turn_number=1,
        store=MagicMock(),
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
        weather_state=w,
        world_demographics=_DEMOGRAPHICS,
        world_calendar=_CALENDAR,
    )
    assert ctx.weather_state is w
    assert ctx.world_demographics is _DEMOGRAPHICS
    assert ctx.world_calendar is _CALENDAR
