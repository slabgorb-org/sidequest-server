"""Story 71-40 (RED) — router decompose env-vs-code latency attribution (AC2).

The ``intent_router.decompose`` span already carries ``latency_ms`` (the whole
attempt loop, measured in ``IntentRouter.decompose``). The 2026-05-27 playtest
measured that total at 4-12s — 3-10x over the ADR-113 < 1.2s budget. This story
splits that total so the GM panel can attribute the cost:

* the **raw SDK round-trip** — time the ``emit_tool`` call independently (the
  environmental cost: Haiku SDK call, network, cold connection), recorded as
  ``sdk_latency_ms``;
* the **serialized state-summary size** — ``state_summary_bytes`` (the prime
  *code* suspect: an oversized state-summary prompt inflates input tokens every
  call), the sibling of the existing ``action_length`` attribute.

This is the REQUIRED router-side wiring test (context-story-71-40.md): it drives
the *real* ``IntentRouter.decompose`` with a mocked ``IntentRouterLLM`` and
asserts the new attribution attributes land on the real emitted
``intent_router.decompose`` span (captured via the live OTEL provider) — not a
unit test of a standalone timing helper.

RED: ``decompose`` does not yet set ``sdk_latency_ms`` or ``state_summary_bytes``
on its span (see ``intent_router.py`` lines ~377-394). Dev (GREEN) times
``emit_tool`` separately from the surrounding bookkeeping and records both.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

_DECOMPOSE_SPAN = "intent_router.decompose"


def _quiet_turn_response() -> dict:
    """Schema-valid empty DispatchPackage (no dispatches) — the router parses
    it and reaches the success-span path."""
    return {
        "turn_id": "turn-lat",
        "per_player": [],
        "cross_player": [],
        "confidence_global": 1.0,
    }


def _mock_router_llm(response: dict) -> AsyncMock:
    mock = AsyncMock()
    mock.emit_tool = AsyncMock(return_value=response)
    return mock


def _decompose_attrs(otel_capture) -> dict:
    spans = [s for s in otel_capture.get_finished_spans() if s.name == _DECOMPOSE_SPAN]
    assert len(spans) == 1, (
        f"expected exactly one {_DECOMPOSE_SPAN} span on the success path; got {len(spans)}"
    )
    return dict(spans[0].attributes or {})


@pytest.mark.asyncio
async def test_decompose_span_carries_sdk_latency_and_summary_size(otel_capture) -> None:
    """AC2: the success span exposes BOTH attribution attributes —
    ``sdk_latency_ms`` (env: raw emit_tool round-trip) and
    ``state_summary_bytes`` (code: serialized prompt size) — alongside the
    existing total ``latency_ms``."""
    from sidequest.agents.intent_router import IntentRouter

    router = IntentRouter(llm=_mock_router_llm(_quiet_turn_response()))
    await router.decompose(action="I look around.", state_summary={"scene": "a quiet hall"})

    attrs = _decompose_attrs(otel_capture)
    for key in ("latency_ms", "sdk_latency_ms", "state_summary_bytes"):
        assert key in attrs, (
            f"intent_router.decompose missing attribution attr {key!r}; "
            f"AC2 requires env-vs-code split. attrs={attrs}"
        )
    assert isinstance(attrs["sdk_latency_ms"], int)
    assert isinstance(attrs["state_summary_bytes"], int)


@pytest.mark.asyncio
async def test_sdk_latency_never_exceeds_total_latency(otel_capture) -> None:
    """AC2 invariant: the raw SDK round-trip is a COMPONENT of the total, so
    ``sdk_latency_ms <= latency_ms`` always. A split where the part exceeds the
    whole would be measuring the wrong thing."""
    from sidequest.agents.intent_router import IntentRouter

    router = IntentRouter(llm=_mock_router_llm(_quiet_turn_response()))
    await router.decompose(action="wait", state_summary={"scene": "x"})

    attrs = _decompose_attrs(otel_capture)
    assert attrs["sdk_latency_ms"] <= attrs["latency_ms"], (
        "raw SDK round-trip cannot exceed the total decompose latency; "
        f"sdk_latency_ms={attrs['sdk_latency_ms']} latency_ms={attrs['latency_ms']}"
    )
    assert attrs["sdk_latency_ms"] >= 0


@pytest.mark.asyncio
async def test_state_summary_bytes_equals_string_length(otel_capture) -> None:
    """AC2: when ``state_summary`` is already a string, ``state_summary_bytes``
    equals its UTF-8 byte length — the size the router actually puts in the
    prompt. Uses an ASCII summary so ``len(str) == len(bytes)``, pinning the
    'serialized size' semantics without coupling to the dict→JSON path."""
    from sidequest.agents.intent_router import IntentRouter

    summary = "scene: a long ASCII state summary describing the room and its exits"
    router = IntentRouter(llm=_mock_router_llm(_quiet_turn_response()))
    await router.decompose(action="look", state_summary=summary)

    attrs = _decompose_attrs(otel_capture)
    assert attrs["state_summary_bytes"] == len(summary.encode("utf-8")), (
        "state_summary_bytes must record the serialized prompt size; for a "
        f"string summary that is its byte length ({len(summary)}). "
        f"Got {attrs['state_summary_bytes']}"
    )


@pytest.mark.asyncio
async def test_state_summary_bytes_grows_with_summary(otel_capture) -> None:
    """AC2 diagnostic property: a larger state summary records a strictly larger
    ``state_summary_bytes`` — the correlation the diagnosis needs to test the
    'oversized state-summary prompt' hypothesis. A constant/placeholder value
    would fail this."""
    from sidequest.agents.intent_router import IntentRouter

    small = "scene: hall"
    large = "scene: hall " + ("detail " * 200)

    router_small = IntentRouter(llm=_mock_router_llm(_quiet_turn_response()))
    await router_small.decompose(action="look", state_summary=small)
    small_bytes = _decompose_attrs(otel_capture)["state_summary_bytes"]

    otel_capture.clear()

    router_large = IntentRouter(llm=_mock_router_llm(_quiet_turn_response()))
    await router_large.decompose(action="look", state_summary=large)
    large_bytes = _decompose_attrs(otel_capture)["state_summary_bytes"]

    assert large_bytes > small_bytes, (
        "a bigger state summary must record more bytes — state_summary_bytes "
        f"must track size, not be a constant (small={small_bytes}, large={large_bytes})"
    )
