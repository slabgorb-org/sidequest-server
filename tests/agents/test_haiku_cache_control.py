"""Haiku prompt-caching for the single-shot SDK adapters (llm_factory).

The cache-aware narrator client (``anthropic_sdk_client``) only ever runs
Sonnet/Opus. Every Haiku path is a single-shot adapter in ``llm_factory`` that
passed ``system=<bare string>`` with NO ``cache_control`` — so Haiku's cache
rate was 0% by construction and the Intent Router (ADR-113), which fires once
per player turn, re-billed its full ~2,760-token static system prompt every
turn.

These tests pin the fix:

* The Intent Router adapter (``_IntentRouterLlm.emit_tool``) marks its static
  system prompt as a 1h ephemeral cache block. One marker caches the whole
  tools+system prefix (~4,730 tok), which clears Haiku 4.5's 4,096-token
  cacheable-prefix floor. The system prompt ALONE (~2,760 tok) is below the
  floor — the margin comes entirely from bundling the DispatchPackage schema.
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

    adapter = _IntentRouterLlm(session_id=None)
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
    """The Haiku call threads cache-read usage onto its llm.request span.

    This verifies the telemetry PLUMBING only: a (mocked) ``cache_read`` from
    the SDK response is propagated to ``llm.cached_input_read_tokens`` on the
    span. It does NOT prove the cache engaged — that is confirmed against the
    live API (the manual two-turn cache-read check), not this unit test. The
    span is the field the GM panel reads to watch cache-read go non-zero on
    turn 2+ in production.
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

    Marking a prefix below Haiku 4.5's 4,096-token floor is accepted by the API
    but silently does not cache. Pinning the bare-string shape prevents a
    well-meaning future dev from adding a marker that implies caching that
    never happens.
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

    adapter = _AsideLlm(session_id=None)
    adapter._sdk = SimpleNamespace(messages=SimpleNamespace(create=create))

    await adapter.complete(system="ASIDE-SYS", user="U")

    system_arg = create.call_args.kwargs["system"]
    assert isinstance(system_arg, str), (
        "aside system prompt is sub-floor (~361 tok); it must stay a bare string "
        "— a cache_control marker there is silently uncacheable (No Silent Fallbacks)"
    )


def test_intent_router_cacheable_prefix_tripwire() -> None:
    """Char tripwire: the cached prefix is tools+system (one marker on the
    system block caches both — canonical order tools → system → messages), and
    that COMBINED prefix must clear Haiku 4.5's 4,096-token cacheable floor or
    the cache_control marker silently no-ops (No Silent Fallbacks).

    The system prompt ALONE (~2,760 tok) is below the floor — the margin comes
    from bundling the DispatchPackage tool schema, so this guard measures the
    combined char length, NOT the system block alone (the original defect). It
    is a coarse early warning, not a proof of the exact token floor: char/token
    ratio varies, so the authoritative check is
    ``test_intent_router_prefix_token_floor_live`` below (count_tokens) plus the
    manual two-turn cache-read verification. Today the combined prefix is
    ~15,289 chars ≈ 4,730 tok; if it shrinks materially, re-verify the live
    token count before trusting the cache.
    """
    import json

    from sidequest.agents.intent_router import _SYSTEM_PROMPT, _dispatch_tool_schema

    combined_chars = len(_SYSTEM_PROMPT) + len(json.dumps(_dispatch_tool_schema()))
    # 4,096-tok floor; at the measured ~3.2 char/tok this prefix is ~4,730 tok.
    # Trip at 13,500 chars (~4,180 tok at that ratio) so a meaningful shrink
    # fails HERE and forces a live re-check rather than silently breaking cache.
    assert combined_chars >= 13_500, (
        f"Intent Router tools+system prefix is {combined_chars} chars; this risks "
        "dropping the combined prefix under Haiku 4.5's 4,096-token cacheable "
        "floor, where the cache_control marker silently no-ops. Re-verify with "
        "test_intent_router_prefix_token_floor_live before changing prompt/schema."
    )


def test_intent_router_prefix_token_floor_live() -> None:
    """Authoritative guard: the live tools+system token count must clear Haiku
    4.5's 4,096-token cacheable floor (count_tokens — the only exact source).

    Opt-in: gated on ``SIDEQUEST_VERIFY_HAIKU_CACHE_FLOOR`` so the default suite
    stays network-free; set it (with ``ANTHROPIC_API_KEY``) to authoritatively
    re-verify after changing the Intent Router prompt or DispatchPackage schema.
    Fails loud — not skips — if the flag is set but the key is missing.
    """
    import os

    if not os.environ.get("SIDEQUEST_VERIFY_HAIKU_CACHE_FLOOR"):
        pytest.skip("set SIDEQUEST_VERIFY_HAIKU_CACHE_FLOOR=1 (+ ANTHROPIC_API_KEY) to run")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.fail("SIDEQUEST_VERIFY_HAIKU_CACHE_FLOOR set but ANTHROPIC_API_KEY missing")

    from anthropic import Anthropic

    from sidequest.agents.intent_router import (
        _SYSTEM_PROMPT,
        _TOOL_DESCRIPTION,
        _TOOL_NAME,
        _dispatch_tool_schema,
    )

    result = Anthropic().messages.count_tokens(
        model=_HAIKU_MODEL,
        system=[{"type": "text", "text": _SYSTEM_PROMPT}],
        tools=[
            {
                "name": _TOOL_NAME,
                "description": _TOOL_DESCRIPTION,
                "input_schema": _dispatch_tool_schema(),
            }
        ],
        messages=[{"role": "user", "content": "x"}],
    )
    assert result.input_tokens >= 4096, (
        f"Intent Router tools+system is {result.input_tokens} tok; below Haiku "
        "4.5's 4,096-token cacheable floor the cache_control marker silently no-ops."
    )
