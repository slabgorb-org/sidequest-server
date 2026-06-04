"""Story 71-23 RED — Solo narration streaming on the SDK narrator path.

On the ADR-101 default backend (``AnthropicSdkClient``), a solo narration turn
arrives as ONE block at end-of-turn: the player submits, then stares at a stall
until the whole card appears. The UI streaming path is ALREADY built and wired
(``streamingNarration.ts`` reducer ← ``useStateMirror`` ← ``narration.delta``,
rendered by ``NarrationScroll`` via ``displayTextForTurn``) and the server
ALREADY has ``broadcast_delta`` + the ``NarrationDelta`` protocol message. The
hole is the PRODUCER on the SDK tooling path:

  * ``AnthropicSdkClient.complete_with_tools`` runs ``self._sdk.messages.create``
    (non-streaming), so ``on_text_delta`` — when supplied — fires ONCE per
    tool-loop iteration with the whole text block, not token-by-token; and
  * ``Orchestrator._run_narration_turn_sdk`` never supplies an ``on_text_delta``
    sink and ``run_narration_turn`` drops ``room`` when it routes to the SDK
    path (orchestrator.py:2898 calls ``_run_narration_turn_sdk(action, context)``
    with no ``room``), so ``broadcast_delta`` is never reached in solo SDK play.

This suite pins the contract for the GREEN phase. It is the "Task 7" that the
inline comment at orchestrator.py:2890 says is "NOT yet implemented", for solo.

These tests are SERVER-ONLY. The UI half (reducer, intake, render) is complete
and covered by sidequest-ui's existing streaming tests — verified green during
RED setup; no UI changes are in scope for this story.

Test layers
-----------
1. Client (real ``AnthropicSdkClient`` + a streaming-shaped fake SDK): the call
   routes through ``messages.stream`` so ``on_text_delta`` receives REAL token
   deltas (multiple incremental chunks), not one whole block. (AC1 granularity.)
2. Orchestrator (``FakeAnthropicSdkClient`` + a room): the SDK narration path
   wires ``on_text_delta`` → ``broadcast_delta`` with ``room`` threaded, so N
   ``NarrationDelta`` messages fan out to the room, each stamped with the turn's
   ``turn_id``, before the canonical result. (AC1 fan-out + the required wiring
   test.) The ``narration.turn`` span carries a ``delta_count`` attribute so the
   GM panel can confirm streaming engaged (OTEL Observability Principle).
3. Regression guards: when no room / no sink is wired, behavior is byte-identical
   to today — no deltas, canonical text unchanged. (AC2.)

NOTE on the assumed seam (logged as a Design Deviation): the client-level tests
mock ``messages.stream`` (the canonical Anthropic async streaming surface named
first in the story context). If GREEN routes through ``messages.create(stream=True)``
instead, the streaming fake here needs the matching shape — the *behavioral*
assertion (incremental deltas) is unchanged.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# Importing the tools package wires the 26 adapters onto default_registry — the
# SDK narration path exposes them, so the import must run for the orch tests.
import sidequest.agents.tools  # noqa: F401
from sidequest.agents.anthropic_sdk_client import AnthropicSdkClient
from sidequest.agents.orchestrator import Orchestrator, TurnContext
from sidequest.agents.tool_registry import ToolContext, default_registry
from sidequest.agents.tooling_protocol import (
    CacheableBlock,
    Message,
    ToolResultBlock,
    ToolUseBlock,
)
from sidequest.protocol.messages import NarrationDelta
from tests.agents.fakes.fake_anthropic_sdk_client import (
    FakeAnthropicSdkClient,
    ScriptedResponse,
)

# ===========================================================================
# Section A — streaming-shaped fake SDK (mirrors the AsyncAnthropic surface)
# ===========================================================================
#
# The real client touches ``self._sdk.messages.create(...)`` today. The GREEN
# implementation routes through ``self._sdk.messages.stream(...)`` — a SYNC call
# returning an async-context-manager that (a) is async-iterable over raw events
# and (b) exposes ``.text_stream`` (async iterator of str) and
# ``get_final_message()``. This fake supports BOTH the create and stream
# surfaces so the CURRENT code runs cleanly (RED) and the streaming code runs
# (GREEN).


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
class _ToolUseSdkBlock:
    type: str
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class _Resp:
    """Shape returned by both messages.create() and stream.get_final_message()."""

    content: list[Any]
    stop_reason: str
    usage: _Usage
    model: str


@dataclass
class _TextDeltaShape:
    text: str
    type: str = "text_delta"


@dataclass
class _StreamEvent:
    """One raw streaming event (content_block_delta carrying a text_delta)."""

    type: str
    delta: _TextDeltaShape


class _FakeMessageStream:
    """Mimics anthropic's MessageStream used under ``async with``.

    Supports both consumption idioms:
      * ``async for event in stream:`` — raw content_block_delta events
      * ``async for text in stream.text_stream:`` — str token deltas
    plus ``await stream.get_final_message()`` for the completed message.
    """

    def __init__(self, *, deltas: list[str], final: _Resp) -> None:
        self._deltas = deltas
        self._final = final

    async def __aenter__(self) -> _FakeMessageStream:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def __aiter__(self):
        async def _events():
            for d in self._deltas:
                yield _StreamEvent(type="content_block_delta", delta=_TextDeltaShape(text=d))

        return _events()

    @property
    def text_stream(self):
        async def _gen():
            for d in self._deltas:
                yield d

        return _gen()

    async def get_final_message(self) -> _Resp:
        return self._final


class _StreamingFakeMessages:
    def __init__(self, scripted: list[dict[str, Any]]) -> None:
        # Each scripted entry: {"deltas": [...], "final": _Resp}
        self._scripted = scripted
        self._cursor = 0
        self.create_calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _Resp:
        self.create_calls.append(kwargs)
        entry = self._scripted[self._cursor]
        self._cursor += 1
        return entry["final"]

    def stream(self, **kwargs: Any) -> _FakeMessageStream:
        self.stream_calls.append(kwargs)
        entry = self._scripted[self._cursor]
        self._cursor += 1
        return _FakeMessageStream(deltas=entry["deltas"], final=entry["final"])


class _StreamingFakeSdk:
    def __init__(self, scripted: list[dict[str, Any]]) -> None:
        self.messages = _StreamingFakeMessages(scripted)


def _text_resp(text: str, *, stop_reason: str = "end_turn") -> _Resp:
    return _Resp(
        content=[_TextBlock(type="text", text=text)],
        stop_reason=stop_reason,
        usage=_Usage(input_tokens=120, output_tokens=24),
        model="claude-sonnet-4-6",
    )


def _system() -> list[CacheableBlock]:
    return [CacheableBlock(text="SYSTEM RULES", cache=True)]


def _msgs() -> list[Message]:
    return [Message(role="user", content="What happens next?")]


# ===========================================================================
# Section B — client-level: complete_with_tools streams token deltas (AC1)
# ===========================================================================

_PROSE_DELTAS = ["The wind ", "howls. ", "The door ", "slams."]
_PROSE_FULL = "The wind howls. The door slams."


@pytest.mark.asyncio
async def test_complete_with_tools_streams_token_deltas_not_one_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1 granularity: with ``on_text_delta`` supplied, the SDK call delivers
    multiple INCREMENTAL token deltas — not a single whole-block callback.

    RED today: ``complete_with_tools`` runs ``messages.create`` (non-streaming),
    so ``on_text_delta`` fires exactly ONCE with the full text. The assertion
    ``len(sink) >= 2`` fails. GREEN routes through ``messages.stream`` and fires
    once per token delta.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    sdk = _StreamingFakeSdk([{"deltas": _PROSE_DELTAS, "final": _text_resp(_PROSE_FULL)}])
    client = AnthropicSdkClient(sdk=sdk)

    sink: list[str] = []
    result = await client.complete_with_tools(
        system_blocks=_system(),
        messages=_msgs(),
        tools=[],
        model="claude-sonnet-4-6",
        on_text_delta=sink.append,
    )

    # Token-by-token: more than one delta, and no single delta is the whole text.
    assert len(sink) >= 2, (
        f"expected incremental token deltas, got {len(sink)} callback(s): {sink!r} "
        "(SDK path is still non-streaming — messages.create fires on_text_delta once)"
    )
    assert _PROSE_FULL not in sink, "a single delta carried the whole block — not streamed"
    # Concatenation reconstructs the full prose, in order.
    assert "".join(sink) == _PROSE_FULL
    # The completed result text is still the full prose.
    assert result.text == _PROSE_FULL
    assert result.stop_reason == "end_turn"


@pytest.mark.asyncio
async def test_complete_with_tools_streaming_preserves_usage_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1 fidelity: streaming must not drop the completed message's usage /
    model — they come off ``get_final_message()`` once streaming lands.

    RED today: passes via messages.create. After GREEN it must STILL hold via
    the streamed final message (regression-proofs the cost-rollup path).
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    sdk = _StreamingFakeSdk([{"deltas": _PROSE_DELTAS, "final": _text_resp(_PROSE_FULL)}])
    client = AnthropicSdkClient(sdk=sdk)

    sink: list[str] = []
    result = await client.complete_with_tools(
        system_blocks=_system(),
        messages=_msgs(),
        tools=[],
        model="claude-sonnet-4-6",
        on_text_delta=sink.append,
    )

    assert result.text == _PROSE_FULL
    assert result.input_tokens == 120
    assert result.output_tokens == 24
    assert result.model == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_complete_with_tools_no_sink_unaffected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2: with ``on_text_delta=None`` the result is byte-identical to today —
    full prose, correct stop_reason, no error. Regression guard: streaming must
    never change the no-sink path's observable output.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    sdk = _StreamingFakeSdk([{"deltas": _PROSE_DELTAS, "final": _text_resp(_PROSE_FULL)}])
    client = AnthropicSdkClient(sdk=sdk)

    result = await client.complete_with_tools(
        system_blocks=_system(),
        messages=_msgs(),
        tools=[],
        model="claude-sonnet-4-6",
        # no on_text_delta
    )

    assert result.text == _PROSE_FULL
    assert result.stop_reason == "end_turn"


@pytest.mark.asyncio
async def test_streaming_coexists_with_tool_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SDK narrator ALWAYS exposes 26 tools — streaming must not break the
    tool loop. A first streamed message ends with stop_reason=tool_use; after
    the tool is dispatched, a second streamed message ends the turn. Prose
    deltas from BOTH iterations reach the sink.

    RED today: messages.create fires on_text_delta once per iteration with the
    whole block (so deltas are coarse). GREEN streams token deltas across both
    iterations while preserving tool dispatch.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    tool_use_resp = _Resp(
        content=[
            _ToolUseSdkBlock(
                type="tool_use",
                id="toolu_1",
                name="apply_status",
                input={"actor": "Rux", "text": "winded", "severity": "Minor"},
            )
        ],
        stop_reason="tool_use",
        usage=_Usage(input_tokens=200, output_tokens=12),
        model="claude-sonnet-4-6",
    )
    sdk = _StreamingFakeSdk(
        [
            {"deltas": ["You brace ", "yourself. "], "final": tool_use_resp},
            {"deltas": ["The door ", "gives way."], "final": _text_resp("The door gives way.")},
        ]
    )
    client = AnthropicSdkClient(sdk=sdk)

    dispatched: list[str] = []

    async def _dispatch(block: ToolUseBlock) -> ToolResultBlock:
        dispatched.append(block.name)
        return ToolResultBlock(tool_use_id=block.id, content="ok", is_error=False)

    sink: list[str] = []
    result = await client.complete_with_tools(
        system_blocks=_system(),
        messages=_msgs(),
        tools=[],
        tool_dispatch=_dispatch,
        model="claude-sonnet-4-6",
        on_text_delta=sink.append,
    )

    # The tool was dispatched during the streamed loop.
    assert dispatched == ["apply_status"]
    # Deltas streamed token-by-token across both iterations.
    assert len(sink) >= 3, f"expected streamed deltas across both iterations, got {sink!r}"
    assert "".join(sink) == "You brace yourself. The door gives way."
    # Final turn text is the last iteration's prose.
    assert result.text == "The door gives way."
    assert result.stop_reason == "end_turn"
    assert [tc.name for tc in result.tool_calls] == ["apply_status"]


# ===========================================================================
# Section C — orchestrator-level: SDK path wires deltas → broadcast_delta
# ===========================================================================


class _FakeRegistry:
    """Minimal PromptRegistry stand-in (mirrors test_narrator_sdk_hybrid_split)."""

    def compose_split(self, agent_name: str) -> tuple[str, str]:
        return ("system text", "user text")

    def compose_split_by_zone(self, agent_name: str):
        from sidequest.agents.prompt_framework.types import AttentionZone

        return ({AttentionZone.Primacy: "system text"}, "user text")

    def registry(self, agent_name: str) -> list:
        return []


class _RoomWithPlayer:
    """SessionRoom stand-in with one connected player; captures broadcasts.

    Matches the real room API used by broadcast_delta:
      connected_player_ids() / socket_for_player() / queue_for_socket().
    """

    def __init__(self) -> None:
        self.q: asyncio.Queue = asyncio.Queue()
        self._socket_id = "sock-1"
        self._player_id = "p-1"

    def connected_player_ids(self) -> list[str]:
        return [self._player_id]

    def socket_for_player(self, pid: str) -> str | None:
        return self._socket_id if pid == self._player_id else None

    def queue_for_socket(self, socket_id: str):
        return self.q if socket_id == self._socket_id else None

    def drain(self) -> list[Any]:
        out: list[Any] = []
        while not self.q.empty():
            out.append(self.q.get_nowait())
        return out


def _streaming_tooling_client(deltas: list[str], *, text: str) -> FakeAnthropicSdkClient:
    """A ToolingLlmClient double that fires on_text_delta once per delta.

    ``FakeAnthropicSdkClient`` already replays ``stream_deltas`` through
    ``on_text_delta`` — so it stands in for a streaming SDK client at the
    orchestrator seam without coupling to the real SDK shape.
    """
    return FakeAnthropicSdkClient(
        responses=[
            ScriptedResponse(
                text=text,
                stop_reason="end_turn",
                input_tokens=120,
                output_tokens=24,
                cached_input_read_tokens=0,
                cached_input_write_tokens=0,
                model="claude-sonnet-4-6",
                stream_deltas=deltas,
            )
        ]
    )


def _patch_orch_internals(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_build_prompt(
        self: Orchestrator, action: str, context: TurnContext
    ) -> tuple[str, _FakeRegistry]:
        return ("prompt-text", _FakeRegistry())

    monkeypatch.setattr(Orchestrator, "build_narrator_prompt", _fake_build_prompt)

    async def _spy_dispatch(block: ToolUseBlock, ctx: ToolContext) -> ToolResultBlock:
        return ToolResultBlock(tool_use_id=block.id, content="ok", is_error=False)

    monkeypatch.setattr(default_registry, "dispatch", _spy_dispatch)


@pytest.mark.asyncio
async def test_sdk_path_fans_out_deltas_to_room(
    monkeypatch: pytest.MonkeyPatch, otel_capture: InMemorySpanExporter
) -> None:
    """AC1 + REQUIRED WIRING: a solo SDK narration turn with a room fans out one
    ``NarrationDelta`` per prose chunk to the room, each stamped with the turn's
    ``turn_id`` (= str(turn_number)), in seq order, before the canonical result.

    RED today: ``run_narration_turn`` routes to ``_run_narration_turn_sdk(action,
    context)`` WITHOUT room (orchestrator.py:2898) and the SDK path supplies no
    ``on_text_delta`` sink, so ``broadcast_delta`` is never reached — the room
    queue is empty.
    """
    monkeypatch.delenv("SIDEQUEST_NARRATOR_STREAMING", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _patch_orch_internals(monkeypatch)

    client = _streaming_tooling_client(
        ["The wind howls. ", "The door slams."], text="The wind howls. The door slams."
    )
    orch = Orchestrator(client=client)
    room = _RoomWithPlayer()
    ctx = TurnContext(character_name="Rux", genre="caverns_and_claudes", turn_number=3)

    result = await orch.run_narration_turn("I look around the room.", ctx, room=room)

    deltas = room.drain()
    assert all(isinstance(m, NarrationDelta) for m in deltas), (
        f"room received non-delta messages: {[type(m).__name__ for m in deltas]}"
    )
    assert len(deltas) == 2, (
        f"expected 2 NarrationDelta fan-outs, got {len(deltas)} "
        "(SDK path does not wire on_text_delta → broadcast_delta yet)"
    )
    # turn_id stamped consistently with the streaming path (str(turn_number)).
    assert {m.payload.turn_id for m in deltas} == {"3"}
    # seq monotonic from 0; chunks reconstruct the prose in order.
    assert [m.payload.seq for m in deltas] == [0, 1]
    assert [m.payload.chunk for m in deltas] == ["The wind howls. ", "The door slams."]
    # The canonical turn result still carries the full prose.
    assert result.narration == "The wind howls. The door slams."


@pytest.mark.asyncio
async def test_sdk_path_emits_delta_count_on_narration_turn_span(
    monkeypatch: pytest.MonkeyPatch, otel_capture: InMemorySpanExporter
) -> None:
    """OTEL Observability Principle (lie-detector wiring): the SDK streaming
    fan-out must be OBSERVABLE — the ``narration.turn`` span carries a
    ``narration.turn.delta_count`` attribute so the GM panel can confirm
    streaming engaged this turn (and how many deltas shipped).

    RED today: no deltas stream and the attribute is absent.
    """
    monkeypatch.delenv("SIDEQUEST_NARRATOR_STREAMING", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _patch_orch_internals(monkeypatch)

    client = _streaming_tooling_client(
        ["Phosphor moss ", "glows green."], text="Phosphor moss glows green."
    )
    orch = Orchestrator(client=client)
    room = _RoomWithPlayer()
    ctx = TurnContext(character_name="Rux", genre="caverns_and_claudes", turn_number=4)

    await orch.run_narration_turn("I light the lamp.", ctx, room=room)

    spans = [s for s in otel_capture.get_finished_spans() if s.name == "narration.turn"]
    assert len(spans) == 1
    attrs = dict(spans[0].attributes or {})
    assert "narration.turn.delta_count" in attrs, (
        "SDK streaming path must stamp delta_count on the narration.turn span "
        "so the GM panel can verify streaming engaged"
    )
    assert attrs["narration.turn.delta_count"] == 2


@pytest.mark.asyncio
async def test_sdk_path_no_room_broadcasts_nothing(
    monkeypatch: pytest.MonkeyPatch, otel_capture: InMemorySpanExporter
) -> None:
    """AC2 regression guard: a solo SDK turn with NO room must not attempt any
    fan-out and must return the unchanged canonical prose. Streaming is a
    presentation channel gated on a room being present (mirrors the claude -p
    streaming path's ``if room is not None`` guard).
    """
    monkeypatch.delenv("SIDEQUEST_NARRATOR_STREAMING", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _patch_orch_internals(monkeypatch)

    broadcasts: list[dict[str, Any]] = []

    async def _spy_broadcast_delta(*, turn_id, chunk, seq, room):
        broadcasts.append({"turn_id": turn_id, "chunk": chunk, "seq": seq})

    # Patch at the emitter source AND the orchestrator module (the streaming
    # path imports it function-locally; the SDK path will too).
    monkeypatch.setattr(
        "sidequest.server.emitters.broadcast_delta", _spy_broadcast_delta, raising=True
    )
    import sidequest.agents.orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "broadcast_delta", _spy_broadcast_delta, raising=False)

    client = _streaming_tooling_client(["No one ", "is watching."], text="No one is watching.")
    orch = Orchestrator(client=client)
    ctx = TurnContext(character_name="Rux", genre="caverns_and_claudes", turn_number=5)

    result = await orch.run_narration_turn("I wait alone.", ctx)  # no room

    assert broadcasts == [], "no room → broadcast_delta must not be called"
    assert result.narration == "No one is watching."


class _MultiPlayerRoom:
    """SessionRoom stand-in with TWO connected players (a true shared-room MP
    session, not the parallel-solo case). Captures broadcasts per player so the
    test can prove whether raw deltas leaked to either socket."""

    def __init__(self) -> None:
        self._players = ["p-1", "p-2"]
        self._sockets = {"p-1": "sock-1", "p-2": "sock-2"}
        self.queues: dict[str, asyncio.Queue] = {
            "sock-1": asyncio.Queue(),
            "sock-2": asyncio.Queue(),
        }

    def connected_player_ids(self) -> list[str]:
        return list(self._players)

    def socket_for_player(self, pid: str) -> str | None:
        return self._sockets.get(pid)

    def queue_for_socket(self, socket_id: str):
        return self.queues.get(socket_id)

    def drain_all(self) -> list[Any]:
        out: list[Any] = []
        for q in self.queues.values():
            while not q.empty():
                out.append(q.get_nowait())
        return out


@pytest.mark.asyncio
async def test_sdk_path_does_not_stream_raw_deltas_in_multiplayer_room(
    monkeypatch: pytest.MonkeyPatch, otel_capture: InMemorySpanExporter
) -> None:
    """REWORK (Reviewer F1, HIGH): the SDK streaming fan-out must be gated to
    SOLO. In a ``>1``-connected (multiplayer) room, raw ``NarrationDelta`` chunks
    must NOT be broadcast — they are unfiltered, non-POV-swapped prose, and the
    canonical path (websocket_session_handler.py:1493) explicitly calls that raw
    bypass "a firewall+POV breach" for ``len(connected) > 1``. This story is
    scoped solo-only ("Out of scope: MP streaming"); per-recipient MP delta
    streaming is deferred. The canonical NARRATION (projected + POV-swapped per
    recipient) remains the authoritative MP delivery.

    RED after rejection: the sink fires for ANY non-None room, so a 2-player room
    leaks 2 raw deltas to each socket. GREEN gates the sink on a solo room
    (``<= 1`` connected player), mirroring the canonical path's ``> 1`` check.
    """
    monkeypatch.delenv("SIDEQUEST_NARRATOR_STREAMING", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _patch_orch_internals(monkeypatch)

    client = _streaming_tooling_client(
        ["A secret door ", "clicks open."], text="A secret door clicks open."
    )
    orch = Orchestrator(client=client)
    room = _MultiPlayerRoom()
    ctx = TurnContext(character_name="Rux", genre="caverns_and_claudes", turn_number=7)

    result = await orch.run_narration_turn("I search the wall.", ctx, room=room)

    # No raw deltas may reach ANY socket in a multiplayer room.
    leaked = room.drain_all()
    assert leaked == [], (
        f"raw NarrationDelta leaked to MP sockets (firewall+POV breach): "
        f"{[getattr(m, 'payload', m) for m in leaked]}"
    )

    # The GM panel must show streaming did NOT engage for MP this turn.
    spans = [s for s in otel_capture.get_finished_spans() if s.name == "narration.turn"]
    assert len(spans) == 1
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("narration.turn.delta_count") == 0, (
        "MP turn must report delta_count=0 — streaming is solo-only"
    )

    # The turn still completes; the canonical narration is the MP delivery path.
    assert result.narration == "A secret door clicks open."
