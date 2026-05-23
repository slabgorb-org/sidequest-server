"""Wiring tests: ``get_world_grounding`` emits used + injection spans (Story 24-7).

The 24-6 baseline already stamps three ``tool.grounding.<section>_present``
attributes on the *dispatch* span. Those answer "did the session have
data wired?" — not "did the narrator's tool call actually return data
to the model?". The 24-7 GM dashboard view needs the second answer to
diff "weather proposed" against "weather used" and to detect
demographics-injection coverage gaps.

This file verifies the two new spans fire from the production tool
handler reached via the real ``default_registry.dispatch`` path. The
pure-helper round-trip is in
``tests/telemetry/spans/test_world_grounding_spans.py``.

Lie-detector framing
~~~~~~~~~~~~~~~~~~~~
The whole point of these spans is to make narrator-improvised weather
visible on the GM panel. The negative cases below are non-negotiable:
if either span fires when no data was returned, the dashboard
miscounts grounding coverage and stops being a lie detector — see
CLAUDE.md OTEL Observability Principle.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.agents.narrator_perception_filter import NarratorPerceptionFilter
from sidequest.agents.tool_registry import ToolContext, default_registry
from sidequest.agents.tooling_protocol import ToolUseBlock
from sidequest.agents.tools import (  # noqa: F401 — registers the tool
    get_world_grounding as _get_world_grounding_module,
)
from sidequest.game.weather import WeatherState

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _weather(seed: int = 42) -> WeatherState:
    return WeatherState(
        zone="glen_floor",
        season="autumn",
        condition="smirr",
        temperature_c=11.5,
        precipitation=True,
        special_event=None,
        effects=[],
        seed=seed,
    )


_DEMOGRAPHICS: dict[str, Any] = {
    "version": "0.1.0",
    "world": "glenross",
    "parish": {"name": "Glenross", "total_population": 412},
    "recurring_cast": [
        {"id": "minister", "name": "Rev. Aulay"},
        {"id": "postmistress", "name": "Margaret"},
    ],
}

_CALENDAR: dict[str, Any] = {
    "version": "0.1.0",
    "world": "glenross",
    "current": {"month": "October", "day": 14, "year": 1908},
}


def _ctx(
    *,
    weather: WeatherState | None = None,
    demographics: dict[str, Any] | None = None,
    calendar: dict[str, Any] | None = None,
    world_id: str = "glenross",
    perspective_pc: str | None = "Alex",
) -> ToolContext:
    return ToolContext(
        world_id=world_id,
        session_id="s1",
        perspective_pc=perspective_pc,
        turn_number=1,
        store=MagicMock(),
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
        weather_state=weather,
        world_demographics=demographics,
        world_calendar=calendar,
    )


def _names(exporter: InMemorySpanExporter) -> list[str]:
    return [s.name for s in exporter.get_finished_spans()]


# ---------------------------------------------------------------------------
# weather_used — fires iff weather requested AND wired
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_weather_used_span_fires_when_weather_requested_and_wired(
    otel_capture: InMemorySpanExporter,
) -> None:
    """Default include surfaces weather; ``weather_state`` is wired ⇒ span fires.

    The span MUST carry ``zone``, ``season``, ``condition``, and ``seed``
    so the dashboard can join the used row to its proposed peer.
    """
    ctx = _ctx(weather=_weather(seed=42), demographics=_DEMOGRAPHICS, calendar=_CALENDAR)
    result = await default_registry.dispatch(
        ToolUseBlock(id="t-used-1", name="get_world_grounding", arguments={}),
        ctx,
    )
    assert result.is_error is False

    used = [
        s for s in otel_capture.get_finished_spans() if s.name == "world_grounding.weather_used"
    ]
    assert len(used) == 1, (
        f"expected one weather_used span, got {len(used)} (all spans: {_names(otel_capture)})"
    )
    attrs = used[0].attributes or {}
    assert attrs["zone"] == "glen_floor"
    assert attrs["season"] == "autumn"
    assert attrs["condition"] == "smirr"
    assert attrs["seed"] == 42
    assert attrs["world_id"] == "glenross"
    assert attrs["perspective_pc"] == "Alex"


@pytest.mark.asyncio
async def test_weather_used_span_does_not_fire_when_section_excluded(
    otel_capture: InMemorySpanExporter,
) -> None:
    """``include=["demographics"]`` ⇒ no weather returned ⇒ NO used span.

    The narrator did not see weather data on this turn. Firing the
    span here would overcount grounding coverage on the dashboard
    and mask the case where narrator is improvising weather without
    fetching it."""
    ctx = _ctx(weather=_weather(), demographics=_DEMOGRAPHICS, calendar=_CALENDAR)
    await default_registry.dispatch(
        ToolUseBlock(
            id="t-used-skip",
            name="get_world_grounding",
            arguments={"include": ["demographics"]},
        ),
        ctx,
    )
    used = [
        s for s in otel_capture.get_finished_spans() if s.name == "world_grounding.weather_used"
    ]
    assert used == [], "weather_used must not fire when 'weather' is not in include"


@pytest.mark.asyncio
async def test_weather_used_span_does_not_fire_when_session_unwired(
    otel_capture: InMemorySpanExporter,
) -> None:
    """``weather_state=None`` (e.g. session-handler not yet plumbed) ⇒
    payload weather is ``None`` ⇒ NO used span.

    The existing ``tool.grounding.weather_present=False`` attribute on
    the dispatch span already records this case; firing
    ``weather_used`` on top would lie about delivery."""
    ctx = _ctx(weather=None, demographics=_DEMOGRAPHICS, calendar=_CALENDAR)
    await default_registry.dispatch(
        ToolUseBlock(id="t-used-unwired", name="get_world_grounding", arguments={}),
        ctx,
    )
    used = [
        s for s in otel_capture.get_finished_spans() if s.name == "world_grounding.weather_used"
    ]
    assert used == [], "weather_used must not fire when ctx.weather_state is None"


@pytest.mark.asyncio
async def test_weather_used_span_does_not_fire_on_empty_include(
    otel_capture: InMemorySpanExporter,
) -> None:
    """``include=[]`` is a degenerate but valid call — no sections
    delivered, no used span. Defensive: the production grounding tool
    accepts an empty list (existing 24-6 test covers this), and 24-7
    must not silently fire a used span for it."""
    ctx = _ctx(weather=_weather(), demographics=_DEMOGRAPHICS, calendar=_CALENDAR)
    await default_registry.dispatch(
        ToolUseBlock(
            id="t-used-empty",
            name="get_world_grounding",
            arguments={"include": []},
        ),
        ctx,
    )
    used = [
        s for s in otel_capture.get_finished_spans() if s.name == "world_grounding.weather_used"
    ]
    assert used == []


# ---------------------------------------------------------------------------
# demographics_injected — fires iff demographics requested AND wired
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_demographics_injected_span_fires_when_requested_and_wired(
    otel_capture: InMemorySpanExporter,
) -> None:
    """Default include surfaces demographics; the dict is wired ⇒
    injection span fires, carrying ``world_id``,
    ``total_population``, and ``recurring_cast_count``."""
    ctx = _ctx(weather=_weather(), demographics=_DEMOGRAPHICS, calendar=_CALENDAR)
    await default_registry.dispatch(
        ToolUseBlock(id="t-demo-1", name="get_world_grounding", arguments={}),
        ctx,
    )

    injected = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == "world_grounding.demographics_injected"
    ]
    assert len(injected) == 1
    attrs = injected[0].attributes or {}
    assert attrs["world_id"] == "glenross"
    assert attrs["total_population"] == 412
    assert attrs["recurring_cast_count"] == 2
    assert attrs["perspective_pc"] == "Alex"


@pytest.mark.asyncio
async def test_demographics_injected_span_does_not_fire_when_section_excluded(
    otel_capture: InMemorySpanExporter,
) -> None:
    """``include=["weather"]`` ⇒ no demographics returned ⇒ NO span.

    Same lie-detector argument as the weather case: firing on
    not-delivered data corrupts the dashboard's coverage histogram."""
    ctx = _ctx(weather=_weather(), demographics=_DEMOGRAPHICS, calendar=_CALENDAR)
    await default_registry.dispatch(
        ToolUseBlock(
            id="t-demo-skip",
            name="get_world_grounding",
            arguments={"include": ["weather"]},
        ),
        ctx,
    )
    injected = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == "world_grounding.demographics_injected"
    ]
    assert injected == []


@pytest.mark.asyncio
async def test_demographics_injected_span_does_not_fire_when_session_unwired(
    otel_capture: InMemorySpanExporter,
) -> None:
    """``world_demographics=None`` ⇒ payload demographics is ``None`` ⇒
    NO injection span."""
    ctx = _ctx(weather=_weather(), demographics=None, calendar=_CALENDAR)
    await default_registry.dispatch(
        ToolUseBlock(id="t-demo-unwired", name="get_world_grounding", arguments={}),
        ctx,
    )
    injected = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == "world_grounding.demographics_injected"
    ]
    assert injected == []


# ---------------------------------------------------------------------------
# Non-interference with existing 24-6 dispatch-span attrs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_existing_tool_grounding_present_attrs_still_fire_on_dispatch_span(
    otel_capture: InMemorySpanExporter,
) -> None:
    """Adding the new spans must NOT regress the 24-6 dispatch-span
    attributes. The dashboard's existing "present" view depends on
    them; this is the regression gate that proves 24-7 is purely
    additive."""
    ctx = _ctx(weather=_weather(), demographics=_DEMOGRAPHICS, calendar=None)
    await default_registry.dispatch(
        ToolUseBlock(id="t-coexist", name="get_world_grounding", arguments={}),
        ctx,
    )
    dispatch = [
        s for s in otel_capture.get_finished_spans() if s.name == "tool.read.get_world_grounding"
    ]
    assert dispatch, "dispatch span (tool.read.get_world_grounding) must still fire"
    attrs = dict(dispatch[-1].attributes or {})
    assert attrs.get("tool.grounding.weather_present") is True
    assert attrs.get("tool.grounding.demographics_present") is True
    assert attrs.get("tool.grounding.calendar_present") is False


@pytest.mark.asyncio
async def test_used_and_injected_spans_are_distinct_from_dispatch_span(
    otel_capture: InMemorySpanExporter,
) -> None:
    """The two new spans must be **separate** OTEL spans, not just
    attributes on the dispatch span. The dashboard's typed
    state_transition channel reads spans by name; collapsing them
    onto the dispatch span would route them under
    ``tool.read.get_world_grounding`` and never surface in the
    world-grounding tab."""
    ctx = _ctx(weather=_weather(), demographics=_DEMOGRAPHICS, calendar=_CALENDAR)
    await default_registry.dispatch(
        ToolUseBlock(id="t-distinct", name="get_world_grounding", arguments={}),
        ctx,
    )
    names = _names(otel_capture)
    assert "world_grounding.weather_used" in names
    assert "world_grounding.demographics_injected" in names
    assert "tool.read.get_world_grounding" in names
