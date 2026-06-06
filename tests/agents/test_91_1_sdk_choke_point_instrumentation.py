"""Story 91-1 — Single SDK choke point + universal usage instrumentation (RED).

Epic 91 ("Dark Spend") keystone. Cost forensics (pingpong [COST-1], 2026-06-05)
found ~97% of Haiku spend emits neither a usage log line nor an OTEL
``llm.request`` span — the adapters (``_AsideLlm``, ``_IntentRouterLlm``) were
wired ad hoc with their own ``AsyncAnthropic`` constructions and partial (or
absent) telemetry. These tests pin the consolidation contract:

**AC-1 — Single construction site.** ``llm_factory.build_async_anthropic()``
is the SOLE ``AsyncAnthropic`` construction seam. Every consumer — the aside
adapter, the intent-router adapter, and the narrator's ``AnthropicSdkClient``
(when no ``sdk=`` is injected) — obtains its SDK by calling that seam,
late-bound through the module dict so a monkeypatched fake is what every
adapter receives. An adapter that bypassed the factory would construct a real
``AsyncAnthropic`` instead of the sentinel and fail here. This is the
load-bearing wiring assertion (server CLAUDE.md "No Source-Text Wiring
Tests" — no repo-grep; behavior only).

**AC-2 — Uniform usage log line on EVERY call.** Each SDK call emits an
INFO-level ``*.sdk.usage``-shaped log line carrying ``caller=<tag>``,
``model=<model id>``, fresh/cached token split (``input=``, ``output=``,
``cache_read=``, ``cache_write=``), and ``cost_usd=`` computed via
``anthropic_cost.compute_cost_usd``. The narrator's existing
``narrator.sdk.usage`` line is the baseline — it must GAIN ``caller`` and
``model`` (it has neither today).

**AC-3 — ``llm.request`` span with a caller tag on EVERY call.** Each call
runs inside an ``llm.request`` span carrying ``llm.caller`` plus the existing
token attributes and ``llm.cost_usd``. Today ``_AsideLlm.complete`` opens NO
span and the intent-router span carries neither caller nor cost.

**AC-4 — ``cost_usd`` via ``compute_cost_usd`` for all models.** Expected
values in these tests are computed by calling the real pricing function —
correct for Haiku, Sonnet, and Opus. A missing ``resp.usage`` is a real
condition that must raise (No Silent Fallbacks), never a zero-cost log line.

**AC-5 — No narrator behavior change** is pinned by the EXISTING narrator
suite staying green, not duplicated here.

Caller-tag taxonomy (story context): ``narrator``, ``intent_router``,
``aside``, ``dungeon_curate``. The dungeon-curate path already routes through
``AnthropicSdkClient.complete_with_tools(caller="dungeon_curate")`` — it is
inside the choke point; its tag must now reach the log line and span too.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from sidequest.agents.anthropic_cost import compute_cost_usd
from sidequest.agents.claude_client import LlmClientError
from sidequest.agents.tooling_protocol import (
    CacheableBlock,
    Message,
    ToolDefinition,
)

_HAIKU = "claude-haiku-4-5-20251001"
_SONNET = "claude-sonnet-4-6"
_OPUS = "claude-opus-4-7"

_COST_RE = re.compile(r"cost_usd=([0-9]+(?:\.[0-9]+)?)")


# ---------------------------------------------------------------------------
# Shared fakes / helpers
# ---------------------------------------------------------------------------


def _fake_usage(
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read: int,
    cache_write: int,
) -> SimpleNamespace:
    """Flat (pre-0.51 SDK) usage shape: aggregate cache_creation, no nesting."""
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
        cache_creation=None,
    )


def _aside_text_response(
    *,
    text: str = '{"answer": "Your pack is small.", "outcome": "answered", "grounded_on": ["inventory"]}',
    usage: Any = None,
) -> SimpleNamespace:
    if usage is None:
        usage = _fake_usage(input_tokens=1000, output_tokens=500, cache_read=2000, cache_write=3000)
    block = SimpleNamespace(type="text", text=text)
    return SimpleNamespace(content=[block], usage=usage, stop_reason="end_turn", model=_HAIKU)


def _router_tool_response(
    *,
    tool_name: str = "emit_dispatch_package",
    usage: Any = None,
) -> SimpleNamespace:
    if usage is None:
        usage = _fake_usage(input_tokens=4800, output_tokens=120, cache_read=4700, cache_write=0)
    block = SimpleNamespace(type="tool_use", name=tool_name, input={"package": 1})
    return SimpleNamespace(content=[block], usage=usage, stop_reason="tool_use", model=_HAIKU)


def _expected_cost(usage: Any, model: str) -> float:
    """Compute the expected cost through the REAL pricing function (AC-4)."""
    return compute_cost_usd(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cached_input_read_tokens=usage.cache_read_input_tokens,
        cached_input_write_tokens=usage.cache_creation_input_tokens,
        model=model,
    )


def _fake_sdk(responses: list[Any]) -> SimpleNamespace:
    """Duck-typed AsyncAnthropic: ``.messages.create`` pops scripted responses."""
    create = AsyncMock(side_effect=list(responses))
    return SimpleNamespace(messages=SimpleNamespace(create=create))


def _usage_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if ".sdk.usage" in r.getMessage() and r.levelno == logging.INFO
    ]


def _usage_line_for_caller(caplog: pytest.LogCaptureFixture, caller: str) -> str:
    matching = [m for m in _usage_lines(caplog) if f"caller={caller}" in m]
    assert matching, (
        f"no INFO '*.sdk.usage' log line carrying caller={caller}; "
        f"usage lines seen: {_usage_lines(caplog)!r}"
    )
    return matching[-1]


def _parsed_cost(line: str) -> float:
    m = _COST_RE.search(line)
    assert m, f"usage log line carries no cost_usd= field: {line!r}"
    return float(m.group(1))


@pytest.fixture
def otel_capture() -> Iterator[Any]:
    """In-memory span exporter on the global provider (test_haiku_cache_control
    pattern)."""
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


def _llm_request_spans(exporter: Any, *, caller: str | None = None) -> list[Any]:
    spans = [s for s in exporter.get_finished_spans() if s.name == "llm.request"]
    if caller is not None:
        spans = [s for s in spans if s.attributes.get("llm.caller") == caller]
    return spans


def _build_aside_adapter(monkeypatch: pytest.MonkeyPatch, sdk: Any) -> Any:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    from sidequest.agents.llm_factory import _AsideLlm

    adapter = _AsideLlm(session_id=None)
    adapter._sdk = sdk
    return adapter


def _build_router_adapter(monkeypatch: pytest.MonkeyPatch, sdk: Any) -> Any:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    from sidequest.agents.llm_factory import _IntentRouterLlm

    adapter = _IntentRouterLlm(session_id=None)
    adapter._sdk = sdk
    return adapter


async def _drive_router(adapter: Any) -> dict[str, Any]:
    return await adapter.emit_tool(
        system="S",
        user="attack the goblin",
        tool_name="emit_dispatch_package",
        tool_description="d",
        tool_schema={"type": "object"},
    )


# --- narrator (real complete_with_tools) harness ---------------------------


@dataclass(frozen=True)
class _NarratorResp:
    content: list[Any]
    stop_reason: str
    usage: Any
    model: str


def _narrator_text_response(*, model: str) -> _NarratorResp:
    return _NarratorResp(
        content=[SimpleNamespace(type="text", text="The door creaks open.")],
        stop_reason="end_turn",
        usage=_fake_usage(
            input_tokens=11_000, output_tokens=400, cache_read=9_000, cache_write=2_500
        ),
        model=model,
    )


def _build_narrator_client(monkeypatch: pytest.MonkeyPatch, sdk: Any) -> Any:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    from sidequest.agents.anthropic_sdk_client import AnthropicSdkClient

    return AnthropicSdkClient(sdk=sdk, cache_ttl="1h")


async def _drive_narrator(client: Any, *, model: str, caller: str) -> Any:
    return await client.complete_with_tools(
        [CacheableBlock(text="rules", cache=True)],
        [Message(role="user", content="go")],
        [ToolDefinition(name="roll_dice", description="Roll", input_schema={"type": "object"})],
        None,
        model=model,
        caller=caller,
    )


# ===========================================================================
# AC-1 — single construction site (the wiring tests)
# ===========================================================================


def test_factory_exposes_single_sdk_construction_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``llm_factory.build_async_anthropic`` exists, is callable with no
    arguments, and returns an SDK exposing ``.messages`` (the surface every
    consumer uses)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    from sidequest.agents import llm_factory

    seam = getattr(llm_factory, "build_async_anthropic", None)
    assert callable(seam), (
        "llm_factory.build_async_anthropic is the single AsyncAnthropic "
        "construction seam (story 91-1) — it must exist and be callable"
    )
    sdk = seam()
    assert hasattr(sdk, "messages"), (
        "the seam must return an AsyncAnthropic-shaped client (has .messages)"
    )


def test_seam_fails_loud_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """No Silent Fallbacks: a missing ANTHROPIC_API_KEY raises at the seam."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from sidequest.agents import llm_factory

    with pytest.raises(LlmClientError):
        llm_factory.build_async_anthropic()


def test_aside_adapter_obtains_sdk_through_factory_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``build_aside_llm`` must receive whatever the seam returns. A bypass
    (its own ``AsyncAnthropic(...)``) would hand it a real client, not the
    sentinel, and fail here."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    from sidequest.agents import llm_factory

    sentinel = _fake_sdk([])
    monkeypatch.setattr(llm_factory, "build_async_anthropic", lambda: sentinel)

    adapter = llm_factory.build_aside_llm(session_id=None)
    assert adapter._sdk is sentinel, (
        "_AsideLlm constructed its own SDK instead of calling llm_factory.build_async_anthropic()"
    )


def test_intent_router_adapter_obtains_sdk_through_factory_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    from sidequest.agents import llm_factory

    sentinel = _fake_sdk([])
    monkeypatch.setattr(llm_factory, "build_async_anthropic", lambda: sentinel)

    adapter = llm_factory.build_intent_router_llm(session_id=None)
    assert adapter._sdk is sentinel, (
        "_IntentRouterLlm constructed its own SDK instead of calling "
        "llm_factory.build_async_anthropic()"
    )


def test_narrator_client_obtains_sdk_through_factory_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``build_llm_client()`` (the production narrator construction path) must
    flow through the same seam when no ``sdk=`` is injected."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("SIDEQUEST_LLM_BACKEND", raising=False)
    from sidequest.agents import llm_factory

    sentinel = _fake_sdk([])
    monkeypatch.setattr(llm_factory, "build_async_anthropic", lambda: sentinel)

    client = llm_factory.build_llm_client()
    assert client._sdk is sentinel, (
        "AnthropicSdkClient constructed its own AsyncAnthropic instead of "
        "obtaining it through llm_factory.build_async_anthropic()"
    )


def test_narrator_explicit_sdk_injection_bypasses_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``sdk=`` injection kwarg (the entire fake-SDK test fleet rides on
    it) must keep winning — the seam must NOT be consulted when a client is
    handed in explicitly."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    from sidequest.agents import llm_factory
    from sidequest.agents.anthropic_sdk_client import AnthropicSdkClient

    def _boom() -> Any:
        raise AssertionError("seam must not be called when sdk= is injected")

    monkeypatch.setattr(llm_factory, "build_async_anthropic", _boom)

    injected = _fake_sdk([])
    client = AnthropicSdkClient(sdk=injected, cache_ttl="1h")
    assert client._sdk is injected


# ===========================================================================
# AC-2 — uniform usage log line on every call site
# ===========================================================================


async def test_aside_call_emits_uniform_usage_log_line(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``_AsideLlm.complete`` (today: NO log line at all) emits the uniform
    ``*.sdk.usage`` INFO line with caller, model, token split, and a cost_usd
    computed by the real pricing function."""
    caplog.set_level(logging.INFO)
    resp = _aside_text_response()
    adapter = _build_aside_adapter(monkeypatch, _fake_sdk([resp]))

    await adapter.complete(system="ASIDE-SYS", user="how big is my pack?")

    line = _usage_line_for_caller(caplog, "aside")
    assert f"model={_HAIKU}" in line
    assert "input=1000" in line
    assert "output=500" in line
    assert "cache_read=2000" in line
    assert "cache_write=3000" in line
    expected = _expected_cost(resp.usage, _HAIKU)
    assert _parsed_cost(line) == pytest.approx(expected, abs=1e-9), (
        "cost_usd in the log line must equal compute_cost_usd(...) for Haiku"
    )


async def test_intent_router_call_emits_uniform_usage_log_line(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``_IntentRouterLlm.emit_tool`` (today: span-only, no log line) emits the
    uniform line with caller=intent_router."""
    caplog.set_level(logging.INFO)
    resp = _router_tool_response()
    adapter = _build_router_adapter(monkeypatch, _fake_sdk([resp]))

    out = await _drive_router(adapter)

    assert out == {"package": 1}
    line = _usage_line_for_caller(caplog, "intent_router")
    assert f"model={_HAIKU}" in line
    assert "input=4800" in line
    assert "output=120" in line
    assert "cache_read=4700" in line
    expected = _expected_cost(resp.usage, _HAIKU)
    assert _parsed_cost(line) == pytest.approx(expected, abs=1e-9)


@pytest.mark.parametrize("caller", ["narrator", "dungeon_curate"])
async def test_narrator_usage_log_line_carries_caller_and_model(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    caller: str,
) -> None:
    """The narrator's existing per-iter ledger line gains ``caller=`` and
    ``model=`` (today it has neither). ``dungeon_curate`` parametrization
    confirms the already-inside-the-choke-point SCRATCH path's tag reaches the
    log line, not just the tool-loop span (story-context red-phase
    confirmation)."""
    caplog.set_level(logging.INFO)
    resp = _narrator_text_response(model=_SONNET)
    client = _build_narrator_client(monkeypatch, _fake_sdk([resp]))

    await _drive_narrator(client, model=_SONNET, caller=caller)

    line = _usage_line_for_caller(caplog, caller)
    assert f"model={_SONNET}" in line, (
        "the narrator usage line must carry the billed model id — log-based "
        "cost accounting is model-blind without it"
    )


# ===========================================================================
# AC-3 — llm.request span with caller tag on every call site
# ===========================================================================


async def test_aside_call_emits_llm_request_span_with_caller_and_cost(
    monkeypatch: pytest.MonkeyPatch, otel_capture: Any
) -> None:
    """``_AsideLlm.complete`` opens NO span today — ~97% of Haiku spend is
    invisible to Jaeger. It must run inside ``llm.request`` carrying the
    caller tag, token split, and cost."""
    resp = _aside_text_response()
    adapter = _build_aside_adapter(monkeypatch, _fake_sdk([resp]))

    await adapter.complete(system="S", user="U")

    spans = _llm_request_spans(otel_capture, caller="aside")
    assert spans, (
        "aside SDK call must emit an llm.request span with llm.caller='aside' "
        f"(spans seen: {[dict(s.attributes) for s in _llm_request_spans(otel_capture)]!r})"
    )
    attrs = spans[-1].attributes
    assert attrs.get("llm.model") == _HAIKU
    assert attrs.get("llm.input_tokens") == 1000
    assert attrs.get("llm.output_tokens") == 500
    assert attrs.get("llm.cached_input_read_tokens") == 2000
    assert attrs.get("llm.cached_input_write_tokens") == 3000
    assert attrs.get("llm.cost_usd") == pytest.approx(_expected_cost(resp.usage, _HAIKU), abs=1e-9)


async def test_intent_router_span_carries_caller_and_cost(
    monkeypatch: pytest.MonkeyPatch, otel_capture: Any
) -> None:
    """The intent-router span exists today but carries neither ``llm.caller``
    nor ``llm.cost_usd`` — both must land."""
    resp = _router_tool_response()
    adapter = _build_router_adapter(monkeypatch, _fake_sdk([resp]))

    await _drive_router(adapter)

    spans = _llm_request_spans(otel_capture, caller="intent_router")
    assert spans, "intent-router llm.request span must carry llm.caller='intent_router'"
    attrs = spans[-1].attributes
    assert attrs.get("llm.cost_usd") == pytest.approx(
        _expected_cost(resp.usage, _HAIKU), abs=1e-9
    ), "router span must carry llm.cost_usd via compute_cost_usd"
    assert attrs.get("llm.cached_input_read_tokens") == 4700


@pytest.mark.parametrize("caller", ["narrator", "dungeon_curate"])
async def test_narrator_llm_request_span_carries_caller(
    monkeypatch: pytest.MonkeyPatch, otel_capture: Any, caller: str
) -> None:
    """The narrator's per-iter ``llm.request`` span gains ``llm.caller`` so
    span-based attribution can split narrator from dungeon-curate traffic."""
    resp = _narrator_text_response(model=_SONNET)
    client = _build_narrator_client(monkeypatch, _fake_sdk([resp]))

    await _drive_narrator(client, model=_SONNET, caller=caller)

    spans = _llm_request_spans(otel_capture, caller=caller)
    assert spans, (
        f"narrator-path llm.request span must carry llm.caller={caller!r} "
        f"(spans seen: {[dict(s.attributes) for s in _llm_request_spans(otel_capture)]!r})"
    )


# ===========================================================================
# AC-4 — cost_usd via compute_cost_usd, correct across models; fail loud on
#         missing usage
# ===========================================================================


@pytest.mark.parametrize("model", [_SONNET, _OPUS])
async def test_narrator_span_cost_matches_pricing_table_across_models(
    monkeypatch: pytest.MonkeyPatch, otel_capture: Any, model: str
) -> None:
    """Span cost must equal the real pricing function's output for whatever
    model the response reports — Sonnet and the (intentionally unused but
    priced) Opus rung alike. No duplicated pricing tables."""
    resp = _narrator_text_response(model=model)
    client = _build_narrator_client(monkeypatch, _fake_sdk([resp]))

    await _drive_narrator(client, model=model, caller="narrator")

    spans = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == "llm.request" and s.attributes.get("llm.model") == model
    ]
    assert spans, f"no llm.request span for model {model}"
    assert spans[-1].attributes.get("llm.cost_usd") == pytest.approx(
        _expected_cost(resp.usage, model), abs=1e-9
    )


async def test_aside_missing_usage_raises_loud(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A response without ``usage`` is a real condition to surface, not a
    zero-cost log line (No Silent Fallbacks). The adapter raises; no usage
    line claiming cost_usd=0 may be emitted."""
    caplog.set_level(logging.INFO)
    resp = _aside_text_response()
    no_usage = SimpleNamespace(
        content=resp.content, usage=None, stop_reason="end_turn", model=_HAIKU
    )
    adapter = _build_aside_adapter(monkeypatch, _fake_sdk([no_usage]))

    with pytest.raises(LlmClientError):
        await adapter.complete(system="S", user="U")

    assert not [m for m in _usage_lines(caplog) if "caller=aside" in m], (
        "a usage-less response must not produce a usage log line"
    )


async def test_intent_router_missing_usage_raises_loud(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    resp = _router_tool_response()
    no_usage = SimpleNamespace(
        content=resp.content, usage=None, stop_reason="tool_use", model=_HAIKU
    )
    adapter = _build_router_adapter(monkeypatch, _fake_sdk([no_usage]))

    with pytest.raises(LlmClientError):
        await _drive_router(adapter)

    assert not [m for m in _usage_lines(caplog) if "caller=intent_router" in m], (
        "a usage-less response must not produce a usage log line"
    )


# ===========================================================================
# Integration — the production composition (handler-shape) emits BOTH
# ===========================================================================


async def test_aside_production_path_emits_log_line_and_span(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    otel_capture: Any,
) -> None:
    """End-to-end through the production composition the player_action handler
    builds — ``AsideResolver(llm=build_aside_llm())`` — with the SDK faked at
    the factory seam. One non-narrator call must produce BOTH the uniform
    usage log line (caplog) and the ``llm.request`` span (exporter). This is
    the story's mandated integration wiring test."""
    caplog.set_level(logging.INFO)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    from sidequest.agents import llm_factory
    from sidequest.agents.aside_resolver import AsideReadView, AsideResolver

    resp = _aside_text_response()
    sentinel = _fake_sdk([resp])
    monkeypatch.setattr(llm_factory, "build_async_anthropic", lambda: sentinel)

    resolver = AsideResolver(llm=llm_factory.build_aside_llm(session_id=None))
    res = await resolver.resolve(
        question="how big is my pack?",
        read_view=AsideReadView(
            character_summary="Rux: a wiry tunnel scout",
            region_summary="The Undermarket",
            inventory=["rope", "pack"],
            rulebook_summary="Genre caverns_and_claudes.",
            recent_narration="You descend into the dark.",
        ),
    )

    assert res.outcome == "answered", (
        f"production aside path failed: {res!r} — the fake SDK was not the "
        "one the resolver called (factory seam bypassed?)"
    )
    line = _usage_line_for_caller(caplog, "aside")
    assert f"model={_HAIKU}" in line
    assert _parsed_cost(line) == pytest.approx(_expected_cost(resp.usage, _HAIKU), abs=1e-9)
    spans = _llm_request_spans(otel_capture, caller="aside")
    assert spans, (
        "the production aside path must emit an llm.request span with "
        "llm.caller='aside' — span-based attribution is how 91-2 hunts the "
        "8x/turn volume"
    )
