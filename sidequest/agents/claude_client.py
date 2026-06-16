"""Claude CLI subprocess client.

Port of sidequest-agents/src/client.rs.

Port lesson #3: Single ClaudeClient with configurable timeout,
consistent error types, and a standard fallback policy.

The subprocess spawner is dependency-injected so tests can substitute
a mock without ever launching the real 'claude' binary.
"""

from __future__ import annotations

import asyncio
import builtins
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from sidequest.telemetry.spans import (
    agent_call_session_span,
    agent_call_span,
)

logger = logging.getLogger(__name__)

# Default timeout for Claude CLI invocations (120 seconds).
DEFAULT_TIMEOUT: float = 120.0

# Default command path for Claude CLI.
DEFAULT_COMMAND: str = "claude"

# Type alias for the subprocess spawner function.
# Uses Any return type so tests can inject duck-typed FakeProcess objects
# without Pyright complaining about structural subtype incompatibility.
SpawnFn = Callable[..., Awaitable[Any]]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LlmClientError(Exception):
    """Base error for any LlmClient backend (Claude CLI, Ollama, future MLX)."""


@dataclass(frozen=True, slots=True)
class LlmCapabilities:
    """Runtime capability report for an LlmClient backend."""

    backend_id: str
    supports_sessions: bool
    supports_tools: bool
    max_context_tokens: int
    supports_streaming: bool


class ClaudeClientError(LlmClientError):
    """Base error from Claude CLI subprocess invocations."""


class TimeoutError(ClaudeClientError):
    """The subprocess exceeded the configured timeout."""

    def __init__(self, elapsed: float) -> None:
        self.elapsed = elapsed
        super().__init__(f"Claude CLI timed out after {elapsed:.1f}s")


class SubprocessFailed(ClaudeClientError):
    """The subprocess exited with a non-zero status."""

    def __init__(self, exit_code: int | None, stderr: str) -> None:
        self.exit_code = exit_code
        self.stderr = stderr
        super().__init__(f"Claude CLI failed (exit code: {exit_code}): {stderr}")


class EmptyResponse(ClaudeClientError):
    """The subprocess returned an empty response."""

    def __init__(self) -> None:
        super().__init__("Claude CLI returned an empty response")


# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------


class ClaudeResponse:
    """Response from an LlmClient call, including token usage telemetry."""

    __slots__ = ("text", "input_tokens", "output_tokens", "session_id", "backend")

    def __init__(
        self,
        text: str,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        session_id: str | None = None,
        backend: str = "claude-cli",
    ) -> None:
        self.text = text
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.session_id = session_id
        self.backend = backend

    def __repr__(self) -> str:
        return (
            f"ClaudeResponse(text={self.text!r:.40}, "
            f"input_tokens={self.input_tokens}, output_tokens={self.output_tokens}, "
            f"session_id={self.session_id!r}, backend={self.backend!r})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ClaudeResponse):
            return NotImplemented
        return (
            self.text == other.text
            and self.input_tokens == other.input_tokens
            and self.output_tokens == other.output_tokens
            and self.session_id == other.session_id
            and self.backend == other.backend
        )


# ---------------------------------------------------------------------------
# Streaming event types — yielded from ClaudeClient.send_stream()
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """Base for events yielded from ClaudeClient.send_stream()."""


@dataclass(frozen=True, slots=True)
class TextDelta(StreamEvent):
    """An incremental chunk of assistant prose.

    Concatenating all TextDelta.text values in stream order yields the
    final response text.
    """

    text: str


@dataclass(frozen=True, slots=True)
class StreamComplete(StreamEvent):
    """Terminal event on success.

    Drop-in metadata equivalent to ClaudeResponse — input_tokens,
    output_tokens, session_id. Carries the accumulated full_text for
    callers that want it without re-concatenating deltas.
    """

    full_text: str
    input_tokens: int | None
    output_tokens: int | None
    cache_creation_input_tokens: int | None
    cache_read_input_tokens: int | None
    session_id: str | None
    elapsed_seconds: float


@dataclass(frozen=True, slots=True)
class StreamError(StreamEvent):
    """Terminal event on failure. Stream cannot continue."""

    kind: Literal["timeout", "subprocess_failed", "parse_error", "empty"]
    elapsed_seconds: float
    partial_text: str
    detail: str
    exit_code: int | None


# ---------------------------------------------------------------------------
# Default subprocess spawner
# ---------------------------------------------------------------------------


async def _default_spawn(
    command: str,
    *args: str,
    env: dict[str, str] | None = None,
    **kwargs: object,
) -> asyncio.subprocess.Process:
    """Spawn the real 'claude' CLI subprocess."""
    return await asyncio.create_subprocess_exec(
        command,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )


# ---------------------------------------------------------------------------
# ClaudeClient
# ---------------------------------------------------------------------------


class ClaudeClient:
    """Claude CLI subprocess client with configurable timeout and command path.

    Port of sidequest-agents client.rs ClaudeClient.

    The spawn_fn is dependency-injected for testability — production code passes
    None (default spawner). Tests inject a mock that returns a FakeProcess.
    """

    def __init__(
        self,
        timeout: float = DEFAULT_TIMEOUT,
        command_path: str = DEFAULT_COMMAND,
        otel_endpoint: str | None = None,
        spawn_fn: SpawnFn | None = None,
    ) -> None:
        self._timeout = timeout
        self._command_path = command_path
        self._otel_endpoint = otel_endpoint or None
        self._spawn = spawn_fn or _default_spawn

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    @property
    def timeout(self) -> float:
        return self._timeout

    @property
    def command_path(self) -> str:
        return self._command_path

    @property
    def otel_endpoint(self) -> str | None:
        return self._otel_endpoint

    def capabilities(self) -> LlmCapabilities:
        """Report Claude CLI capabilities (ADR-073 Phase 1)."""
        return LlmCapabilities(
            backend_id="claude-cli",
            supports_sessions=True,
            supports_tools=True,
            max_context_tokens=200_000,
            supports_streaming=True,
        )

    # ------------------------------------------------------------------
    # Builder-style constructors
    # ------------------------------------------------------------------

    @classmethod
    def with_timeout(
        cls,
        timeout: float,
        spawn_fn: SpawnFn | None = None,
    ) -> ClaudeClient:
        """Create a new client with a custom timeout."""
        return cls(timeout=timeout, spawn_fn=spawn_fn)

    @classmethod
    def builder(cls) -> ClaudeClientBuilder:
        """Create a builder for more complex configuration."""
        return ClaudeClientBuilder()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def send(self, prompt: str) -> ClaudeResponse:
        """Execute a subprocess call with default settings."""
        return await self._send_impl(prompt, model=None, allowed_tools=[], extra_env={})

    async def send_with_model(self, prompt: str, model: str) -> ClaudeResponse:
        """Execute a subprocess call with a specific model.

        Passes --model <model> before -p <prompt>. Returns stdout on success.
        """
        return await self._send_impl(prompt, model=model, allowed_tools=[], extra_env={})

    async def send_with_session(
        self,
        prompt: str,
        model: str,
        session_id: str | None = None,
        system_prompt: str | None = None,
        allowed_tools: list[str] | None = None,
        env_vars: dict[str, str] | None = None,
    ) -> ClaudeResponse:
        """Execute a persistent session call (ADR-066).

        If session_id is not None, resumes that session via --resume.
        If None, creates a new session with a fresh UUID via --session-id
        and includes --system-prompt for session establishment.

        Returns the session ID in ClaudeResponse.session_id for storage.
        """
        allowed = allowed_tools or []
        env = env_vars or {}

        with agent_call_session_span(
            model=model,
            prompt_len=len(prompt),
            backend="claude-cli",
        ) as span:
            if not prompt.strip():
                raise EmptyResponse()

            args: list[str] = ["--model", model]

            is_resume = session_id is not None
            if is_resume and session_id:
                args += ["--resume", session_id]
                logger.info("narrator.session_resume session_id=%s", session_id)
            else:
                new_id = str(uuid.uuid4())
                args += ["--session-id", new_id]
                if system_prompt:
                    args += ["--system-prompt", system_prompt]
                logger.info("narrator.session_create session_id=%s", new_id)

            if allowed:
                args += ["--tools", ",".join(allowed)]
            else:
                args += ["--tools", ""]

            args += ["-p", prompt, "--output-format", "json"]

            # Build env overlay
            process_env = self._build_env(env)

            return await self._run_subprocess(args, process_env, span)

    async def send_stateless(
        self,
        system_prompt: str,
        user_message: str,
        model: str,
        allowed_tools: list[str] | None = None,
        env_vars: dict[str, str] | None = None,
    ) -> ClaudeResponse:
        """Execute a stateless `claude -p` call (ADR-098).

        Neither `--resume` nor `--session-id` is passed; every call is a
        fresh, independent conversation. The system_prompt carries the
        stable scaffold (identity, voice, format); the user_message
        carries turn-dynamic state plus the player action.

        Unlike :meth:`send_with_session`, the returned
        :attr:`ClaudeResponse.session_id` is not meaningful — callers
        should ignore it.
        """
        allowed = allowed_tools or []
        env = env_vars or {}

        with agent_call_session_span(
            model=model,
            prompt_len=len(user_message),
            backend="claude-cli",
        ) as span:
            if not user_message.strip():
                raise EmptyResponse()

            args: list[str] = ["--model", model]
            if system_prompt:
                args += ["--system-prompt", system_prompt]
            if allowed:
                args += ["--tools", ",".join(allowed)]
            else:
                args += ["--tools", ""]
            args += ["-p", user_message, "--output-format", "json"]

            process_env = self._build_env(env)
            return await self._run_subprocess(args, process_env, span)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _send_impl(
        self,
        prompt: str,
        model: str | None,
        allowed_tools: list[str],
        extra_env: dict[str, str],
    ) -> ClaudeResponse:
        """Core subprocess execution used by all send methods."""
        model_label = model or "default"

        with agent_call_span(
            model=model_label, prompt_len=len(prompt), backend="claude-cli"
        ) as span:
            if not prompt.strip():
                raise EmptyResponse()

            args: list[str] = []
            if model:
                args += ["--model", model]
            if allowed_tools:
                args += ["--tools", ",".join(allowed_tools)]
            else:
                args += ["--tools", ""]
            args += ["-p", prompt, "--output-format", "json"]

            logger.debug(
                "claude_cli.command_built command=%s model=%s prompt_len=%d",
                self._command_path,
                model_label,
                len(prompt),
            )

            process_env = self._build_env(extra_env)
            return await self._run_subprocess(args, process_env, span)

    def _build_env(self, extra_env: dict[str, str]) -> dict[str, str] | None:
        """Build the environment dict for the subprocess."""
        import os

        base = dict(os.environ)
        if self._otel_endpoint:
            base.update(
                {
                    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
                    "OTEL_TRACES_EXPORTER": "otlp",
                    "OTEL_LOGS_EXPORTER": "otlp",
                    "OTEL_METRICS_EXPORTER": "otlp",
                    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
                    "OTEL_EXPORTER_OTLP_ENDPOINT": self._otel_endpoint,
                    "OTEL_LOG_TOOL_CONTENT": "1",
                    "OTEL_LOG_TOOL_DETAILS": "1",
                    "CLAUDE_CODE_OTEL_FLUSH_TIMEOUT_MS": "3000",
                }
            )
        base.update(extra_env)
        return base

    async def _spawn_subprocess(
        self,
        args: list[str],
        env: dict[str, str] | None,
    ) -> Any:
        """Spawn the claude CLI subprocess, returning the process handle.

        Caller is responsible for reading stdout/stderr and calling
        proc.wait() / proc.kill() as appropriate.
        """
        try:
            return await self._spawn(self._command_path, *args, env=env)
        except Exception as e:
            logger.error("Failed to spawn subprocess: %s", e)
            raise SubprocessFailed(exit_code=None, stderr=str(e)) from e

    async def _collect_response(
        self,
        proc: Any,
        span: object,
        start: float,
    ) -> ClaudeResponse:
        """Wait for proc to finish, parse the JSON envelope, return ClaudeResponse.

        Used by the synchronous send_*() entry points. The streaming
        variant uses _iterate_stream() instead.
        """
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=self._timeout,
            )
        except builtins.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            elapsed = time.monotonic() - start
            logger.warning(
                "Claude CLI subprocess timed out after %.1fs (timeout=%.1fs)",
                elapsed,
                self._timeout,
            )
            raise TimeoutError(elapsed=elapsed) from None

        elapsed = time.monotonic() - start
        returncode = proc.returncode

        stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
        stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""

        if returncode != 0:
            raise SubprocessFailed(exit_code=returncode, stderr=stderr)

        trimmed = stdout.strip()
        if not trimmed:
            raise EmptyResponse()

        # Parse JSON envelope from --output-format json (existing logic moved verbatim)
        return self._parse_json_envelope(trimmed, elapsed, span)

    def _parse_json_envelope(
        self,
        trimmed: str,
        elapsed: float,
        span: object,
    ) -> ClaudeResponse:
        """Parse the JSON envelope returned by claude --output-format json.

        Extracts token usage, session_id, and result text. Used by the
        synchronous path only — the streaming path produces StreamComplete
        directly from accumulated NDJSON events.
        """
        input_tokens: int | None = None
        output_tokens: int | None = None
        response_session_id: str | None = None

        try:
            envelope = json.loads(trimmed)
        except json.JSONDecodeError:
            # Not JSON (shouldn't happen with --output-format json, but handle gracefully)
            text = trimmed
        else:
            # Extract token counts from usage block.
            # Prompt caching (ADR-066) sends most input tokens through
            # cache_creation_input_tokens / cache_read_input_tokens fields.
            # Sum all three so the GM panel shows the real input cost.
            usage = envelope.get("usage")
            if usage:
                raw_in = int(usage.get("input_tokens") or 0)
                cache_create = int(usage.get("cache_creation_input_tokens") or 0)
                cache_read = int(usage.get("cache_read_input_tokens") or 0)
                total_in = raw_in + cache_create + cache_read
                if total_in > 0:
                    input_tokens = total_in
                out = usage.get("output_tokens")
                if out is not None:
                    output_tokens = int(out)

            sid = envelope.get("session_id")
            if sid:
                response_session_id = str(sid)

            result = envelope.get("result")
            text = result if isinstance(result, str) else trimmed

        if not text:
            raise EmptyResponse()

        logger.debug(
            "claude_cli.complete elapsed=%.2fs response_len=%d",
            elapsed,
            len(text),
        )

        return ClaudeResponse(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            session_id=response_session_id,
            backend="claude-cli",
        )

    async def _run_subprocess(
        self,
        args: list[str],
        env: dict[str, str] | None,
        span: object,
    ) -> ClaudeResponse:
        """Spawn + collect — kept as a thin wrapper for the synchronous path."""
        start = time.monotonic()
        proc = await self._spawn_subprocess(args, env)
        return await self._collect_response(proc, span, start)

    async def _iterate_stream(
        self,
        proc: Any,
        span: object,
        start: float,
    ) -> AsyncIterator[StreamEvent]:
        """Consume the subprocess stdout as NDJSON events; yield StreamEvents.

        Always terminates with exactly one StreamComplete or StreamError.
        On iterator cancellation (aclose / break / raise), the subprocess
        is killed in the finally block.
        """
        from sidequest.agents.claude_stream_parser import (
            extract_terminal_metadata,
            extract_text_delta,
            is_terminal_event,
        )

        accumulated = ""
        terminal_meta = None  # TerminalMetadata | None

        try:
            try:
                async with asyncio.timeout(self._timeout):
                    async for raw_line in proc.stdout:
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            logger.warning("claude_cli.stream.malformed_line line=%r", line[:200])
                            continue

                        delta_text = extract_text_delta(event)
                        if delta_text is not None:
                            accumulated += delta_text
                            yield TextDelta(text=delta_text)
                            continue

                        if is_terminal_event(event):
                            terminal_meta = extract_terminal_metadata(event)
                            continue
            except builtins.TimeoutError:
                elapsed = time.monotonic() - start
                yield StreamError(
                    kind="timeout",
                    elapsed_seconds=elapsed,
                    partial_text=accumulated,
                    detail=f"claude CLI timed out after {elapsed:.1f}s",
                    exit_code=None,
                )
                return

            await proc.wait()
            elapsed = time.monotonic() - start
            returncode = proc.returncode

            if returncode != 0:
                stderr_text = ""
                if proc.stderr is not None:
                    try:
                        stderr_bytes = await proc.stderr.read()
                        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()[:4096]
                    except Exception:
                        stderr_text = ""
                detail = f"claude CLI exited with code {returncode}"
                if stderr_text:
                    detail = f"{detail} stderr={stderr_text}"
                yield StreamError(
                    kind="subprocess_failed",
                    elapsed_seconds=elapsed,
                    partial_text=accumulated,
                    detail=detail,
                    exit_code=returncode,
                )
                return

            if not accumulated and terminal_meta is None:
                yield StreamError(
                    kind="empty",
                    elapsed_seconds=elapsed,
                    partial_text="",
                    detail="claude CLI returned no output",
                    exit_code=returncode,
                )
                return

            if terminal_meta is None:
                yield StreamComplete(
                    full_text=accumulated,
                    input_tokens=None,
                    output_tokens=None,
                    cache_creation_input_tokens=None,
                    cache_read_input_tokens=None,
                    session_id=None,
                    elapsed_seconds=elapsed,
                )
                return

            yield StreamComplete(
                full_text=terminal_meta.full_text or accumulated,
                input_tokens=terminal_meta.input_tokens,
                output_tokens=terminal_meta.output_tokens,
                cache_creation_input_tokens=terminal_meta.cache_creation_input_tokens,
                cache_read_input_tokens=terminal_meta.cache_read_input_tokens,
                session_id=terminal_meta.session_id,
                elapsed_seconds=elapsed,
            )
        finally:
            # Cancel-safety: if the consumer abandons us via aclose / break,
            # the subprocess may still be alive. Reap it.
            try:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
            except Exception:
                pass

    async def send_stream(
        self,
        prompt: str,
        model: str,
        session_id: str | None = None,
        system_prompt: str | None = None,
        allowed_tools: list[str] | None = None,
        env_vars: dict[str, str] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Streaming variant of send_with_session.

        Spawns claude with --output-format stream-json, parses NDJSON line-by-line,
        yields TextDelta events as they arrive. Terminates with StreamComplete
        on success or StreamError on failure.
        """
        allowed = allowed_tools or []
        env = env_vars or {}

        with agent_call_session_span(
            model=model, prompt_len=len(prompt), backend="claude-cli"
        ) as span:
            if not prompt.strip():
                yield StreamError(
                    kind="empty",
                    elapsed_seconds=0.0,
                    partial_text="",
                    detail="empty prompt",
                    exit_code=None,
                )
                return

            args: list[str] = ["--model", model]
            is_resume = session_id is not None
            if is_resume and session_id:
                args += ["--resume", session_id]
            else:
                new_id = str(uuid.uuid4())
                args += ["--session-id", new_id]
                if system_prompt:
                    args += ["--system-prompt", system_prompt]

            if allowed:
                args += ["--tools", ",".join(allowed)]
            else:
                args += ["--tools", ""]

            args += ["-p", prompt, "--output-format", "stream-json", "--verbose"]

            process_env = self._build_env(env)
            start = time.monotonic()
            proc = await self._spawn_subprocess(args, process_env)

            inner = self._iterate_stream(proc, span, start)
            try:
                async for event in inner:
                    yield event
            finally:
                await inner.aclose()


# ---------------------------------------------------------------------------
# ClaudeClientBuilder
# ---------------------------------------------------------------------------


class ClaudeClientBuilder:
    """Builder for ClaudeClient configuration."""

    def __init__(self) -> None:
        self._timeout = DEFAULT_TIMEOUT
        self._command_path = DEFAULT_COMMAND
        self._otel_endpoint: str | None = None
        self._spawn_fn: SpawnFn | None = None

    def timeout(self, timeout: float) -> ClaudeClientBuilder:
        """Set the timeout duration."""
        self._timeout = timeout
        return self

    def command_path(self, path: str) -> ClaudeClientBuilder:
        """Set the command path."""
        self._command_path = path
        return self

    def otel_endpoint(self, endpoint: str) -> ClaudeClientBuilder:
        """Set the OTEL endpoint for Claude subprocess telemetry export.

        Empty strings are normalized to None.
        """
        self._otel_endpoint = endpoint.strip() or None
        return self

    def spawn_fn(self, fn: SpawnFn) -> ClaudeClientBuilder:
        """Inject a custom subprocess spawner (for testing)."""
        self._spawn_fn = fn
        return self

    def build(self) -> ClaudeClient:
        """Build the ClaudeClient."""
        return ClaudeClient(
            timeout=self._timeout,
            command_path=self._command_path,
            otel_endpoint=self._otel_endpoint,
            spawn_fn=self._spawn_fn,
        )


# ---------------------------------------------------------------------------
# LlmClient protocol (ADR-073 Phase 1 — generalised from ClaudeLike)
# ---------------------------------------------------------------------------


@runtime_checkable
class LlmClient(Protocol):
    """Object-safe abstraction over any LLM client backend.

    Production code takes LlmClient so tests can substitute a mock and so
    alternative backends (Ollama, MLX) can slot in via `build_llm_client`.
    Maps to ADR-073 Phase 1 LlmClient trait.
    """

    def capabilities(self) -> LlmCapabilities:
        """Report backend capabilities (ADR-073 Phase 1)."""
        ...

    async def send_with_model(self, prompt: str, model: str) -> ClaudeResponse:
        """Execute a one-shot call with an explicit model."""
        ...

    async def send_with_session(
        self,
        prompt: str,
        model: str,
        session_id: str | None = None,
        system_prompt: str | None = None,
        allowed_tools: list[str] | None = None,
        env_vars: dict[str, str] | None = None,
    ) -> ClaudeResponse:
        """Execute a persistent-session call (ADR-066)."""
        ...

    async def send_stateless(
        self,
        system_prompt: str,
        user_message: str,
        model: str,
        allowed_tools: list[str] | None = None,
        env_vars: dict[str, str] | None = None,
    ) -> ClaudeResponse:
        """Execute a stateless call with no --resume or --session-id (ADR-098)."""
        ...
