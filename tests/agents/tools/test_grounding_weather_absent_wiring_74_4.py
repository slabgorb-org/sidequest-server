"""Story 74-4 AC2 wiring — ``weather_absent`` fires from the real grounding tool.

The helper round-trip lives in
``tests/telemetry/spans/test_world_grounding_weather_absent_74_4.py``. This file
proves the span fires from the production ``get_world_grounding`` handler reached
via the real ``default_registry.dispatch`` path, and — critically for a lie
detector — that ``weather_absent`` and ``weather_used`` are **mutually
exclusive**: exactly one fires per weather-requested call, depending on whether
the session actually has weather wired.

The negative cases are non-negotiable. If ``weather_absent`` fired when weather
WAS delivered, or when the narrator never asked for weather, the GM panel would
miscount and stop being a lie detector (CLAUDE.md OTEL Observability Principle).
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

_ABSENT = "world_grounding.weather_absent"
_USED = "world_grounding.weather_used"


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
}


def _ctx(
    *,
    weather: WeatherState | None = None,
    demographics: dict[str, Any] | None = None,
    world_id: str = "glenross",
    perspective_pc: str | None = "Alex",
) -> ToolContext:
    return ToolContext(
        world_id=world_id,
        session_id="s1",
        perspective_pc=perspective_pc,
        turn_number=1,
        repository=MagicMock(),
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
        weather_state=weather,
        world_demographics=demographics,
        world_calendar=None,
    )


def _spans_named(exporter: InMemorySpanExporter, name: str) -> list[Any]:
    return [s for s in exporter.get_finished_spans() if s.name == name]


# --------------------------------------------------------------------------- #
# absent fires iff weather REQUESTED but NOT wired
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_weather_absent_fires_when_requested_but_unwired(
    otel_capture: InMemorySpanExporter,
) -> None:
    """Default include surfaces weather; ``weather_state=None`` ⇒ absence span
    fires, carrying ``world_id`` and ``perspective_pc`` so the GM panel can show
    *which* world reported no weather."""
    ctx = _ctx(weather=None, demographics=_DEMOGRAPHICS)
    result = await default_registry.dispatch(
        ToolUseBlock(id="t-absent-1", name="get_world_grounding", arguments={}),
        ctx,
    )
    assert result.is_error is False

    absent = _spans_named(otel_capture, _ABSENT)
    assert len(absent) == 1, (
        f"expected one weather_absent span, got {len(absent)} "
        f"(all: {[s.name for s in otel_capture.get_finished_spans()]})"
    )
    attrs = absent[0].attributes or {}
    assert attrs["world_id"] == "glenross"
    assert attrs["perspective_pc"] == "Alex"
    # Mutually exclusive with weather_used.
    assert _spans_named(otel_capture, _USED) == [], (
        "weather_used must NOT fire when weather is unwired"
    )


@pytest.mark.asyncio
async def test_weather_absent_does_not_fire_when_weather_wired(
    otel_capture: InMemorySpanExporter,
) -> None:
    """Weather IS wired ⇒ ``weather_used`` fires, ``weather_absent`` does NOT.

    Firing the absence span here would tell the GM panel the world has no
    weather while the narrator was, in fact, handed weather — a direct lie."""
    ctx = _ctx(weather=_weather(), demographics=_DEMOGRAPHICS)
    await default_registry.dispatch(
        ToolUseBlock(id="t-absent-wired", name="get_world_grounding", arguments={}),
        ctx,
    )
    assert _spans_named(otel_capture, _ABSENT) == [], (
        "weather_absent must NOT fire when ctx.weather_state is present"
    )
    assert len(_spans_named(otel_capture, _USED)) == 1, (
        "weather_used must fire when weather is wired (mutual-exclusivity peer)"
    )


@pytest.mark.asyncio
async def test_weather_absent_does_not_fire_when_section_excluded(
    otel_capture: InMemorySpanExporter,
) -> None:
    """``include=["demographics"]`` ⇒ the narrator did not ask for weather, so
    the absence span must NOT fire even though ``weather_state`` is None. The
    signal is "narrator asked and got nothing", not "no weather exists"."""
    ctx = _ctx(weather=None, demographics=_DEMOGRAPHICS)
    await default_registry.dispatch(
        ToolUseBlock(
            id="t-absent-skip",
            name="get_world_grounding",
            arguments={"include": ["demographics"]},
        ),
        ctx,
    )
    assert _spans_named(otel_capture, _ABSENT) == [], (
        "weather_absent must NOT fire when 'weather' is not in include"
    )


@pytest.mark.asyncio
async def test_weather_absent_does_not_fire_on_empty_include(
    otel_capture: InMemorySpanExporter,
) -> None:
    """``include=[]`` is degenerate-but-valid: no sections requested, no absence
    span. Defensive symmetry with the weather_used empty-include guard."""
    ctx = _ctx(weather=None, demographics=_DEMOGRAPHICS)
    await default_registry.dispatch(
        ToolUseBlock(
            id="t-absent-empty",
            name="get_world_grounding",
            arguments={"include": []},
        ),
        ctx,
    )
    assert _spans_named(otel_capture, _ABSENT) == []
