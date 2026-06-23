"""Story 153-11 — harden empty-prose-on-continuation UPSTREAM of the degraded stall.

sq-playtest 2026-06-20 (oz/Fate, finding NARRATOR-EMPTY-NARRATION-DEGRADED): on a
*continuation* turn the SDK narrator returned EMPTY player-facing prose. The
``_guard_empty_narration`` degraded-stall guard recovered cleanly (no client hang) —
but the guard is a SYMPTOM net. It owns the player-facing harm; it does NOT explain
WHY the prose came back empty. The existing guard's own docstring says so:

    "Defect #1 (this guard): the player-facing hang. Defect #2 (WHY the prose came
     back empty — tool-only response / prose in the wrong field) is separately tracked."

This story IS defect #2. The SDK tool-use loop can converge (``end_turn``) with WRITE
tools fired but NO final text block — the "tool-only response": the model narrated
nothing after acting. Today ``_assemble_turn_result_sdk`` reads ``result.text == ""``
and silently produces an empty ``NarrationTurnResult``; the ONLY thing that reacts is
the downstream guard, which trips the stall. The upstream origin is never made loud or
categorized — a silent gap (CLAUDE.md "No Silent Fallbacks") and an OTEL blind spot
(the GM panel can see the *symptom* span ``narrator.empty_narration`` but not the
*cause*).

The hardening (UPSTREAM of, and distinct from, the degraded-stall guard): when the SDK
narration path returns empty final prose, emit a LOUD, categorizing signal —

  * OTEL span ``narrator.empty_prose_upstream`` carrying ``cause`` (one of
    ``"tool_only_response"`` when tools fired, ``"no_output"`` when none did),
    ``tool_call_count``, ``raw_len``, ``turn_number``; and
  * a GM-panel watcher event ``narrator_empty_prose_upstream`` (component
    ``orchestrator``, severity ``warn``) carrying the same root-cause detail.

The degraded-stall guard is UNCHANGED and MUST still recover (AC1): the upstream signal
is ADDITIVE — it makes the guard a true last resort by naming the cause before the
symptom net fires. This story does NOT add a reprompt-for-prose recovery (the guard
remains the recovery path); see Delivery Findings for that design fork.

Scope: the detection is SDK-path-only. "Continuation" is a tool-loop concept — the
synchronous (``ClaudeClient``) path has no tool-use continuation to categorize and is
left to the existing downstream guard alone (test_empty_narration_guard.py).

Contract names asserted below — the Dev implements to these:
  span:  narrator.empty_prose_upstream   (attrs: cause, tool_call_count, raw_len, turn_number)
  event: narrator_empty_prose_upstream   (component=orchestrator, severity=warn)
  cause: "tool_only_response" | "no_output"
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# Importing the tools package wires the 26 adapters onto default_registry,
# matching the production SDK path's expectations.
import sidequest.agents.tools  # noqa: F401
from sidequest.agents.claude_client import ClaudeResponse
from sidequest.agents.orchestrator import NarrationTurnResult, Orchestrator, TurnContext
from sidequest.agents.tool_registry import ToolContext, default_registry
from sidequest.agents.tooling_protocol import ToolResultBlock, ToolUseBlock
from sidequest.telemetry.watcher_hub import WatcherHub, watcher_hub
from tests._helpers.doubles import FakeSocket
from tests.agents.fakes.fake_anthropic_sdk_client import (
    FakeAnthropicSdkClient,
    ScriptedResponse,
)

# --- the contract under test (Dev implements code that fires these) ---------
UPSTREAM_SPAN = "narrator.empty_prose_upstream"
UPSTREAM_EVENT = "narrator_empty_prose_upstream"
SYMPTOM_SPAN = "narrator.empty_narration"
CAUSE_TOOL_ONLY = "tool_only_response"
CAUSE_NO_OUTPUT = "no_output"

_APPLY_STATUS = ToolUseBlock(
    id="toolu_status_1",
    name="apply_status",
    arguments={"actor": "Kael", "text": "Bleeding gash", "severity": "Wound"},
)


def _response(
    *,
    text: str,
    stop_reason: str,
    tool_uses: list[ToolUseBlock] | None = None,
) -> ScriptedResponse:
    return ScriptedResponse(
        text=text,
        stop_reason=stop_reason,
        input_tokens=200,
        output_tokens=12,
        cached_input_read_tokens=0,
        cached_input_write_tokens=0,
        model="claude-sonnet-4-6",
        tool_uses=tool_uses or [],
    )


class _FakeRegistry:
    """Minimal PromptRegistry stand-in (mirrors test_narrator_sdk_hybrid_split)."""

    def compose_split(self, agent_name: str) -> tuple[str, str]:
        return ("system text", "user text")

    def compose_split_by_zone(self, agent_name: str):
        from sidequest.agents.prompt_framework.types import AttentionZone

        return ({AttentionZone.Primacy: "system text"}, "user text")

    def registry(self, agent_name: str) -> list:
        return []


def _tool_only_empty_client() -> FakeAnthropicSdkClient:
    """A tool_use round (apply_status) THEN an empty end_turn continuation.

    The fake returns ``ToolingResult(text="", tool_calls=[apply_status])`` — the exact
    "tool-only response" the playtest hit: the model acted, then narrated nothing.
    """
    return FakeAnthropicSdkClient(
        responses=[
            _response(text="", stop_reason="tool_use", tool_uses=[_APPLY_STATUS]),
            _response(text="", stop_reason="end_turn"),
        ]
    )


async def _drive_sdk_turn(
    monkeypatch: pytest.MonkeyPatch,
    client: FakeAnthropicSdkClient,
    *,
    action: str = "I continue to use the oil can.",
    turn_number: int = 2,
) -> NarrationTurnResult:
    """Run the action through the real ``run_narration_turn`` SDK path with ``client``."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    orch = Orchestrator(client=client)

    async def _spy_dispatch(block: ToolUseBlock, ctx: ToolContext) -> ToolResultBlock:
        return ToolResultBlock(tool_use_id=block.id, content="ok", is_error=False)

    monkeypatch.setattr(default_registry, "dispatch", _spy_dispatch)

    async def _fake_build_prompt(
        self: Orchestrator, action: str, context: TurnContext
    ) -> tuple[str, _FakeRegistry]:
        return ("prompt-text", _FakeRegistry())

    monkeypatch.setattr(Orchestrator, "build_narrator_prompt", _fake_build_prompt)

    ctx = TurnContext(character_name="Kael", genre="caverns_and_claudes", turn_number=turn_number)
    return await orch.run_narration_turn(action, ctx)


def _upstream_spans(otel_capture: InMemorySpanExporter) -> list:
    return [s for s in otel_capture.get_finished_spans() if s.name == UPSTREAM_SPAN]


@pytest.fixture
async def bound_hub() -> WatcherHub:
    """Bind the watcher hub to the test loop and clear subscribers (test_61_3 pattern)."""
    watcher_hub.bind_loop(asyncio.get_running_loop())
    async with watcher_hub._lock:  # noqa: SLF001
        watcher_hub._subscribers.clear()  # noqa: SLF001
    return watcher_hub


# ---------------------------------------------------------------------------
# AC2 — empty prose on a tool-use continuation is detected UPSTREAM with a span.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_only_continuation_emits_upstream_span(
    monkeypatch: pytest.MonkeyPatch, otel_capture: InMemorySpanExporter
) -> None:
    """Tools fired + empty final prose → exactly one ``narrator.empty_prose_upstream``
    span with ``cause=tool_only_response`` and ``tool_call_count=1``.

    This is the playtest repro: the model called apply_status, then ended the turn
    with no text. The cause must be auditable on the GM panel, distinct from the
    downstream symptom span.
    """
    await _drive_sdk_turn(monkeypatch, _tool_only_empty_client())

    spans = _upstream_spans(otel_capture)
    assert len(spans) == 1, (
        "an empty-prose tool-use continuation must fire exactly one "
        f"{UPSTREAM_SPAN} span; got {len(spans)} "
        f"(all: {[s.name for s in otel_capture.get_finished_spans()]})"
    )
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("cause") == CAUSE_TOOL_ONLY, (
        f"tools fired + empty prose is a {CAUSE_TOOL_ONLY!r}; got cause={attrs.get('cause')!r}"
    )
    assert attrs.get("tool_call_count") == 1, (
        f"the upstream signal must carry the tool-call count; got {attrs.get('tool_call_count')!r}"
    )
    assert attrs.get("raw_len") == 0, (
        f"empty final prose has raw_len 0; got {attrs.get('raw_len')!r}"
    )
    assert attrs.get("turn_number") == 2, (
        f"the signal must carry the turn number for GM-panel correlation; "
        f"got {attrs.get('turn_number')!r}"
    )


# ---------------------------------------------------------------------------
# AC4 — the upstream condition reaches the GM-panel transport (wiring test).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_only_continuation_emits_watcher_event_to_gm_panel(
    monkeypatch: pytest.MonkeyPatch, bound_hub: WatcherHub
) -> None:
    """The upstream detection MUST publish ``narrator_empty_prose_upstream`` to a live
    watcher subscriber — not just log. This is the CLAUDE.md "every test suite needs a
    wiring test" gate: drive the real ``run_narration_turn`` SDK path, subscribe a
    socket to the live hub, prove the GM-panel event arrives with the root cause.
    """
    sock = FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    await _drive_sdk_turn(monkeypatch, _tool_only_empty_client())
    await asyncio.sleep(0.05)

    events = [e for e in sock.events if e.get("event_type") == UPSTREAM_EVENT]
    assert len(events) == 1, (
        f"exactly one {UPSTREAM_EVENT} event must reach watcher subscribers; "
        f"got {len(events)} (all: {[e.get('event_type') for e in sock.events]})"
    )
    event = events[0]
    assert event.get("severity") == "warn", (
        "an empty-prose turn that the guard still recovers is a recoverable "
        f"condition → severity 'warn'; got {event.get('severity')!r}"
    )
    assert event.get("component") == "orchestrator", (
        f"the upstream detection lives in the orchestrator; got {event.get('component')!r}"
    )
    fields = event.get("fields", {})
    assert fields.get("cause") == CAUSE_TOOL_ONLY, (
        f"the GM panel needs the root cause; got fields={fields!r}"
    )
    assert fields.get("tool_call_count") == 1, (
        f"the GM panel needs the tool-call count; got fields={fields!r}"
    )


# ---------------------------------------------------------------------------
# AC2 — the zero-tool empty case is categorized DISTINCTLY (no_output).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_with_no_tools_emits_upstream_span_no_output(
    monkeypatch: pytest.MonkeyPatch, otel_capture: InMemorySpanExporter
) -> None:
    """Empty final prose with NO tool calls is a different cause (``no_output``): the
    model returned nothing at all, rather than acting-then-not-narrating. The upstream
    signal must distinguish the two so the GM panel can tell them apart.
    """
    client = FakeAnthropicSdkClient(responses=[_response(text="", stop_reason="end_turn")])

    await _drive_sdk_turn(monkeypatch, client)

    spans = _upstream_spans(otel_capture)
    assert len(spans) == 1, f"expected one {UPSTREAM_SPAN} span; got {len(spans)}"
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("cause") == CAUSE_NO_OUTPUT, (
        f"no tools + empty prose is {CAUSE_NO_OUTPUT!r}; got cause={attrs.get('cause')!r}"
    )
    assert attrs.get("tool_call_count") == 0, (
        f"no tools fired → tool_call_count 0; got {attrs.get('tool_call_count')!r}"
    )


# ---------------------------------------------------------------------------
# AC3 — whitespace-only prose trips the same upstream detection (stripped-empty
#        semantics, matching the downstream guard's .strip() keying).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_whitespace_only_continuation_trips_upstream_detection(
    monkeypatch: pytest.MonkeyPatch, otel_capture: InMemorySpanExporter
) -> None:
    """A markup/whitespace residual that strips to empty is still empty prose. The
    upstream detection must key on the STRIPPED text (like ``_guard_empty_narration``),
    not raw length — the reported playtest turn logged raw len=20 that stripped to 0.
    """
    client = FakeAnthropicSdkClient(
        responses=[
            _response(text="", stop_reason="tool_use", tool_uses=[_APPLY_STATUS]),
            _response(text="   \n  \t  \n", stop_reason="end_turn"),
        ]
    )

    await _drive_sdk_turn(monkeypatch, client)

    spans = _upstream_spans(otel_capture)
    assert len(spans) == 1, (
        f"whitespace-only prose strips to empty and must trip {UPSTREAM_SPAN}; "
        f"got {len(spans)} span(s)"
    )
    assert dict(spans[0].attributes or {}).get("cause") == CAUSE_TOOL_ONLY


# ---------------------------------------------------------------------------
# AC1 — the degraded-stall guard is UNCHANGED and still recovers. The upstream
#        signal is additive: BOTH spans fire, distinct, and the player surface
#        stays renderable.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upstream_detection_coexists_with_degraded_stall_guard(
    monkeypatch: pytest.MonkeyPatch, otel_capture: InMemorySpanExporter
) -> None:
    """The hardening must NOT weaken the guard. On the same empty turn:

    * the NEW upstream cause span (``narrator.empty_prose_upstream``) fires, AND
    * the EXISTING symptom guard span (``narrator.empty_narration``) still fires, AND
    * the result is still ``is_degraded=True`` with a renderable in-fiction stall.

    Two distinct spans (cause upstream, symptom downstream) — the guard remains the
    recovery path; the upstream signal only names the cause.
    """
    result = await _drive_sdk_turn(monkeypatch, _tool_only_empty_client())

    names = [s.name for s in otel_capture.get_finished_spans()]
    assert UPSTREAM_SPAN in names, f"upstream cause span must fire; saw {names}"
    assert SYMPTOM_SPAN in names, (
        f"the degraded-stall guard must be UNCHANGED and still fire {SYMPTOM_SPAN}; saw {names}"
    )

    assert result.is_degraded is True, "the guard must still flag the empty turn degraded"
    assert result.narration.strip(), "the guard must still substitute renderable stall prose"
    assert "world holds its breath" in result.narration.lower(), (
        "the in-fiction stall text must be unchanged (no client hang)"
    )


# ---------------------------------------------------------------------------
# Negative — a healthy SDK turn fires NEITHER the upstream span NOR the event.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_healthy_sdk_turn_emits_no_upstream_signal(
    monkeypatch: pytest.MonkeyPatch, bound_hub: WatcherHub, otel_capture: InMemorySpanExporter
) -> None:
    """Real prose passes through clean: no upstream cause span, no watcher event, not
    degraded. Guards against a detector that fires on every turn (false positive).
    """
    sock = FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    client = FakeAnthropicSdkClient(
        responses=[
            _response(text="The lamp sputters and the wick catches.", stop_reason="end_turn")
        ]
    )
    result = await _drive_sdk_turn(monkeypatch, client)
    await asyncio.sleep(0.05)

    assert _upstream_spans(otel_capture) == [], (
        "a healthy turn must NOT fire the upstream empty-prose cause span"
    )
    assert [e for e in sock.events if e.get("event_type") == UPSTREAM_EVENT] == [], (
        "a healthy turn must NOT publish the upstream watcher event"
    )
    assert result.is_degraded is False
    assert result.narration == "The lamp sputters and the wick catches."


# ---------------------------------------------------------------------------
# Scope — the upstream cause span is SDK-path-only. The synchronous path has no
#          tool-use continuation to categorize; its empty turns stay the
#          downstream guard's job (test_empty_narration_guard.py), NOT this one.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synchronous_empty_turn_does_not_emit_upstream_span(
    simple_turn_context, otel_capture: InMemorySpanExporter
) -> None:
    """An empty turn on the NON-tooling (ClaudeClient/sync) path must NOT fire the
    upstream cause span — there is no tool-use continuation there to categorize.

    The existing degraded-stall guard still handles it (asserted here too, so this
    test also pins that the sync path is otherwise untouched).
    """
    client = AsyncMock()
    client.send_stateless = AsyncMock(return_value=ClaudeResponse(text="", session_id=None))
    orch = Orchestrator(client=client)

    result = await orch.run_narration_turn("look around", simple_turn_context)

    assert _upstream_spans(otel_capture) == [], (
        f"{UPSTREAM_SPAN} is SDK-tool-loop-scoped; the synchronous path must not fire it"
    )
    # The sync path's empty turn is still recovered by the unchanged downstream guard.
    assert result.is_degraded is True
    assert result.narration.strip()
