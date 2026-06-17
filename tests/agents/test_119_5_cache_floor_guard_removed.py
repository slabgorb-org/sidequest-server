"""Story 119-5 RED — AC1: the vestigial Intent-Router cache-floor guard is GONE.

Story 91-3 built a build-time guard that refused to construct the Intent-Router
adapter when the combined tools+system prefix fell below Haiku 4.5's
4,096-token cacheable floor — because below the floor a ``cache_control`` marker
is accepted by the API and silently never caches (the epic-91 dark-spend
incident). The guard was correct WHEN the adapter shipped a ``cache_control``
marker on the raw Anthropic messages API.

Story 119-3 ported every Haiku site onto ``claude-agent-sdk`` over subscription
auth. ``_IntentRouterLlm.emit_tool`` now drives ``build_agent_sdk_options(...)``
with ``output_format`` (Path A structured extraction) and ships **no**
``cache_control`` marker at all — there is none to put on the request, and the
SDK exposes no caller-visible cache-control surface to re-home the guard onto.
The guard therefore protects a trap the code can no longer spring: a build-time
pre-flight for a marker that is never sent.

**Decision (119-5 AC1): DELETE the guard, do not re-home it.** Re-homing would
require an SDK-exposed cache signal that does not exist — inventing it is
stubbing infrastructure (No Stubbing / Don't Reinvent). Deleting the dead guard
IS the explicit, fail-loud cleanup (No Silent Fallbacks: a guard that can never
fire is a silent lie about what is protected).

These tests pin the chosen behavior:

* The four vestigial symbols are removed from ``llm_factory``.
* ``build_intent_router_llm`` no longer raises on a sub-floor prefix — the
  trap cannot fire (behavioral proof, not a symbol grep).
* No ``intent_router.cache_floor`` span fires on build — the dead telemetry is
  removed too (don't leave a span advertising a guard that no longer exists).

Assertions are behavioral / reflective, never source-text greps
(CLAUDE.md "No Source-Text Wiring Tests"). The companion deletion is
``tests/agents/test_91_3_cache_floor_guard.py``, removed in this RED commit
because it pins the now-removed behavior.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

# The four symbols story 91-3 introduced and 119-5 removes.
_REMOVED_SYMBOLS = (
    "IntentRouterCacheFloorError",
    "HAIKU_CACHEABLE_PREFIX_FLOOR_TOKENS",
    "_INTENT_ROUTER_CACHE_TTL",
    "_estimate_intent_router_prefix_tokens",
)


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
    """Shrink BOTH halves of the production prefix far below the old floor.

    Same lever the 91-3 guard tests pulled to trip the refusal. After the guard
    is deleted this is inert — the build never measures the prefix — which is
    exactly what the behavioral test below asserts.
    """
    import sidequest.agents.intent_router as ir

    monkeypatch.setattr(ir, "_SYSTEM_PROMPT", "tiny system prompt")
    monkeypatch.setattr(ir, "_dispatch_tool_schema", lambda: {"type": "object"})


# ---------------------------------------------------------------------------
# AC1 — the vestigial symbols are removed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("symbol", _REMOVED_SYMBOLS)
def test_vestigial_floor_symbol_is_removed(symbol: str) -> None:
    """Each cache-floor symbol must be gone from ``llm_factory``.

    A guard that can never fire must be removed, not left dormant (No Silent
    Fallbacks). ``hasattr`` on the module is reflective, not a source grep.
    """
    import sidequest.agents.llm_factory as llm_factory

    assert not hasattr(llm_factory, symbol), (
        f"{symbol!r} is a vestigial cache-floor guard symbol — the SDK path "
        "ships no cache_control marker, so the floor trap can never spring. "
        "It must be deleted (119-5 AC1), not left in the module."
    )


# ---------------------------------------------------------------------------
# AC1 — behavioral: the build no longer refuses a sub-floor prefix
# ---------------------------------------------------------------------------


def test_build_does_not_raise_on_subfloor_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The core behavioral proof: a sub-floor prefix that the 91-3 guard would
    have REFUSED must now build a working adapter.

    Today ``build_intent_router_llm`` raises ``IntentRouterCacheFloorError`` on
    this prefix. After the guard is deleted the build never measures the
    prefix and returns the adapter — there is no marker to protect.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    _force_subfloor_prefix(monkeypatch)

    from sidequest.agents.llm_factory import build_intent_router_llm

    adapter = build_intent_router_llm(session_id=None)
    assert hasattr(adapter, "emit_tool"), (
        "with the floor guard removed, a sub-floor prefix must still build the "
        "IntentRouterLLM-shaped adapter — the SDK path has no cache marker to "
        "guard, so there is nothing left to refuse on"
    )


# ---------------------------------------------------------------------------
# AC1 — the dead OTEL span no longer fires
# ---------------------------------------------------------------------------


def test_build_emits_no_cache_floor_span(
    monkeypatch: pytest.MonkeyPatch,
    otel_capture,
) -> None:
    """Building the router must emit NO ``intent_router.cache_floor`` span.

    The span fired only from inside the deleted guard. Leaving it would
    advertise a guard decision that no longer happens — telemetry must not lie
    about which subsystems engaged (OTEL Observability Principle / No Silent
    Fallbacks).
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

    from sidequest.agents.llm_factory import build_intent_router_llm

    build_intent_router_llm(session_id=None)

    floor_spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "intent_router.cache_floor"
    ]
    assert not floor_spans, (
        "build must emit no intent_router.cache_floor span after the guard is "
        f"removed; got {[s.name for s in floor_spans]!r}"
    )
