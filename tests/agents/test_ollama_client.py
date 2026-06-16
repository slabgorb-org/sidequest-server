"""Tests for OllamaClient — HTTP backend behind LlmClient (ADR-073 Phase 2)."""

from __future__ import annotations

import asyncio
import json

import pytest

from sidequest.agents.claude_client import LlmClient
from sidequest.agents.ollama_client import OllamaClient, OllamaClientError, UnknownModel


def test_ollama_client_satisfies_llm_client_protocol():
    client = OllamaClient(base_url="http://localhost:11434")
    assert isinstance(client, LlmClient)


def test_ollama_client_reports_capabilities():
    client = OllamaClient(base_url="http://localhost:11434")
    caps = client.capabilities()
    assert caps.backend_id == "ollama"
    assert caps.supports_sessions is False
    assert caps.supports_tools is False
    assert caps.max_context_tokens == 16_384
    assert caps.supports_streaming is False


def test_unknown_model_is_ollama_client_error_subclass():
    assert issubclass(UnknownModel, OllamaClientError)


class _FakeHttpResponse:
    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _ok_generate_body(text: str, eval_count: int = 5, prompt_eval_count: int = 7) -> bytes:
    return json.dumps(
        {
            "model": "sidequest-narrator:latest",
            "response": text,
            "done": True,
            "eval_count": eval_count,
            "prompt_eval_count": prompt_eval_count,
        }
    ).encode()


def test_send_with_model_calls_api_generate_and_maps_tokens():
    calls: list[tuple[str, bytes]] = []

    def fake_http(req):
        calls.append((req.full_url, req.data or b""))
        return _FakeHttpResponse(_ok_generate_body("hello world"))

    client = OllamaClient(http_fn=fake_http)
    response = asyncio.run(client.send_with_model("hi", model="sonnet"))

    assert response.text == "hello world"
    assert response.backend == "ollama"
    assert response.input_tokens == 7
    assert response.output_tokens == 5
    assert response.session_id is None
    assert len(calls) == 1
    url, body_bytes = calls[0]
    assert url == "http://localhost:11434/api/generate"
    body = json.loads(body_bytes)
    assert body["model"] == "sidequest-narrator:latest"
    assert body["prompt"] == "hi"
    assert body["stream"] is False


def test_send_with_model_unknown_hint_raises():
    client = OllamaClient(http_fn=lambda req: pytest.fail("should not call HTTP"))
    with pytest.raises(UnknownModel):
        asyncio.run(client.send_with_model("hi", model="this-model-is-not-mapped"))


def test_send_with_model_non_200_raises_ollama_error():
    def fake_http(req):
        return _FakeHttpResponse(b"server exploded", status=500)

    client = OllamaClient(http_fn=fake_http)
    with pytest.raises(OllamaClientError):
        asyncio.run(client.send_with_model("hi", model="sonnet"))


def _ok_chat_body(text: str, eval_count: int = 5, prompt_eval_count: int = 7) -> bytes:
    return json.dumps(
        {
            "model": "sidequest-narrator:latest",
            "message": {"role": "assistant", "content": text},
            "done": True,
            "eval_count": eval_count,
            "prompt_eval_count": prompt_eval_count,
        }
    ).encode()


def test_send_with_session_establishes_new_session():
    calls: list[dict] = []

    def fake_http(req):
        body = json.loads(req.data)
        calls.append(body)
        return _FakeHttpResponse(_ok_chat_body("hi back"))

    client = OllamaClient(http_fn=fake_http)
    response = asyncio.run(
        client.send_with_session(
            prompt="hello",
            model="sonnet",
            session_id=None,
            system_prompt="you are a bot",
        )
    )

    assert response.text == "hi back"
    assert response.backend == "ollama"
    assert response.session_id is not None  # fresh uuid
    assert len(calls) == 1
    call = calls[0]
    assert call["messages"] == [
        {"role": "system", "content": "you are a bot"},
        {"role": "user", "content": "hello"},
    ]
    assert call["stream"] is False


def test_send_with_session_resume_replays_history():
    bodies = [
        _ok_chat_body("turn 1 reply"),
        _ok_chat_body("turn 2 reply"),
    ]
    calls: list[dict] = []

    def fake_http(req):
        calls.append(json.loads(req.data))
        return _FakeHttpResponse(bodies.pop(0))

    client = OllamaClient(http_fn=fake_http)
    first = asyncio.run(
        client.send_with_session(
            prompt="turn 1",
            model="sonnet",
            session_id=None,
            system_prompt="sys",
        )
    )
    sid = first.session_id
    asyncio.run(
        client.send_with_session(
            prompt="turn 2",
            model="sonnet",
            session_id=sid,
        )
    )

    # Second call must replay the full history (system, u1, a1, u2).
    assert calls[1]["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "turn 1"},
        {"role": "assistant", "content": "turn 1 reply"},
        {"role": "user", "content": "turn 2"},
    ]


def test_send_with_session_unknown_session_id_raises():
    client = OllamaClient(http_fn=lambda req: pytest.fail("no http expected"))
    with pytest.raises(OllamaClientError):
        asyncio.run(
            client.send_with_session(
                prompt="hi",
                model="sonnet",
                session_id="00000000-0000-0000-0000-000000000000",
            )
        )


def test_send_with_session_history_cap_drops_oldest_pairs(caplog):
    # Drive the client past OLLAMA_HISTORY_CAP and assert it drops oldest
    # exchanges while preserving the leading system message.
    from sidequest.agents.ollama_client import OLLAMA_HISTORY_CAP

    reply_bodies = [_ok_chat_body(f"reply {i}") for i in range(OLLAMA_HISTORY_CAP + 2)]

    def fake_http(req):
        return _FakeHttpResponse(reply_bodies.pop(0))

    client = OllamaClient(http_fn=fake_http)
    first = asyncio.run(
        client.send_with_session(
            prompt="turn 0",
            model="sonnet",
            session_id=None,
            system_prompt="sys",
        )
    )
    sid = first.session_id
    assert sid is not None
    with caplog.at_level("WARNING"):
        for i in range(1, OLLAMA_HISTORY_CAP + 2):
            asyncio.run(
                client.send_with_session(
                    prompt=f"turn {i}",
                    model="sonnet",
                    session_id=sid,
                )
            )

    history = client._histories[sid]
    # System is always first, and user+assistant pairs fit within the cap.
    assert history[0]["role"] == "system"
    assert len(history) <= OLLAMA_HISTORY_CAP * 2 + 1
    assert any("history_cap_exceeded" in rec.message for rec in caplog.records)
