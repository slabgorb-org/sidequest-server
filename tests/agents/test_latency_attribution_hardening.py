"""Story 82-9 (RED) — strengthen two 71-40 latency-attribution test gaps (AC5).

71-40 left two attribution gaps the Reviewer flagged:

* **dict→JSON ``state_summary_bytes``** — the existing test pins only the
  *string* summary path and explicitly avoids "coupling to the dict→JSON path".
  But production passes a dict; the prime "oversized prompt" hypothesis turns on
  the serialized-JSON size, which is untested.
* **vacuous ``sdk_latency_ms`` invariant** — ``test_sdk_latency_never_exceeds_total``
  asserts only ``sdk <= total`` with an instant mock, where ``sdk_latency_ms``
  can sit at 0 and pass without measuring anything. It never proves the value
  TRACKS the real ``emit_tool`` round-trip.

These tests close both: the dict path equals the serialized-JSON byte length the
router actually sends, and an ``emit_tool`` that genuinely takes time records a
positive ``sdk_latency_ms`` bounded above by the total.

RED-safe note: these assert on the EXISTING ``intent_router.decompose``
instrumentation (already merged) but at inputs 71-40 left uncovered. They pass
once the dict-serialization + real timing behave correctly; they fail if a
regression flattens either to a constant.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

_DECOMPOSE_SPAN = "intent_router.decompose"


def _quiet_turn_response() -> dict:
    return {
        "turn_id": "turn-hard",
        "per_player": [],
        "cross_player": [],
        "confidence_global": 1.0,
    }


class _DelayingRouterLlm:
    """A router LLM whose ``emit_tool`` sleeps a fixed duration, so the measured
    ``sdk_latency_ms`` reflects a real (non-zero) round-trip — the mock equivalent
    of the network/env cost 71-40 set out to attribute."""

    def __init__(self, response: dict, delay_s: float) -> None:
        self._response = response
        self._delay_s = delay_s

    async def emit_tool(self, **_kwargs: Any) -> dict:
        await asyncio.sleep(self._delay_s)
        return self._response


class _InstantRouterLlm:
    def __init__(self, response: dict) -> None:
        self._response = response

    async def emit_tool(self, **_kwargs: Any) -> dict:
        return self._response


def _decompose_attrs(otel_capture) -> dict:
    spans = [s for s in otel_capture.get_finished_spans() if s.name == _DECOMPOSE_SPAN]
    assert len(spans) == 1, f"expected exactly one {_DECOMPOSE_SPAN} span; got {len(spans)}"
    return dict(spans[0].attributes or {})


# --- AC5 gap 1: dict state_summary records the serialized-JSON byte length ----


@pytest.mark.asyncio
async def test_state_summary_bytes_for_dict_equals_serialized_json_length(otel_capture) -> None:
    """When ``state_summary`` is a dict (the production shape), ``state_summary_bytes``
    must equal the UTF-8 byte length of the EXACT serialization the router puts in
    the prompt — proved by reusing the router's own ``_serialize_state_summary``,
    not by re-deriving the JSON here. This pins the dict→JSON path the 71-40
    string test deliberately skipped."""
    from sidequest.agents.intent_router import IntentRouter, _serialize_state_summary

    summary = {
        "scene": "a long ASCII state summary describing the room and its exits",
        "exits": ["north", "south", "a rusted hatch"],
        "npcs": ["the quartermaster", "a wary deckhand"],
        "flags": {"alarm": False, "tide": "rising"},
    }
    router = IntentRouter(llm=_InstantRouterLlm(_quiet_turn_response()))
    await router.decompose(action="look", state_summary=summary)

    attrs = _decompose_attrs(otel_capture)
    expected = len(_serialize_state_summary(summary).encode("utf-8"))
    assert attrs["state_summary_bytes"] == expected, (
        "for a dict summary, state_summary_bytes must equal the serialized-JSON "
        f"byte length the router actually sends ({expected}); got "
        f"{attrs['state_summary_bytes']}"
    )
    # Guard against a degenerate dict serializing to a constant/placeholder.
    assert attrs["state_summary_bytes"] > len("{}"), (
        "a non-empty dict must serialize to more than empty braces"
    )


# --- AC5 gap 2: sdk_latency_ms tracks a real emit_tool round-trip -------------


@pytest.mark.asyncio
async def test_sdk_latency_tracks_real_emit_tool_duration(otel_capture) -> None:
    """De-vacuify the 71-40 invariant: when ``emit_tool`` genuinely takes ~50ms,
    ``sdk_latency_ms`` must record a POSITIVE value (not the 0 a flat mock leaves)
    AND remain bounded above by the total ``latency_ms``. This proves the split
    measures the real SDK round-trip — the environmental cost the diagnosis
    attributes — instead of trivially satisfying ``sdk <= total`` at zero."""
    from sidequest.agents.intent_router import IntentRouter

    router = IntentRouter(llm=_DelayingRouterLlm(_quiet_turn_response(), delay_s=0.05))
    await router.decompose(action="wait", state_summary={"scene": "x"})

    attrs = _decompose_attrs(otel_capture)
    # asyncio.sleep(0.05) guarantees >= 50ms; floor-to-ms leaves a safe margin.
    assert attrs["sdk_latency_ms"] >= 20, (
        "sdk_latency_ms must track the real ~50ms emit_tool round-trip, not sit "
        f"at 0 like a flat mock; got {attrs['sdk_latency_ms']}"
    )
    assert attrs["sdk_latency_ms"] <= attrs["latency_ms"], (
        "the raw SDK round-trip is a COMPONENT of the total — it cannot exceed it; "
        f"sdk={attrs['sdk_latency_ms']} total={attrs['latency_ms']}"
    )
