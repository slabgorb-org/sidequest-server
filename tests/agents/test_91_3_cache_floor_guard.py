"""Story 91-3 — Intent Router cache repair + fail-loud floor guard (epic 91).

Cost forensics (pingpong [COST-1], 2026-06-05) found Haiku 4.5 100% uncached
org-wide — ``cache_creation = 0`` and ``cache_read = 0`` every day — despite
``_IntentRouterLlm.emit_tool`` carrying the 1h ``cache_control`` marker and
the mandatory ``extended-cache-ttl`` beta header. Below Haiku 4.5's
4,096-token cacheable floor the marker is accepted by the API and *silently
never caches* — a No Silent Fallbacks violation embedded in API behavior.

Today the floor is enforced only by test tripwires in
``test_haiku_cache_control.py``. Production code (``build_intent_router_llm``)
will happily construct an adapter whose marker silently no-ops. This story
moves the floor guard into the PRODUCTION build path:

* AC1 — ``build_intent_router_llm()`` validates the combined tools+system
  prefix (production ``_SYSTEM_PROMPT`` + DispatchPackage tool schema) at
  build time against the 4,096-token floor.
* AC2 — a sub-floor prefix raises ``IntentRouterCacheFloorError`` (an
  ``LlmClientError``) with a clear message naming the floor — never ship a
  marker that silently doesn't cache.
* AC3/AC4 — live-gated two-turn proof: ``cache_creation_input_tokens`` >= the
  floor on turn 1 (cold write of the whole prefix), ``cache_read_input_tokens``
  >= the floor on turn 2 (warm read). Gated on
  ``SIDEQUEST_VERIFY_HAIKU_CACHE_LIVE`` like the composer's Gymnopedie smoke
  test — never runs in default CI; fails loud (not skip) if the flag is set
  but the key is missing.
* AC5 — the guard's decision is OTEL-visible: an ``intent_router.cache_floor``
  span fires on every build, pass or fail, so the GM panel can verify the
  guard engaged (OTEL Observability Principle). Per-call cache metrics on
  ``llm.request`` spans are already pinned by 91-1
  (``test_91_1_sdk_choke_point_instrumentation.py``) and
  ``test_haiku_cache_control.py``.

Contract notes for the GREEN phase:

* The guard must read the production prefix LATE-BOUND through the
  ``sidequest.agents.intent_router`` module at call time (module-dict lookup,
  same doctrine as ``build_async_anthropic``'s monkeypatch contract) — these
  tests monkeypatch ``_SYSTEM_PROMPT`` / ``_dispatch_tool_schema`` on that
  module to drive the sub-floor path.
* The token estimate must be OFFLINE (no network): the adapter is built once
  per turn (``build_intent_router_for_session`` docstring), so a
  ``count_tokens`` API call per build is not acceptable. Calibrate the
  chars→tokens ratio so the real production prefix (~15.3k chars ≈ ~4.7k tok
  measured) PASSES while a genuinely sub-floor prefix raises — both ends are
  pinned below. The authoritative exact count remains the opt-in
  ``test_intent_router_prefix_token_floor_live`` (count_tokens).
* The guard applies ONLY to the cached adapter. The aside adapter is
  intentionally sub-floor and uncached (bare-string system) — it must build
  unguarded.

Assertions are behavioral — on exceptions raised, adapters built, and spans
emitted — not greps of source text (CLAUDE.md "No Source-Text Wiring Tests").
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator

import pytest

_HAIKU_FLOOR_TOKENS = 4096
_FLOOR_PATTERN = re.compile(r"4,?096")

# A schema whose JSON serialization alone is far above the floor under any
# sane chars->tokens calibration (40k chars; even a conservative 4 chars/tok
# reading is ~10k tokens).
_HUGE_SCHEMA = {"type": "object", "description": "x" * 40_000}
# A schema whose serialization is trivially sub-floor.
_TINY_SCHEMA = {"type": "object"}


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


def _force_subfloor_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink BOTH halves of the production prefix below the floor."""
    import sidequest.agents.intent_router as ir

    monkeypatch.setattr(ir, "_SYSTEM_PROMPT", "tiny system prompt")
    monkeypatch.setattr(ir, "_dispatch_tool_schema", lambda: dict(_TINY_SCHEMA))


# ---------------------------------------------------------------------------
# AC1 + AC2 — build-time floor guard, fail loud
# ---------------------------------------------------------------------------


def test_build_raises_cache_floor_error_below_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1/AC2: a sub-floor combined prefix must REFUSE to build.

    Today this passes silently — ``build_intent_router_llm()`` constructs an
    adapter whose cache marker the API will accept and silently never honor.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    _force_subfloor_prefix(monkeypatch)

    from sidequest.agents.llm_factory import (
        IntentRouterCacheFloorError,
        build_intent_router_llm,
    )

    with pytest.raises(IntentRouterCacheFloorError):
        build_intent_router_llm(session_id=None)


def test_cache_floor_error_is_llm_client_error() -> None:
    """The guard's exception folds into the uniform LlmClientError hierarchy
    so existing fail-loud handling at the config boundary catches it."""
    from sidequest.agents.claude_client import LlmClientError
    from sidequest.agents.llm_factory import IntentRouterCacheFloorError

    assert issubclass(IntentRouterCacheFloorError, LlmClientError)


def test_cache_floor_error_message_names_floor_and_trap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2: the error must be diagnosable from its message alone — it names
    the 4,096-token floor and the silent-no-cache consequence, so the
    operator staring at a dead session knows exactly what to fix."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    _force_subfloor_prefix(monkeypatch)

    from sidequest.agents.llm_factory import (
        IntentRouterCacheFloorError,
        build_intent_router_llm,
    )

    with pytest.raises(IntentRouterCacheFloorError) as exc_info:
        build_intent_router_llm(session_id=None)

    message = str(exc_info.value)
    assert _FLOOR_PATTERN.search(message), (
        f"floor-guard error must name the 4,096-token floor; got: {message!r}"
    )
    assert "cache" in message.lower(), (
        f"floor-guard error must name the silent-no-cache trap; got: {message!r}"
    )


def test_build_succeeds_with_production_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard must NOT false-positive on the real production prefix.

    The measured combined prefix is ~15.3k chars ≈ ~4.7k tokens — above the
    floor. A guard calibrated too conservatively (e.g. chars/4) would refuse
    every production build and kill every turn; this test pins the passing
    end so calibration is forced to match the measured ratio.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    from sidequest.agents.llm_factory import build_intent_router_llm

    adapter = build_intent_router_llm(session_id=None)
    assert hasattr(adapter, "emit_tool"), (
        "build must return the IntentRouterLLM-shaped adapter on a passing prefix"
    )


def test_guard_measures_combined_prefix_not_system_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ORIGINAL defect class: the system prompt ALONE (~2,760 tok) is
    below the floor — the whole margin comes from bundling the tool schema
    (canonical cache order tools → system → messages, one marker on the
    system block caches both). A guard that measures the system block in
    isolation would wrongly refuse a healthy prefix. Sub-floor system + huge
    schema must BUILD."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    import sidequest.agents.intent_router as ir

    monkeypatch.setattr(ir, "_SYSTEM_PROMPT", "tiny system prompt")
    monkeypatch.setattr(ir, "_dispatch_tool_schema", lambda: dict(_HUGE_SCHEMA))

    from sidequest.agents.llm_factory import build_intent_router_llm

    adapter = build_intent_router_llm(session_id=None)  # must not raise
    assert hasattr(adapter, "emit_tool")


def test_aside_build_is_not_floor_guarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The aside adapter is intentionally sub-floor and UNCACHED (bare-string
    system, no marker — pinned in test_haiku_cache_control.py). The floor
    guard protects cache markers, not prompts; it must not overreach into the
    uncached adapter."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    _force_subfloor_prefix(monkeypatch)

    from sidequest.agents.llm_factory import build_aside_llm

    adapter = build_aside_llm(session_id=None)  # must not raise despite sub-floor env
    assert hasattr(adapter, "complete")


# ---------------------------------------------------------------------------
# Wiring — the PRODUCTION construction path goes through the guard
# ---------------------------------------------------------------------------


def test_production_session_build_path_is_floor_guarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wiring test (CLAUDE.md "Every Test Suite Needs a Wiring Test"): the
    per-turn production construction site —
    ``intent_router_pass.build_intent_router_for_session`` — must reach the
    guard. A guard that exists in llm_factory but is bypassed by the
    production path is exactly the half-wired failure mode this project
    forbids."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    _force_subfloor_prefix(monkeypatch)

    from sidequest.agents.llm_factory import IntentRouterCacheFloorError
    from sidequest.server.intent_router_pass import build_intent_router_for_session

    with pytest.raises(IntentRouterCacheFloorError):
        build_intent_router_for_session(session_id=None)


# ---------------------------------------------------------------------------
# AC5 — the guard decision is OTEL-visible (GM panel lie detector)
# ---------------------------------------------------------------------------


def test_passing_build_emits_cache_floor_span(
    monkeypatch: pytest.MonkeyPatch,
    otel_capture,
) -> None:
    """A passing build emits ``intent_router.cache_floor`` carrying the
    measurement, so the GM panel can verify the guard engaged rather than
    trusting that it exists (OTEL Observability Principle)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    from sidequest.agents.llm_factory import build_intent_router_llm

    build_intent_router_llm(session_id=None)

    spans = [s for s in otel_capture.get_finished_spans() if s.name == "intent_router.cache_floor"]
    assert spans, "build must emit an intent_router.cache_floor span"
    attrs = spans[-1].attributes
    assert attrs.get("passed") is True
    assert attrs.get("floor_tokens") == _HAIKU_FLOOR_TOKENS
    assert int(attrs.get("estimated_tokens", 0)) >= _HAIKU_FLOOR_TOKENS, (
        "the recorded estimate for the production prefix must clear the floor"
    )
    assert int(attrs.get("prefix_chars", 0)) > 0


def test_failing_build_emits_cache_floor_span_before_raising(
    monkeypatch: pytest.MonkeyPatch,
    otel_capture,
) -> None:
    """The refusal itself must be GM-panel visible: the fail path emits the
    span (passed=False, sub-floor estimate recorded) BEFORE raising, so a
    session that dies at build leaves a telemetry trail naming why."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    _force_subfloor_prefix(monkeypatch)

    from sidequest.agents.llm_factory import (
        IntentRouterCacheFloorError,
        build_intent_router_llm,
    )

    with pytest.raises(IntentRouterCacheFloorError):
        build_intent_router_llm(session_id=None)

    spans = [s for s in otel_capture.get_finished_spans() if s.name == "intent_router.cache_floor"]
    assert spans, "the refused build must still emit intent_router.cache_floor"
    attrs = spans[-1].attributes
    assert attrs.get("passed") is False
    assert attrs.get("floor_tokens") == _HAIKU_FLOOR_TOKENS
    assert 0 < int(attrs.get("estimated_tokens", 0)) < _HAIKU_FLOOR_TOKENS, (
        "the sub-floor estimate must be recorded so the operator sees the gap"
    )


# ---------------------------------------------------------------------------
# AC3 + AC4 — live two-turn cache proof (opt-in, never in default CI)
# ---------------------------------------------------------------------------


@pytest.mark.timeout(180)
async def test_intent_router_live_cache_write_then_read(otel_capture) -> None:
    """THE repair proof: against the real API, the production adapter's cache
    actually engages — ``cache_creation_input_tokens`` >= the floor on turn 1
    (the whole tools+system prefix written cold) and
    ``cache_read_input_tokens`` >= the floor on turn 2 (warm read). This is
    the assertion the Admin API says has NEVER been true in production
    (epic 91 incident: 0/0 org-wide, every day).

    Gated like the composer's Gymnopedie smoke test: opt-in via
    ``SIDEQUEST_VERIFY_HAIKU_CACHE_LIVE`` so the default suite stays
    network-free; fails loud — not skips — if the flag is set but the key is
    missing.

    The production system prompt is salted with a per-run nonce so turn 1 is
    guaranteed a COLD write even when a prior run within the 1h TTL left the
    unsalted prefix warm (a warm turn 1 would report creation=0/read>0 and
    flake the assertion). The salt adds ~10 tokens — the floor margin is
    unaffected, and the marker/header/floor mechanics under test are
    identical to production.

    Usage is read from the ``llm.request`` spans the adapter emits — the same
    field the GM panel watches — so this also proves AC5's cache metrics
    end-to-end through production telemetry, not through a test-only seam.
    """
    import asyncio
    import os

    if not os.environ.get("SIDEQUEST_VERIFY_HAIKU_CACHE_LIVE"):
        pytest.skip("set SIDEQUEST_VERIFY_HAIKU_CACHE_LIVE=1 (+ ANTHROPIC_API_KEY) to run")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.fail("SIDEQUEST_VERIFY_HAIKU_CACHE_LIVE set but ANTHROPIC_API_KEY missing")

    from sidequest.agents.intent_router import (
        _SYSTEM_PROMPT,
        _TOOL_DESCRIPTION,
        _TOOL_NAME,
        _dispatch_tool_schema,
    )
    from sidequest.agents.llm_factory import build_intent_router_llm

    adapter = build_intent_router_llm(session_id=None)
    salted_system = _SYSTEM_PROMPT + f"\n<!-- cache-proof nonce: {uuid.uuid4()} -->"
    tool_schema = _dispatch_tool_schema()

    async def _one_turn(action: str) -> None:
        await adapter.emit_tool(
            system=salted_system,
            user=(
                '<game_state>\n{"scene": "a quiet camp", "present_npcs": []}\n'
                "</game_state>\n"
                f"<raw_action>\n{action}\n</raw_action>\n"
                "Call emit_dispatch_package once for this single action."
            ),
            tool_name=_TOOL_NAME,
            tool_description=_TOOL_DESCRIPTION,
            tool_schema=tool_schema,
        )

    await _one_turn("I look around the camp.")
    # Brief settle between write and read — cheap insurance against cache
    # registration lag; the 1h TTL makes timing otherwise irrelevant.
    await asyncio.sleep(2)
    await _one_turn("I add a log to the fire.")

    spans = [s for s in otel_capture.get_finished_spans() if s.name == "llm.request"]
    assert len(spans) >= 2, "two live calls must emit two llm.request spans"
    turn1, turn2 = spans[-2].attributes, spans[-1].attributes

    creation_t1 = int(turn1.get("llm.cached_input_write_tokens", 0))
    read_t2 = int(turn2.get("llm.cached_input_read_tokens", 0))

    assert creation_t1 >= _HAIKU_FLOOR_TOKENS, (
        f"AC3: turn 1 must COLD-WRITE the whole tools+system prefix "
        f"(cache_creation_input_tokens={creation_t1}, expected >= "
        f"{_HAIKU_FLOOR_TOKENS}). creation=0 means the marker silently "
        "no-opped — the exact dead-cache incident this story repairs."
    )
    assert read_t2 >= _HAIKU_FLOOR_TOKENS, (
        f"AC4: turn 2 must WARM-READ the prefix "
        f"(cache_read_input_tokens={read_t2}, expected >= "
        f"{_HAIKU_FLOOR_TOKENS}). A write without a read still re-bills "
        "every turn."
    )
