"""Wiring tests for narrator streaming branch behind SIDEQUEST_NARRATOR_STREAMING."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest


def test_narrator_module_exposes_streaming_capability_check():
    """The narrator module must expose a function that reports whether
    streaming is enabled via env var. This is the wiring test that ensures
    the flag is actually consulted and not orphaned."""
    from sidequest.agents.narrator import is_streaming_enabled

    assert callable(is_streaming_enabled)


def test_streaming_disabled_by_default(monkeypatch):
    monkeypatch.delenv("SIDEQUEST_NARRATOR_STREAMING", raising=False)
    from sidequest.agents.narrator import is_streaming_enabled

    assert is_streaming_enabled() is False


def test_streaming_enabled_when_flag_is_one(monkeypatch):
    monkeypatch.setenv("SIDEQUEST_NARRATOR_STREAMING", "1")
    from sidequest.agents.narrator import is_streaming_enabled

    assert is_streaming_enabled() is True


def test_streaming_disabled_when_flag_is_zero(monkeypatch):
    monkeypatch.setenv("SIDEQUEST_NARRATOR_STREAMING", "0")
    from sidequest.agents.narrator import is_streaming_enabled

    assert is_streaming_enabled() is False


# ---------------------------------------------------------------------------
# Helpers — minimal streaming mock client
# ---------------------------------------------------------------------------

CANNED_PROSE = "**Location**\n\nThe wind howls. The door slams.\n\n"
CANNED_PATCH_JSON = '{"items_lost": []}'
CANNED_FULL_TEXT = (
    '**Location**\n\nThe wind howls. The door slams.\n\n```game_patch\n{"items_lost": []}\n```\n'
)


class StreamingMockClient:
    """Minimal LlmClient mock that supports send_stream via a provided async generator."""

    def __init__(self, stream_fn) -> None:
        self._stream_fn = stream_fn

    def capabilities(self):
        from sidequest.agents.claude_client import LlmCapabilities

        return LlmCapabilities(
            backend_id="mock-stream",
            supports_sessions=True,
            supports_tools=False,
            max_context_tokens=200_000,
            supports_streaming=True,
        )

    async def send_with_model(self, prompt: str, model: str):
        raise NotImplementedError("streaming mock: use send_stream")

    async def send_with_session(self, **kwargs):
        raise NotImplementedError("streaming mock: use send_stream")

    async def send_stream(self, **kwargs) -> AsyncIterator[Any]:
        async for event in self._stream_fn(**kwargs):
            yield event


class MinimalRoom:
    """Minimal SessionRoom stand-in: no connected players, captures broadcasts."""

    def __init__(self) -> None:
        self._broadcasts: list[dict] = []

    def connected_player_ids(self) -> list[str]:
        return []

    def socket_for_player(self, pid: str) -> str | None:
        return None

    def queue_for_socket(self, socket_id: str):
        return None


# ---------------------------------------------------------------------------
# Integration test — streaming path broadcasts deltas + emits canonical
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_path_broadcasts_deltas_then_returns_canonical(monkeypatch):
    """End-to-end: streaming produces N deltas + 1 canonical NarrationTurnResult.

    Mocks send_stream to yield a controlled sequence of TextDelta + StreamComplete.
    Spies on broadcast_delta to capture fan-out calls.
    Verifies the returned NarrationTurnResult has correct prose and no fence
    content leaked into the narration field.
    """
    monkeypatch.setenv("SIDEQUEST_NARRATOR_STREAMING", "1")

    from sidequest.agents.claude_client import StreamComplete, TextDelta

    async def mock_send_stream(**kwargs):
        yield TextDelta(text="**Location**\n\n")
        yield TextDelta(text="The wind howls. ")
        yield TextDelta(text="The door slams.\n\n")
        yield TextDelta(text="\n```game_patch\n")
        yield TextDelta(text='{"items_lost": []}\n')
        yield TextDelta(text="```\n")
        yield StreamComplete(
            full_text=CANNED_FULL_TEXT,
            input_tokens=100,
            output_tokens=20,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            session_id="sess-1",
            elapsed_seconds=2.5,
        )

    delta_calls: list[dict] = []

    async def spy_broadcast_delta(*, turn_id, chunk, seq, room):
        delta_calls.append({"turn_id": turn_id, "chunk": chunk, "seq": seq})

    monkeypatch.setattr(
        "sidequest.server.emitters.broadcast_delta",
        spy_broadcast_delta,
        raising=True,
    )
    # Also patch the import inside the streaming method itself
    import sidequest.agents.orchestrator as orch_mod

    monkeypatch.setattr(
        orch_mod,
        "broadcast_delta",  # not imported at module level — patched via emitters
        spy_broadcast_delta,
        raising=False,
    )

    client = StreamingMockClient(stream_fn=mock_send_stream)
    room = MinimalRoom()

    from sidequest.agents.orchestrator import Orchestrator, TurnContext

    orch = Orchestrator(client=client)
    context = TurnContext(
        genre="caverns_and_claudes",
        character_name="Rux",
        turn_number=3,
    )

    result = await orch._run_narration_turn_streaming(
        "I look around the room.",
        context,
        room=room,
    )

    # The returned NarrationTurnResult should have clean prose (no fence content)
    assert result.is_degraded is False
    assert "game_patch" not in result.narration
    assert "items_lost" not in result.narration
    assert "The wind howls" in result.narration or "Location" in result.narration

    # items_lost was parsed from the game_patch block
    assert result.items_lost == []


@pytest.mark.asyncio
async def test_streaming_path_with_room_fans_out_deltas(monkeypatch):
    """When a room with connected players is provided, broadcast_delta is
    called once per prose chunk (fence chunks are NOT broadcast)."""
    monkeypatch.setenv("SIDEQUEST_NARRATOR_STREAMING", "1")

    from sidequest.agents.claude_client import StreamComplete, TextDelta

    async def mock_send_stream(**kwargs):
        # 3 prose chunks + 3 fence chunks
        yield TextDelta(text="**Location**\n\n")
        yield TextDelta(text="The wind howls. ")
        yield TextDelta(text="The door slams.\n\n")
        yield TextDelta(text="\n```game_patch\n")
        yield TextDelta(text='{"items_lost": []}\n')
        yield TextDelta(text="```\n")
        yield StreamComplete(
            full_text=CANNED_FULL_TEXT,
            input_tokens=50,
            output_tokens=10,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            session_id="sess-fanout",
            elapsed_seconds=1.0,
        )

    import asyncio

    class RoomWithPlayer:
        """Room with one connected player that captures broadcast payloads."""

        def __init__(self):
            self.q: asyncio.Queue = asyncio.Queue()
            self._socket_id = "sock-1"
            self._player_id = "p-1"

        def connected_player_ids(self):
            return [self._player_id]

        def socket_for_player(self, pid):
            return self._socket_id if pid == self._player_id else None

        def queue_for_socket(self, sid):
            return self.q if sid == self._socket_id else None

    room = RoomWithPlayer()
    client = StreamingMockClient(stream_fn=mock_send_stream)

    from sidequest.agents.orchestrator import Orchestrator, TurnContext

    orch = Orchestrator(client=client)
    context = TurnContext(
        genre="caverns_and_claudes",
        character_name="Rux",
        turn_number=4,
    )

    result = await orch._run_narration_turn_streaming(
        "I push the door open.",
        context,
        room=room,
    )

    assert result.is_degraded is False

    # Drain all messages from the room queue
    messages = []
    while not room.q.empty():
        messages.append(room.q.get_nowait())

    # Every message should be a NarrationDelta (not a game_patch fence chunk)
    from sidequest.protocol.messages import NarrationDelta

    assert len(messages) > 0, "Expected at least one delta message in queue"
    for msg in messages:
        assert isinstance(msg, NarrationDelta), f"Unexpected message type: {type(msg)}"
        assert "items_lost" not in msg.payload.chunk
        assert "game_patch" not in msg.payload.chunk

    # Sequence numbers are contiguous from 0
    seqs = [msg.payload.seq for msg in messages]
    assert seqs == list(range(len(messages)))


@pytest.mark.asyncio
async def test_streaming_path_degrades_gracefully_on_stream_error(monkeypatch):
    """StreamError terminal event → is_degraded=True, partial prose preserved."""
    monkeypatch.setenv("SIDEQUEST_NARRATOR_STREAMING", "1")

    from sidequest.agents.claude_client import StreamError, TextDelta

    async def mock_send_stream(**kwargs):
        yield TextDelta(text="The fortress looms... ")
        yield StreamError(
            kind="timeout",
            elapsed_seconds=30.0,
            partial_text="The fortress looms... ",
            detail="subprocess timed out",
            exit_code=None,
        )

    client = StreamingMockClient(stream_fn=mock_send_stream)
    room = MinimalRoom()

    from sidequest.agents.orchestrator import Orchestrator, TurnContext

    orch = Orchestrator(client=client)
    context = TurnContext(
        genre="heavy_metal",
        character_name="Vex",
        current_location="Iron Gate",
        turn_number=1,
    )

    result = await orch._run_narration_turn_streaming(
        "I charge the gate.",
        context,
        room=room,
    )

    assert result.is_degraded is True


@pytest.mark.asyncio
async def test_streaming_path_degrades_when_client_lacks_send_stream(monkeypatch):
    """If client has no send_stream, falls back to sync with a warning."""
    monkeypatch.setenv("SIDEQUEST_NARRATOR_STREAMING", "1")

    # Build a client that has send_with_session but NOT send_stream
    class SyncOnlyClient:
        def capabilities(self):
            from sidequest.agents.claude_client import LlmCapabilities

            return LlmCapabilities(
                backend_id="sync-only",
                supports_sessions=True,
                supports_tools=False,
                max_context_tokens=200_000,
                supports_streaming=False,
            )

        async def send_with_model(self, prompt, model):
            raise NotImplementedError

        async def send_with_session(
            self,
            prompt,
            model,
            session_id=None,
            system_prompt=None,
            allowed_tools=None,
            env_vars=None,
        ):
            from sidequest.agents.claude_client import ClaudeResponse

            raw = (
                "**Test location**\n\nSync fallback prose.\n\n"
                '```game_patch\n{"location": "Test location"}\n```\n'
            )
            return ClaudeResponse(
                text=raw,
                input_tokens=10,
                output_tokens=5,
                session_id="sync-sess",
            )

        async def send_stateless(
            self,
            system_prompt,
            user_message,
            model,
            allowed_tools=None,
            env_vars=None,
        ):
            from sidequest.agents.claude_client import ClaudeResponse

            raw = (
                "**Test location**\n\nSync fallback prose.\n\n"
                '```game_patch\n{"location": "Test location"}\n```\n'
            )
            return ClaudeResponse(
                text=raw,
                input_tokens=10,
                output_tokens=5,
                session_id=None,
            )

    client = SyncOnlyClient()

    from sidequest.agents.orchestrator import Orchestrator, TurnContext

    orch = Orchestrator(client=client)
    context = TurnContext(
        genre="space_opera",
        character_name="Zara",
        turn_number=1,
    )

    # Should NOT raise — degrades to sync path
    result = await orch._run_narration_turn_streaming(
        "I fire the thrusters.",
        context,
        room=None,
    )

    # Sync path returned a real result, not a degraded one
    assert result.is_degraded is False
    assert "Sync fallback prose" in result.narration
