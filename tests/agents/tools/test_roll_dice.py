"""Tests for the roll_dice tool — Phase C Task 2.

Narrator-private dice rolls for behind-the-scenes checks. Distinct from
ADR-074's player-facing 3D dice flow.
"""

from __future__ import annotations

import json
from typing import Any, cast
from unittest.mock import MagicMock

from sidequest.agents.narrator_perception_filter import NarratorPerceptionFilter
from sidequest.agents.tool_registry import (
    ToolContext,
    ToolResult,
    ToolResultStatus,
    default_registry,
)
from sidequest.agents.tooling_protocol import ToolUseBlock
from sidequest.agents.tools import roll_dice as _roll_dice_module  # noqa: F401


def _make_ctx() -> ToolContext:
    return ToolContext(
        world_id="w",
        session_id="s",
        perspective_pc="alex",
        turn_number=1,
        store=MagicMock(),
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
    )


async def _call(arguments: dict) -> ToolResult:
    """Invoke the registered handler directly (bypass dispatch span)."""
    registered = default_registry._tools["roll_dice"]
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, _make_ctx())


def _payload(r: ToolResult) -> dict[str, Any]:
    """Narrow payload to dict for pyright (handlers return dict on OK)."""
    assert r.payload is not None
    return cast(dict[str, Any], r.payload)


def test_roll_dice_is_registered() -> None:
    assert "roll_dice" in default_registry.list_names()


async def test_d20_returns_value_in_range() -> None:
    for _ in range(50):
        r = await _call({"notation": "d20"})
        assert r.status is ToolResultStatus.OK
        assert 1 <= _payload(r)["value"] <= 20


async def test_3d6_plus_2_returns_value_in_range() -> None:
    for _ in range(50):
        r = await _call({"notation": "3d6+2"})
        assert r.status is ToolResultStatus.OK
        # min: 3*1 + 2 = 5; max: 3*6 + 2 = 20
        p = _payload(r)
        assert 5 <= p["value"] <= 20
        assert len(p["rolls"]) == 3
        for roll in p["rolls"]:
            assert 1 <= roll <= 6


async def test_d20_minus_1_modifier() -> None:
    for _ in range(50):
        r = await _call({"notation": "d20-1"})
        assert r.status is ToolResultStatus.OK
        assert 0 <= _payload(r)["value"] <= 19


async def test_invalid_notation_returns_recoverable_error() -> None:
    r = await _call({"notation": "banana"})
    assert r.status is ToolResultStatus.ERROR_RECOVERABLE
    assert r.message is not None
    assert "invalid notation" in r.message


async def test_count_equals_form_rejected() -> None:
    """Only NdM[+K] form supported — 'count=10' style is invalid."""
    r = await _call({"notation": "count=10"})
    assert r.status is ToolResultStatus.ERROR_RECOVERABLE


async def test_seed_determinism() -> None:
    r1 = await _call({"notation": "3d6+2", "seed": 42})
    r2 = await _call({"notation": "3d6+2", "seed": 42})
    assert r1.status is ToolResultStatus.OK
    p1, p2 = _payload(r1), _payload(r2)
    assert p1["value"] == p2["value"]
    assert p1["rolls"] == p2["rolls"]


async def test_payload_shape() -> None:
    r = await _call({"notation": "d20", "seed": 7})
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert set(p.keys()) >= {"value", "rolls", "notation"}
    assert p["notation"] == "d20"
    assert isinstance(p["rolls"], list)
    assert len(p["rolls"]) == 1


async def test_otel_span_carries_notation_and_value(otel_capture) -> None:
    """Dispatching through Registry.dispatch lands per-tool attrs on the dispatch span.

    The dispatcher seeds standard attrs (tool.name, tool.category, ...) on the
    tool.gen.roll_dice span and injects that span into ctx.otel_span before
    invoking the handler — so the handler's tool.dice.* enrichments land on
    the same span the GM panel watches.
    """
    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-otel",
            name="roll_dice",
            arguments={"notation": "d20", "seed": 99},
        ),
        _make_ctx(),
    )
    assert out.is_error is False
    payload = json.loads(out.content)

    spans = otel_capture.get_finished_spans()
    dice_spans = [s for s in spans if s.name == "tool.gen.roll_dice"]
    assert dice_spans, f"no tool.gen.roll_dice span; got: {[s.name for s in spans]}"
    attrs = dict(dice_spans[-1].attributes or {})
    # Dispatcher-seeded standard attrs
    assert attrs.get("tool.name") == "roll_dice"
    assert attrs.get("tool.category") == "generate"
    assert attrs.get("tool.result_status") == "ok"
    # Handler-set per-tool attrs — must land on the dispatch span
    assert attrs.get("tool.dice.notation") == "d20"
    assert attrs.get("tool.dice.value") == payload["value"]
    assert attrs.get("tool.dice.seed") == 99


async def test_dispatch_through_registry_succeeds() -> None:
    """End-to-end: dispatch through default_registry and parse the JSON payload."""
    reg = default_registry
    out = await reg.dispatch(
        ToolUseBlock(id="t1", name="roll_dice", arguments={"notation": "d20"}),
        _make_ctx(),
    )
    assert out.is_error is False
    payload = json.loads(out.content)
    assert 1 <= payload["value"] <= 20


async def test_out_of_range_dice_spec_rejected() -> None:
    # 0 count or 0/1 sides should be rejected by the range guard
    r = await _call({"notation": "1d1"})
    assert r.status is ToolResultStatus.ERROR_RECOVERABLE


# ---------------------------------------------------------------------------
# Story 50-24 AC-3 — roll_dice span session/world attribution.
#
# Source: /Users/slabgorb/Projects/sq-playtest-pingpong.md, "OQ-2 ARCHITECT
# RESOLUTION" bullet under the "Narrator fabricates dice" headline.
#
# The Architect could not bind any tool.gen.roll_dice span to the
# coyote_star session because the span carries ONLY tool.dice.notation/
# value/seed — no session/world attribute (roll_dice.py:62-65). The
# absence had to be argued temporally instead of by session filter. AC-3
# closes that attribution gap so a future audit can bind a private roll
# to its game directly. ``_make_ctx`` already supplies
# world_id="w"/session_id="s"; the handler simply never writes them.
# ---------------------------------------------------------------------------


async def test_otel_span_carries_session_and_world_attribution(otel_capture) -> None:
    """The tool.gen.roll_dice span MUST carry the ctx session_id and
    world_id so a roll is bindable to its game in Jaeger.

    Target keys are the established ``tool.dice.*`` handler namespace
    (parity with tool.dice.notation/value/seed). If Dev prefers a
    different key this is a spec deviation to log — not a silent change.
    """
    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-sess",
            name="roll_dice",
            arguments={"notation": "d20", "seed": 7},
        ),
        _make_ctx(),  # world_id="w", session_id="s"
    )
    assert out.is_error is False

    spans = otel_capture.get_finished_spans()
    dice_spans = [s for s in spans if s.name == "tool.gen.roll_dice"]
    assert dice_spans, f"no tool.gen.roll_dice span; got: {[s.name for s in spans]}"
    attrs = dict(dice_spans[-1].attributes or {})

    assert attrs.get("tool.dice.session_id") == "s", (
        "tool.gen.roll_dice span has no session attribution "
        f"(tool.dice.session_id). Span attrs: {sorted(attrs)}. AC-3: the "
        "Architect had to argue Jaeger absence temporally because a "
        "roll_dice span cannot be bound to its session — close that gap."
    )
    assert attrs.get("tool.dice.world_id") == "w", (
        "tool.gen.roll_dice span has no world attribution "
        f"(tool.dice.world_id). Span attrs: {sorted(attrs)}. AC-3 names "
        "session/world; both must land for a per-game audit."
    )
