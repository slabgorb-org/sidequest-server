"""Tests for claude_client.py — ClaudeClient with mocked subprocess.

No live Claude CLI calls — the subprocess spawner is mocked via a
FakeProcess + fake spawn_fn pattern.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

import pytest

from sidequest.agents import ClaudeClient, ClaudeResponse
from sidequest.agents.claude_client import (
    EmptyResponse,
    LlmClient,
    SubprocessFailed,
    TimeoutError,
)

# ---------------------------------------------------------------------------
# FakeProcess — minimal asyncio.subprocess.Process stand-in
# ---------------------------------------------------------------------------


class FakeProcess:
    """Minimal stand-in for asyncio.subprocess.Process.

    Tests control stdout, stderr, and returncode directly.
    """

    def __init__(
        self,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        delay: float = 0.0,
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._delay = delay

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._stdout, self._stderr

    def kill(self) -> None:
        pass

    async def wait(self) -> int:
        return self.returncode


def make_spawn_fn(
    stdout: bytes = b"",
    stderr: bytes = b"",
    returncode: int = 0,
    delay: float = 0.0,
    raise_exc: Exception | None = None,
) -> Callable[..., Awaitable[FakeProcess]]:
    """Build a spawn_fn that returns a FakeProcess with given attributes."""

    async def _spawn(*args: object, **kwargs: object) -> FakeProcess:
        if raise_exc is not None:
            raise raise_exc
        return FakeProcess(
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
            delay=delay,
        )

    return _spawn


class _FakeStderrStream:
    """Stream-like for FakeStreamingProcess.stderr — async ``.read()`` mirrors
    asyncio.subprocess.Process.stderr so claude_client.send_stream can capture
    stderr on subprocess_failed paths."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read(self) -> bytes:
        return self._data


class FakeStreamingProcess:
    """FakeProcess variant that emits stdout line-by-line for streaming tests.

    returncode starts as None (like a live subprocess) and is set to the
    configured exit code by wait(), matching asyncio.subprocess.Process semantics.
    """

    def __init__(
        self,
        lines: list[bytes],
        returncode: int = 0,
        per_line_delay: float = 0.0,
        stderr: bytes = b"",
    ) -> None:
        self._lines = lines
        self._final_returncode = returncode
        self.returncode: int | None = None  # None until wait() is called
        self._per_line_delay = per_line_delay
        self._stderr = stderr
        self._killed = False
        self.stdout = self  # asyncio uses `proc.stdout` as an async iterator
        self.stderr = _FakeStderrStream(stderr)

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        if self._killed or not self._lines:
            raise StopAsyncIteration
        line = self._lines.pop(0)
        if self._per_line_delay:
            await asyncio.sleep(self._per_line_delay)
        return line

    async def communicate(self) -> tuple[bytes, bytes]:
        all_remaining = b"".join(self._lines)
        self._lines = []
        return all_remaining, self._stderr

    def kill(self) -> None:
        self._killed = True

    async def wait(self) -> int:
        self.returncode = self._final_returncode
        return self._final_returncode


def make_streaming_spawn_fn(
    lines: list[bytes],
    returncode: int = 0,
    per_line_delay: float = 0.0,
    raise_exc: Exception | None = None,
) -> Callable[..., Awaitable[FakeStreamingProcess]]:
    async def _spawn(*args: object, **kwargs: object) -> FakeStreamingProcess:
        if raise_exc is not None:
            raise raise_exc
        return FakeStreamingProcess(
            lines=list(lines), returncode=returncode, per_line_delay=per_line_delay
        )

    return _spawn


def json_envelope(
    result: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    session_id: str | None = None,
    cache_create: int = 0,
    cache_read: int = 0,
    cost_usd: float | None = None,
) -> bytes:
    """Build a minimal JSON envelope like --output-format json returns."""
    payload: dict = {
        "result": result,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_input_tokens": cache_create,
            "cache_read_input_tokens": cache_read,
        },
    }
    if session_id:
        payload["session_id"] = session_id
    if cost_usd is not None:
        payload["total_cost_usd"] = cost_usd
    return json.dumps(payload).encode()


# =========================================================================
# ClaudeClient — constructor and accessors
# =========================================================================


def test_client_default_timeout():
    client = ClaudeClient()
    assert client.timeout == 120.0


def test_client_default_command_path():
    client = ClaudeClient()
    assert client.command_path == "claude"


def test_client_default_otel_endpoint_is_none():
    client = ClaudeClient()
    assert client.otel_endpoint is None


def test_client_with_timeout_sets_timeout():
    client = ClaudeClient.with_timeout(30.0)
    assert client.timeout == 30.0


# =========================================================================
# ClaudeClientBuilder
# =========================================================================


def test_builder_default_produces_default_client():
    client = ClaudeClient.builder().build()
    assert client.timeout == 120.0
    assert client.command_path == "claude"
    assert client.otel_endpoint is None


def test_builder_timeout():
    client = ClaudeClient.builder().timeout(45.0).build()
    assert client.timeout == 45.0


def test_builder_command_path():
    client = ClaudeClient.builder().command_path("/usr/local/bin/claude").build()
    assert client.command_path == "/usr/local/bin/claude"


def test_builder_otel_endpoint():
    client = ClaudeClient.builder().otel_endpoint("http://localhost:4318").build()
    assert client.otel_endpoint == "http://localhost:4318"


def test_builder_otel_endpoint_empty_string_is_none():
    client = ClaudeClient.builder().otel_endpoint("   ").build()
    assert client.otel_endpoint is None


# =========================================================================
# ClaudeClient.send_with_model — success paths
# =========================================================================


@pytest.mark.asyncio
async def test_send_with_model_returns_text():
    spawn = make_spawn_fn(stdout=json_envelope("Hello, world!"))
    client = ClaudeClient(spawn_fn=spawn)
    resp = await client.send_with_model("Say hello", "claude-3-haiku")
    assert resp.text == "Hello, world!"


@pytest.mark.asyncio
async def test_send_with_model_parses_output_tokens():
    spawn = make_spawn_fn(stdout=json_envelope("Hi", output_tokens=42))
    client = ClaudeClient(spawn_fn=spawn)
    resp = await client.send_with_model("Hi", "haiku")
    assert resp.output_tokens == 42


@pytest.mark.asyncio
async def test_send_with_model_sums_input_tokens_with_cache():
    spawn = make_spawn_fn(
        stdout=json_envelope("Hi", input_tokens=10, cache_create=500, cache_read=200)
    )
    client = ClaudeClient(spawn_fn=spawn)
    resp = await client.send_with_model("Hi", "haiku")
    assert resp.input_tokens == 710


@pytest.mark.asyncio
async def test_send_with_model_plain_text_fallback():
    """Non-JSON stdout is returned as-is (not an error)."""
    spawn = make_spawn_fn(stdout=b"plain text response")
    client = ClaudeClient(spawn_fn=spawn)
    resp = await client.send_with_model("Prompt", "haiku")
    assert resp.text == "plain text response"


@pytest.mark.asyncio
async def test_send_with_model_no_session_id_in_response():
    spawn = make_spawn_fn(stdout=json_envelope("Hello"))
    client = ClaudeClient(spawn_fn=spawn)
    resp = await client.send_with_model("Hi", "haiku")
    assert resp.session_id is None


# =========================================================================
# ClaudeClient.send_with_model — error paths
# =========================================================================


@pytest.mark.asyncio
async def test_send_with_model_empty_prompt_raises_empty_response():
    spawn = make_spawn_fn(stdout=json_envelope(""))
    client = ClaudeClient(spawn_fn=spawn)
    with pytest.raises(EmptyResponse):
        await client.send_with_model("   ", "haiku")


@pytest.mark.asyncio
async def test_send_with_model_nonzero_exit_raises_subprocess_failed():
    spawn = make_spawn_fn(returncode=1, stderr=b"error from claude")
    client = ClaudeClient(spawn_fn=spawn)
    with pytest.raises(SubprocessFailed) as exc_info:
        await client.send_with_model("Hi", "haiku")
    assert exc_info.value.exit_code == 1
    assert "error from claude" in exc_info.value.stderr


@pytest.mark.asyncio
async def test_send_with_model_spawn_failure_raises_subprocess_failed():
    spawn = make_spawn_fn(raise_exc=OSError("No such file"))
    client = ClaudeClient(spawn_fn=spawn)
    with pytest.raises(SubprocessFailed):
        await client.send_with_model("Hi", "haiku")


@pytest.mark.asyncio
async def test_send_with_model_empty_stdout_raises_empty_response():
    spawn = make_spawn_fn(stdout=b"  ")
    client = ClaudeClient(spawn_fn=spawn)
    with pytest.raises(EmptyResponse):
        await client.send_with_model("Hi", "haiku")


@pytest.mark.asyncio
async def test_send_with_model_timeout_raises_timeout_error():
    spawn = make_spawn_fn(stdout=json_envelope("Hi"), delay=1.0)
    client = ClaudeClient(timeout=0.05, spawn_fn=spawn)
    with pytest.raises(TimeoutError):
        await client.send_with_model("Hi", "haiku")


# =========================================================================
# ClaudeClient.send_with_session — persistent sessions
# =========================================================================


@pytest.mark.asyncio
async def test_send_with_session_returns_session_id():
    spawn = make_spawn_fn(stdout=json_envelope("Narration.", session_id="abc-123"))
    client = ClaudeClient(spawn_fn=spawn)
    resp = await client.send_with_session("Go north.", "opus", session_id=None)
    assert resp.session_id == "abc-123"


@pytest.mark.asyncio
async def test_send_with_session_empty_prompt_raises():
    client = ClaudeClient(spawn_fn=make_spawn_fn())
    with pytest.raises(EmptyResponse):
        await client.send_with_session("  ", "opus")


@pytest.mark.asyncio
async def test_send_with_session_resume_does_not_set_new_session_id_in_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--resume must be passed when session_id is provided."""
    captured_args: list = []

    async def capture_spawn(command: str, *args: str, **kwargs: object) -> FakeProcess:
        captured_args.extend(args)
        return FakeProcess(stdout=json_envelope("ok"))

    client = ClaudeClient(spawn_fn=capture_spawn)
    await client.send_with_session("Go north.", "opus", session_id="existing-id-42")
    assert "--resume" in captured_args
    assert "existing-id-42" in captured_args
    assert "--session-id" not in captured_args


@pytest.mark.asyncio
async def test_send_with_session_new_session_uses_session_id_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--session-id must be passed when session_id is None."""
    captured_args: list = []

    async def capture_spawn(command: str, *args: str, **kwargs: object) -> FakeProcess:
        captured_args.extend(args)
        return FakeProcess(stdout=json_envelope("ok"))

    client = ClaudeClient(spawn_fn=capture_spawn)
    await client.send_with_session("Go north.", "opus", session_id=None)
    assert "--session-id" in captured_args
    assert "--resume" not in captured_args


@pytest.mark.asyncio
async def test_send_with_session_passes_tools_empty_by_default() -> None:
    """No allowed_tools → '--tools ""' so claude CLI does not load tool schemas."""
    captured_args: list = []

    async def capture_spawn(command: str, *args: str, **kwargs: object) -> FakeProcess:
        captured_args.extend(args)
        return FakeProcess(stdout=json_envelope("ok"))

    client = ClaudeClient(spawn_fn=capture_spawn)
    await client.send_with_session("Go north.", "opus", session_id=None)
    assert "--tools" in captured_args
    idx = captured_args.index("--tools")
    assert captured_args[idx + 1] == ""
    assert "--allowedTools" not in captured_args


@pytest.mark.asyncio
async def test_send_stateless_passes_tools_empty_by_default() -> None:
    """send_stateless mirrors send_with_session: --tools '' by default."""
    captured_args: list = []

    async def capture_spawn(command: str, *args: str, **kwargs: object) -> FakeProcess:
        captured_args.extend(args)
        return FakeProcess(stdout=json_envelope("ok"))

    client = ClaudeClient(spawn_fn=capture_spawn)
    await client.send_stateless("sys", "user", "opus")
    assert "--tools" in captured_args
    idx = captured_args.index("--tools")
    assert captured_args[idx + 1] == ""


@pytest.mark.asyncio
async def test_send_stateless_passes_tools_comma_joined_when_allowed() -> None:
    """Non-empty allowed_tools → comma-joined --tools value."""
    captured_args: list = []

    async def capture_spawn(command: str, *args: str, **kwargs: object) -> FakeProcess:
        captured_args.extend(args)
        return FakeProcess(stdout=json_envelope("ok"))

    client = ClaudeClient(spawn_fn=capture_spawn)
    await client.send_stateless("sys", "user", "opus", allowed_tools=["Bash", "Read"])
    assert "--tools" in captured_args
    idx = captured_args.index("--tools")
    assert captured_args[idx + 1] == "Bash,Read"


@pytest.mark.asyncio
async def test_send_with_session_passes_system_prompt_on_new_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_args: list = []

    async def capture_spawn(command: str, *args: str, **kwargs: object) -> FakeProcess:
        captured_args.extend(args)
        return FakeProcess(stdout=json_envelope("ok"))

    client = ClaudeClient(spawn_fn=capture_spawn)
    await client.send_with_session(
        "Go north.", "opus", session_id=None, system_prompt="You are the narrator."
    )
    assert "--system-prompt" in captured_args
    idx = captured_args.index("--system-prompt")
    assert captured_args[idx + 1] == "You are the narrator."


# =========================================================================
# ClaudeClient.send (basic)
# =========================================================================


@pytest.mark.asyncio
async def test_send_returns_response():
    spawn = make_spawn_fn(stdout=json_envelope("Response text."))
    client = ClaudeClient(spawn_fn=spawn)
    resp = await client.send("A prompt.")
    assert resp.text == "Response text."


# =========================================================================
# ClaudeResponse equality
# =========================================================================


def test_claude_response_equality():
    a = ClaudeResponse(text="hi", input_tokens=5, output_tokens=10, session_id="s1")
    b = ClaudeResponse(text="hi", input_tokens=5, output_tokens=10, session_id="s1")
    assert a == b


def test_claude_response_inequality():
    a = ClaudeResponse(text="hi")
    b = ClaudeResponse(text="bye")
    assert a != b


# =========================================================================
# LlmClient protocol conformance
# =========================================================================


def test_claude_client_satisfies_llm_client_protocol():
    client = ClaudeClient()
    assert isinstance(client, LlmClient)


def test_llm_client_protocol_requires_capabilities():
    class MissingCaps:
        async def send_with_model(self, prompt: str, model: str) -> ClaudeResponse:
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
            raise NotImplementedError

    # Missing capabilities() means it does NOT satisfy the Protocol.
    assert not isinstance(MissingCaps(), LlmClient)


# =========================================================================
# Wiring test — imports from public sidequest.agents
# =========================================================================


@pytest.mark.asyncio
async def test_wiring_import_from_public_api():
    """Verify ClaudeClient is importable from sidequest.agents and exercisable."""
    from sidequest.agents import ClaudeClient

    spawn = make_spawn_fn(stdout=json_envelope("wiring ok"))
    client = ClaudeClient(spawn_fn=spawn)
    resp = await client.send_with_model("test", "haiku")
    assert resp.text == "wiring ok"


def test_claude_client_reports_capabilities():
    client = ClaudeClient()
    caps = client.capabilities()
    assert caps.supports_sessions is True
    assert caps.supports_tools is True
    assert caps.supports_streaming is True
    assert caps.max_context_tokens >= 200_000
    assert caps.backend_id == "claude-cli"


def test_llm_capabilities_is_frozen():
    from dataclasses import FrozenInstanceError

    from sidequest.agents.claude_client import LlmCapabilities

    caps = LlmCapabilities(
        backend_id="x",
        supports_sessions=True,
        supports_tools=False,
        max_context_tokens=1,
        supports_streaming=False,
    )
    with pytest.raises(FrozenInstanceError):
        caps.backend_id = "y"  # type: ignore[misc]


def test_claude_response_carries_backend_tag():
    r = ClaudeResponse(text="hi", input_tokens=1, output_tokens=1, session_id=None)
    assert r.backend == "claude-cli"


def test_claude_response_backend_accepts_override():
    r = ClaudeResponse(
        text="hi", input_tokens=1, output_tokens=1, session_id=None, backend="ollama"
    )
    assert r.backend == "ollama"


def test_claude_client_stamps_backend_on_response():
    """End-to-end: a successful send_with_model call surfaces backend='claude-cli'."""
    spawn = make_spawn_fn(stdout=json_envelope("hi back"))
    client = ClaudeClient(spawn_fn=spawn)
    response = asyncio.run(client.send_with_model("hi", model="sonnet"))
    assert response.backend == "claude-cli"
