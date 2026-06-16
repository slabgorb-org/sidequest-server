"""Ollama HTTP backend for LlmClient (ADR-073 Phase 2)."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable, Iterable
from typing import Any
from urllib.request import Request, urlopen

from sidequest.agents.claude_client import (
    ClaudeResponse,
    LlmCapabilities,
    LlmClientError,
)
from sidequest.telemetry.spans import agent_call_session_span, agent_call_span

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_MODEL_MAP: dict[str, str] = {
    "sonnet": "sidequest-narrator:latest",
    "haiku": "sidequest-decomposer:latest",
    "opus": "sidequest-narrator:latest",
}
OLLAMA_HISTORY_CAP = 32  # exchanges (system + N*2 messages)

# urllib returns a context manager protocol (HTTPResponse); Any keeps the fake HTTP
# response class in tests compatible without needing to mirror the full type.
HttpFn = Callable[[Request], Any]


def _default_http(req: Request) -> Any:
    return urlopen(req, timeout=120)  # noqa: S310 — fixed localhost Ollama URL


def genre_model_map(genres: Iterable[str], *, base: dict[str, str] | None = None) -> dict[str, str]:
    """Build an OllamaClient model_map with genre-specialized narrator routes.

    Story 48-3 substep (e): genre-tagged requests must resolve to the
    adapter-fused model. Each genre adds a ``"genre:<slug>"`` hint mapping to
    ``"sidequest-narrator-<slug>:latest"``; the default sonnet/haiku/opus
    hints (or ``base`` if supplied) are preserved so existing routing is
    unaffected. An unmapped genre still raises ``UnknownModel`` at resolve
    time — no silent fallback to the generic narrator.
    """
    result = dict(DEFAULT_MODEL_MAP if base is None else base)
    for genre in genres:
        slug = genre.strip()
        if not slug:
            raise ValueError("genre must be a non-empty string")
        result[f"genre:{slug}"] = f"sidequest-narrator-{slug}:latest"
    return result


class OllamaClientError(LlmClientError):
    """Base error for OllamaClient."""


class UnknownModel(OllamaClientError):
    """Caller asked for a model hint not present in the Ollama model map."""


class OllamaClient:
    """HTTP client against an Ollama server (ADR-073 Phase 2).

    Sessions are simulated client-side: each session_id maps to an in-process
    chat history, replayed on subsequent send_with_session calls.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_OLLAMA_URL,
        model_map: dict[str, str] | None = None,
        http_fn: HttpFn | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model_map: dict[str, str] = dict(model_map or DEFAULT_MODEL_MAP)
        self._histories: dict[str, list[dict[str, str]]] = {}
        self._http: HttpFn = http_fn or _default_http

    def capabilities(self) -> LlmCapabilities:
        return LlmCapabilities(
            backend_id="ollama",
            supports_sessions=False,
            supports_tools=False,
            max_context_tokens=16_384,
            supports_streaming=False,
        )

    def _resolve_model(self, hint: str) -> str:
        resolved = self._model_map.get(hint)
        if resolved is None:
            raise UnknownModel(
                f"model hint {hint!r} not in Ollama model_map keys={sorted(self._model_map.keys())}"
            )
        return resolved

    async def send_with_model(self, prompt: str, model: str) -> ClaudeResponse:
        local_model = self._resolve_model(model)
        with agent_call_span(model=local_model, prompt_len=len(prompt), backend="ollama"):
            body = {"model": local_model, "prompt": prompt, "stream": False}
            return await asyncio.to_thread(self._post_generate, body)

    def _post_generate(self, body: dict[str, object]) -> ClaudeResponse:
        req = Request(
            f"{self._base_url}/api/generate",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._http(req) as resp:
                status = getattr(resp, "status", 200)
                payload = resp.read()
        except Exception as exc:
            raise OllamaClientError(f"ollama /api/generate transport error: {exc}") from exc
        if status != 200:
            raise OllamaClientError(f"ollama /api/generate HTTP {status}: {payload!r:.200}")
        try:
            envelope = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise OllamaClientError(f"ollama /api/generate non-json body: {exc}") from exc
        return ClaudeResponse(
            text=envelope.get("response", ""),
            input_tokens=envelope.get("prompt_eval_count"),
            output_tokens=envelope.get("eval_count"),
            session_id=None,
            backend="ollama",
        )

    async def send_with_session(
        self,
        prompt: str,
        model: str,
        session_id: str | None = None,
        system_prompt: str | None = None,
        allowed_tools: list[str] | None = None,  # noqa: ARG002 — ollama ignores tools
        env_vars: dict[str, str] | None = None,  # noqa: ARG002 — ollama ignores env vars
    ) -> ClaudeResponse:
        local_model = self._resolve_model(model)

        if session_id is None:
            new_id = str(uuid.uuid4())
            history: list[dict[str, str]] = []
            if system_prompt:
                history.append({"role": "system", "content": system_prompt})
            history.append({"role": "user", "content": prompt})
            self._histories[new_id] = history
            session_to_return = new_id
        else:
            existing = self._histories.get(session_id)
            if existing is None:
                raise OllamaClientError(
                    f"ollama session_id {session_id!r} is not known to this client "
                    f"(process restart clears session state)"
                )
            existing.append({"role": "user", "content": prompt})
            session_to_return = session_id

        # Enforce cap: keep the leading system message plus the most recent
        # (cap * 2) user+assistant messages.
        self._cap_history(self._histories[session_to_return])

        with agent_call_session_span(model=local_model, prompt_len=len(prompt), backend="ollama"):
            body = {
                "model": local_model,
                "messages": list(self._histories[session_to_return]),
                "stream": False,
            }
            response = await asyncio.to_thread(self._post_chat, body)

        # Append assistant reply to history for next turn.
        self._histories[session_to_return].append({"role": "assistant", "content": response.text})
        self._cap_history(self._histories[session_to_return])

        return ClaudeResponse(
            text=response.text,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            session_id=session_to_return,
            backend="ollama",
        )

    async def send_stateless(
        self,
        system_prompt: str,
        user_message: str,
        model: str,
        allowed_tools: list[str] | None = None,
        env_vars: dict[str, str] | None = None,
    ) -> ClaudeResponse:
        """Stateless send. Ollama has no native session concept, so we
        compose system_prompt and user_message into a single prompt."""
        combined = f"{system_prompt}\n\n{user_message}" if system_prompt else user_message
        return await self.send_with_session(
            prompt=combined,
            model=model,
            session_id=None,
            system_prompt=None,
            allowed_tools=allowed_tools,
            env_vars=env_vars,
        )

    def _cap_history(self, history: list[dict[str, str]]) -> None:
        """Keep leading system message + most recent exchanges up to cap."""
        max_total = OLLAMA_HISTORY_CAP * 2 + (
            1 if history and history[0]["role"] == "system" else 0
        )
        if len(history) <= max_total:
            return
        logger.warning("ollama.history_cap_exceeded len=%d cap=%d", len(history), max_total)
        if history and history[0]["role"] == "system":
            system = history[0]
            tail = history[-(OLLAMA_HISTORY_CAP * 2) :]
            history[:] = [system, *tail]
        else:
            history[:] = history[-(OLLAMA_HISTORY_CAP * 2) :]

    def _post_chat(self, body: dict[str, object]) -> ClaudeResponse:
        req = Request(
            f"{self._base_url}/api/chat",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._http(req) as resp:
                status = getattr(resp, "status", 200)
                payload = resp.read()
        except Exception as exc:
            raise OllamaClientError(f"ollama /api/chat transport error: {exc}") from exc
        if status != 200:
            raise OllamaClientError(f"ollama /api/chat HTTP {status}: {payload!r:.200}")
        try:
            envelope = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise OllamaClientError(f"ollama /api/chat non-json body: {exc}") from exc
        message = envelope.get("message") or {}
        return ClaudeResponse(
            text=message.get("content", ""),
            input_tokens=envelope.get("prompt_eval_count"),
            output_tokens=envelope.get("eval_count"),
            session_id=None,  # filled in by caller
            backend="ollama",
        )
