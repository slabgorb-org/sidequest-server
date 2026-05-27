"""Story 61-followup-B — Promote ``narrator.sdk.usage`` to a watcher INFO event.

RED-phase gate. Today the per-call usage breakdown
(``input_tokens``/``output_tokens``/``cost_usd``/cache split) escapes only as a
``logger.info("narrator.sdk.usage …")`` line in
``AnthropicSdkClient.complete_with_tools`` (``anthropic_sdk_client.py`` ~L396).
A log line is invisible to the GM-panel watcher transport — it cannot be
plotted or trended. This story promotes the line to an additive
``severity="info"`` watcher event (the log line stays) so the GM panel gets a
continuous per-call cost baseline beneath the 61-4 ``warn`` alarm and the
61-followup-D session ceiling.

Locked design — see ``sprint/context/context-story-61-followup-B.md``:
  - Event type: ``narrator.sdk.usage`` (mirrors the log-line name).
  - Component: ``narrator.sdk`` (matches the 60-7 ``both_writes_fired`` sibling
    so the Subsystems tab groups them together).
  - Severity: ``info`` (continuous baseline, NOT the 61-4 ``warn``).
  - Field contract (exactly these six keys): ``input_tokens``,
    ``output_tokens``, ``cost_usd``, ``model``, ``cache_read_tokens``,
    ``cache_write_tokens``.
  - Cadence (AC-3 resolution): once per SDK call / per tool-use iteration. A
    3-iteration turn emits 3 events; a single-call turn emits exactly one. The
    per-*turn* cumulative pulse is followup-D's job, not this event's.

Wiring discipline (server CLAUDE.md — "No Source-Text Wiring Tests"): every
test drives a synthetic SDK response through the real
``complete_with_tools`` path and captures published events at the
``watcher_hub`` transport boundary. No test greps source.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from sidequest.agents.anthropic_cost import compute_cost_usd
from sidequest.agents.anthropic_sdk_client import AnthropicSdkClient
from sidequest.agents.tooling_protocol import (
    CacheableBlock,
    Message,
    ToolDefinition,
    ToolResultBlock,
    ToolUseBlock,
)
from sidequest.telemetry.watcher_hub import WatcherHub, watcher_hub

USAGE_EVENT = "narrator.sdk.usage"
USAGE_COMPONENT = "narrator.sdk"
REQUIRED_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cost_usd",
    "model",
    "cache_read_tokens",
    "cache_write_tokens",
)


# --- SDK-shape fakes (mirror tests/agents/test_61_4_cost_runaway_alarm.py) ----


@dataclass(frozen=True)
class _Usage:
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_creation: Any | None = None


@dataclass(frozen=True)
class _TextBlock:
    type: str
    text: str


@dataclass(frozen=True)
class _ToolUseContent:
    type: str
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class _Resp:
    content: list[Any]
    stop_reason: str
    usage: _Usage
    model: str


class _Msgs:
    def __init__(self, responses: list[_Resp]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _Resp:
        self.calls.append(kwargs)
        if not self._responses:
            raise RuntimeError("FakeSdk: out of scripted responses")
        return self._responses.pop(0)


class _Sdk:
    def __init__(self, responses: list[_Resp]) -> None:
        self.messages = _Msgs(responses)


def _text_resp(
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_write: int = 0,
    text: str = "ok",
    model: str = "claude-sonnet-4-6",
) -> _Resp:
    return _Resp(
        content=[_TextBlock(type="text", text=text)],
        stop_reason="end_turn",
        usage=_Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_write,
        ),
        model=model,
    )


def _tool_use_resp(
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_write: int = 0,
    tool_id: str = "toolu_1",
    model: str = "claude-sonnet-4-6",
) -> _Resp:
    return _Resp(
        content=[
            _ToolUseContent(type="tool_use", id=tool_id, name="roll_dice", input={"sides": 20})
        ],
        stop_reason="tool_use",
        usage=_Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_write,
        ),
        model=model,
    )


def _system_blocks() -> list[CacheableBlock]:
    return [CacheableBlock(text="rules", cache=True)]


def _user_msg() -> list[Message]:
    return [Message(role="user", content="go")]


def _roll_tool() -> list[ToolDefinition]:
    return [ToolDefinition(name="roll_dice", description="Roll", input_schema={"type": "object"})]


def _dispatch(block: ToolUseBlock) -> ToolResultBlock:
    return ToolResultBlock(tool_use_id=block.id, content="17", is_error=False)


class _FakeSocket:
    """Watcher-hub subscriber that collects every published event so tests
    assert delivery to the GM-panel transport — not just a logger call.
    Same pattern as the 61-4 / 61-3 tests."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any]) -> None:
        self.events.append(data)


@pytest.fixture
async def bound_hub() -> WatcherHub:
    """Bind the watcher hub to the test loop and clear subscribers."""
    watcher_hub.bind_loop(asyncio.get_running_loop())
    async with watcher_hub._lock:  # noqa: SLF001
        watcher_hub._subscribers.clear()  # noqa: SLF001
    return watcher_hub


def _build_client(sdk: _Sdk) -> AnthropicSdkClient:
    return AnthropicSdkClient(sdk=sdk, cache_ttl="1h")


def _usage_events(sock: _FakeSocket) -> list[dict[str, Any]]:
    return [e for e in sock.events if e.get("event_type") == USAGE_EVENT]


# ---------------------------------------------------------------------------
# AC-1 + AC-4 — the wiring test: event reaches the watcher transport as info
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sdk_usage_event_reaches_watcher_transport_as_info(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """AC-1 + AC-4 (mandatory wiring test). A simple single-call narration
    turn driven through the production ``complete_with_tools`` path MUST
    publish exactly one ``narrator.sdk.usage`` event to watcher subscribers,
    tagged ``severity="info"`` and ``component="narrator.sdk"``. Captured at
    the hub boundary, not at the helper call — proves real wiring."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sock = _FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    client = _build_client(_Sdk(responses=[_text_resp(input_tokens=12_000, output_tokens=500)]))
    await client.complete_with_tools(
        system_blocks=_system_blocks(),
        messages=_user_msg(),
        tools=[],
        model="claude-sonnet-4-6",
        session_id="61-followup-B-wiring",
    )
    await asyncio.sleep(0.05)

    events = _usage_events(sock)
    assert len(events) == 1, (
        "Exactly one narrator.sdk.usage event must reach watcher subscribers "
        f"per SDK call; got {len(events)} "
        f"(all events: {[e.get('event_type') for e in sock.events]})."
    )
    assert events[0].get("severity") == "info", (
        "narrator.sdk.usage is the continuous baseline signal — it MUST be "
        f"severity='info' (NOT the 61-4 'warn'). Got {events[0].get('severity')!r}."
    )
    assert events[0].get("component") == USAGE_COMPONENT, (
        "Component must be 'narrator.sdk' to group with the 60-7 cache event "
        f"in the Subsystems tab. Got {events[0].get('component')!r}."
    )


# ---------------------------------------------------------------------------
# AC-2 — payload carries all six contract fields with correct values + names
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sdk_usage_event_payload_carries_all_six_fields(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """AC-2. The event payload MUST carry exactly the six contract fields with
    correct values. The field is named ``cost_usd`` (the local variable is
    ``cost`` — an easy mis-map), and ``cost_usd`` is a float, not a
    preformatted string."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sock = _FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    model = "claude-sonnet-4-6"
    client = _build_client(
        _Sdk(
            responses=[
                _text_resp(
                    input_tokens=12_000,
                    output_tokens=500,
                    cache_read=80,
                    cache_write=40,
                    model=model,
                )
            ]
        )
    )
    await client.complete_with_tools(
        system_blocks=_system_blocks(),
        messages=_user_msg(),
        tools=[],
        model=model,
        session_id="61-followup-B-payload",
    )
    await asyncio.sleep(0.05)

    events = _usage_events(sock)
    assert len(events) == 1, f"need one usage event; got {len(events)}"
    fields = events[0].get("fields", {})

    for key in REQUIRED_FIELDS:
        assert key in fields, (
            f"narrator.sdk.usage payload must carry '{key}'. Got fields: {sorted(fields)}"
        )

    assert "cost" not in fields, (
        "Field must be named 'cost_usd' (the watcher contract), not the local "
        "variable name 'cost'. Found a stray 'cost' key — mis-mapped."
    )

    assert fields["input_tokens"] == 12_000, fields
    assert fields["output_tokens"] == 500, fields
    assert fields["cache_read_tokens"] == 80, fields
    assert fields["cache_write_tokens"] == 40, fields
    assert fields["model"] == model, fields

    expected_cost = compute_cost_usd(
        input_tokens=12_000,
        output_tokens=500,
        cached_input_read_tokens=80,
        cached_input_write_tokens=40,
        model=model,
    )
    assert isinstance(fields["cost_usd"], float), (
        f"cost_usd must be a float for trend plotting, got {type(fields['cost_usd'])}"
    )
    assert fields["cost_usd"] == pytest.approx(expected_cost), (
        f"cost_usd must equal compute_cost_usd(...)={expected_cost}; got {fields['cost_usd']}"
    )


# ---------------------------------------------------------------------------
# AC-2 edge — zero-valued cache fields are present (not omitted)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sdk_usage_event_includes_zero_cache_fields(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """AC-2 edge case. A cache-cold call (no read, no write) must still emit
    ``cache_read_tokens=0`` and ``cache_write_tokens=0`` — present, not
    dropped — so the GM panel never sees a gap in the trend."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sock = _FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    client = _build_client(
        _Sdk(
            responses=[_text_resp(input_tokens=300, output_tokens=20, cache_read=0, cache_write=0)]
        )
    )
    await client.complete_with_tools(
        system_blocks=_system_blocks(),
        messages=_user_msg(),
        tools=[],
        model="claude-sonnet-4-6",
        session_id="61-followup-B-zero",
    )
    await asyncio.sleep(0.05)

    events = _usage_events(sock)
    assert len(events) == 1, f"need one usage event; got {len(events)}"
    fields = events[0]["fields"]
    assert fields["cache_read_tokens"] == 0, fields
    assert fields["cache_write_tokens"] == 0, fields


# ---------------------------------------------------------------------------
# AC-3 — cadence: one event per tool-use iteration, with per-iter values
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sdk_usage_event_fires_once_per_tool_iteration(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """AC-3 (locked resolution). A two-iteration tool-use turn (tool_use →
    end_turn) MUST emit two ``narrator.sdk.usage`` events — one per SDK call —
    each carrying ITS OWN iteration's token shape, not the turn aggregate. The
    continuous baseline the 61-4 alarm feeds is per-call, so this event is
    per-call too."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sock = _FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    client = _build_client(
        _Sdk(
            responses=[
                _tool_use_resp(input_tokens=200, output_tokens=15),
                _text_resp(input_tokens=220, output_tokens=10),
            ]
        )
    )
    await client.complete_with_tools(
        system_blocks=_system_blocks(),
        messages=_user_msg(),
        tools=_roll_tool(),
        tool_dispatch=_dispatch,
        model="claude-sonnet-4-6",
        session_id="61-followup-B-loop",
    )
    await asyncio.sleep(0.05)

    events = _usage_events(sock)
    assert len(events) == 2, (
        "A two-iteration tool-use turn must emit one narrator.sdk.usage event "
        f"per SDK call (2), not a single per-turn rollup. Got {len(events)}."
    )
    # Per-iteration values, in call order — NOT the cumulative turn total (420/25).
    assert events[0]["fields"]["input_tokens"] == 200, events[0]["fields"]
    assert events[0]["fields"]["output_tokens"] == 15, events[0]["fields"]
    assert events[1]["fields"]["input_tokens"] == 220, events[1]["fields"]
    assert events[1]["fields"]["output_tokens"] == 10, events[1]["fields"]


# ---------------------------------------------------------------------------
# AC-5 — regression: simple turn, payload correctness end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_simple_turn_emits_usage_event_with_matching_payload(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """AC-5 (regression). A minimal narration turn produces exactly one usage
    event whose token fields match the synthesized SDK usage — the
    payload-correctness guard distinct from AC-4's transport-reachability
    guard."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sock = _FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    client = _build_client(_Sdk(responses=[_text_resp(input_tokens=4_096, output_tokens=128)]))
    await client.complete_with_tools(
        system_blocks=_system_blocks(),
        messages=_user_msg(),
        tools=[],
        model="claude-sonnet-4-6",
        session_id="61-followup-B-regression",
    )
    await asyncio.sleep(0.05)

    events = _usage_events(sock)
    assert len(events) == 1, f"need one usage event; got {len(events)}"
    fields = events[0]["fields"]
    assert fields["input_tokens"] == 4_096, fields
    assert fields["output_tokens"] == 128, fields
    assert fields["cost_usd"] > 0.0, (
        f"a non-empty call must report positive cost_usd; got {fields['cost_usd']}"
    )


# ---------------------------------------------------------------------------
# AC-6 — OTEL span carries cost/token/model attributes for dashboard filtering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_request_span_carries_cost_fields_for_filtering(
    monkeypatch: pytest.MonkeyPatch,
    otel_capture: Any,
) -> None:
    """AC-6. The per-call ``llm.request`` span MUST expose the cost/token/model
    attributes so the GM dashboard can filter the trend. (Regression guard: the
    span already seeds ``llm.model`` and ``llm.cost_usd`` today — this test
    pins those attributes against future regression.)"""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    model = "claude-sonnet-4-6"
    client = _build_client(
        _Sdk(
            responses=[
                _text_resp(input_tokens=12_000, output_tokens=500, cache_read=80, model=model)
            ]
        )
    )
    await client.complete_with_tools(
        system_blocks=_system_blocks(),
        messages=_user_msg(),
        tools=[],
        model=model,
        session_id="61-followup-B-span",
    )

    spans = [s for s in otel_capture.get_finished_spans() if s.name == "llm.request"]
    assert spans, "expected an llm.request span"
    attrs = dict(spans[-1].attributes or {})

    for attr in ("llm.input_tokens", "llm.output_tokens", "llm.cost_usd", "llm.model"):
        assert attr in attrs, (
            f"llm.request span must carry '{attr}' for dashboard filtering. "
            f"Got attributes: {sorted(attrs)}"
        )
    assert attrs["llm.model"] == model, attrs
    assert attrs["llm.input_tokens"] == 12_000, attrs
    assert attrs["llm.output_tokens"] == 500, attrs
    assert isinstance(attrs["llm.cost_usd"], float), attrs
