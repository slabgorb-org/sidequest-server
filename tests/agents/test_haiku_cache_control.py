"""Haiku prompt-caching for the single-shot SDK adapters (llm_factory).

The cache-aware narrator client (``anthropic_sdk_client``) only ever runs
Sonnet/Opus. Every Haiku path is a single-shot adapter in ``llm_factory`` that
passed ``system=<bare string>`` with NO ``cache_control`` — so Haiku's cache
rate was 0% by construction and the Intent Router (ADR-113), which fires once
per player turn, re-billed its full ~2,760-token static system prompt every
turn.

These tests pin the fix:

* The Intent Router adapter (``_IntentRouterLlm.emit_tool``) marks its static
  system prompt as a 1h ephemeral cache block. Its prompt (~2,760 tokens)
  clears Haiku's 2,048-token cacheable-prefix floor (Sonnet/Opus floor is
  1,024), so the marker actually caches.
* The 1h TTL is a beta — without the ``extended-cache-ttl`` header the API
  400-rejects the request, so the adapter MUST send that header (No Silent
  Fallbacks). Mirrors ``anthropic_sdk_client``'s 1h path.
* The adapter emits an ``llm.request`` OTEL span carrying
  ``llm.cached_input_read_tokens`` so the GM panel (the lie detector) can
  verify caching is live on turn 2+ (OTEL Observability Principle).
* The aside resolver adapter (``_AsideLlm.complete``) is sub-floor (~361
  tokens) and stays a BARE string — adding a marker there is accepted by the
  API but silently does not cache, which would imply caching that never
  happens (No Silent Fallbacks).

Assertions are behavioral — on the payload the adapter BUILDS and the span it
EMITS — not greps of source text (CLAUDE.md "No Source-Text Wiring Tests").
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

_HAIKU_MODEL = "claude-haiku-4-5-20251001"
_EXTENDED_CACHE_TTL_BETA = "extended-cache-ttl-2025-04-11"


def _fake_tool_response(
    *,
    tool_name: str = "emit_dispatch_package",
    tool_input: dict | None = None,
    cache_read: int = 0,
    cache_write: int = 0,
) -> SimpleNamespace:
    """A synthetic Anthropic message carrying one forced ``tool_use`` block."""
    block = SimpleNamespace(
        type="tool_use",
        name=tool_name,
        input={"package": 1} if tool_input is None else tool_input,
    )
    usage = SimpleNamespace(
        input_tokens=42,
        output_tokens=7,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
    )
    return SimpleNamespace(content=[block], usage=usage, stop_reason="tool_use")


def _build_intent_adapter(monkeypatch: pytest.MonkeyPatch, create_mock: AsyncMock):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    from sidequest.agents.llm_factory import _IntentRouterLlm

    adapter = _IntentRouterLlm()
    adapter._sdk = SimpleNamespace(messages=SimpleNamespace(create=create_mock))
    return adapter


@pytest.fixture
def otel_capture() -> Iterator:
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


async def test_intent_router_marks_system_prompt_for_1h_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The static Intent Router system prompt is sent as a cached content block."""
    create = AsyncMock(return_value=_fake_tool_response(tool_input={"package": 9}))
    adapter = _build_intent_adapter(monkeypatch, create)

    out = await adapter.emit_tool(
        system="INTENT-SYS",
        user="attack the goblin",
        tool_name="emit_dispatch_package",
        tool_description="desc",
        tool_schema={"type": "object"},
    )

    assert out == {"package": 9}
    system_arg = create.call_args.kwargs["system"]
    assert isinstance(system_arg, list), (
        "system must be a content-block list to carry cache_control"
    )
    assert system_arg[0]["type"] == "text"
    assert system_arg[0]["text"] == "INTENT-SYS"
    assert system_arg[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


async def test_intent_router_sends_extended_cache_ttl_beta_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ttl:'1h' is a beta — the request must opt in via the beta header or 400."""
    create = AsyncMock(return_value=_fake_tool_response())
    adapter = _build_intent_adapter(monkeypatch, create)

    await adapter.emit_tool(
        system="S",
        user="U",
        tool_name="emit_dispatch_package",
        tool_description="d",
        tool_schema={"type": "object"},
    )

    headers = create.call_args.kwargs.get("extra_headers") or {}
    assert headers.get("anthropic-beta") == _EXTENDED_CACHE_TTL_BETA


async def test_intent_router_emits_cache_read_tokens_on_llm_request_span(
    monkeypatch: pytest.MonkeyPatch,
    otel_capture,
) -> None:
    """The Haiku call emits an llm.request span carrying the cache-read count.

    This is the lie-detector field — on turn 2+ it goes non-zero, proving the
    cache actually engaged rather than Claude just claiming it did.
    """
    create = AsyncMock(return_value=_fake_tool_response(cache_read=2500, cache_write=0))
    adapter = _build_intent_adapter(monkeypatch, create)

    await adapter.emit_tool(
        system="S",
        user="U",
        tool_name="emit_dispatch_package",
        tool_description="d",
        tool_schema={"type": "object"},
    )

    spans = [s for s in otel_capture.get_finished_spans() if s.name == "llm.request"]
    assert spans, "intent router Haiku call must emit an llm.request span"
    attrs = spans[-1].attributes
    assert attrs.get("llm.cached_input_read_tokens") == 2500
    assert attrs.get("llm.model") == _HAIKU_MODEL


async def test_aside_system_prompt_stays_uncached_subfloor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The aside resolver prompt is sub-floor; it must stay a bare string.

    Marking a <2,048-token prefix is accepted by the API but silently does not
    cache. Pinning the bare-string shape prevents a well-meaning future dev
    from adding a marker that implies caching that never happens.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    text_block = SimpleNamespace(
        type="text",
        text='{"answer": "yes", "outcome": "answered", "grounded_on": []}',
    )
    resp = SimpleNamespace(
        content=[text_block],
        usage=SimpleNamespace(
            input_tokens=1,
            output_tokens=1,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
        stop_reason="end_turn",
    )
    create = AsyncMock(return_value=resp)

    from sidequest.agents.llm_factory import _AsideLlm

    adapter = _AsideLlm()
    adapter._sdk = SimpleNamespace(messages=SimpleNamespace(create=create))

    await adapter.complete(system="ASIDE-SYS", user="U")

    system_arg = create.call_args.kwargs["system"]
    assert isinstance(system_arg, str), (
        "aside system prompt is sub-floor (~361 tok); it must stay a bare string "
        "— a cache_control marker there is silently uncacheable (No Silent Fallbacks)"
    )


def test_intent_router_system_prompt_clears_haiku_cache_floor() -> None:
    """Guard: the Intent Router prompt must stay above Haiku's cache floor.

    Haiku only caches a prefix of >= 2,048 tokens. At a pessimistic 4 chars/
    token the prompt must exceed 8,192 chars for the cache_control marker to
    actually cache. If a future edit trims it below the floor, the marker
    silently stops caching — fail loud here instead.
    """
    from sidequest.agents.intent_router import _SYSTEM_PROMPT

    assert len(_SYSTEM_PROMPT) >= 8192, (
        f"Intent Router system prompt is {len(_SYSTEM_PROMPT)} chars; below the "
        "Haiku cacheable-prefix floor the cache_control marker silently no-ops."
    )
