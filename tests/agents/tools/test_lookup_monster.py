"""Tests for the lookup_monster tool — Phase C Task 14.

READ tool. ADR-059 (Monster Manual — server-side pre-generation). Returns the
lore-safe surface (description, behavior cues) for a Manual NPC by name.

Phase B amendment: :class:`~sidequest.agents.tool_registry.ToolContext`
gained an optional ``monster_manual: MonsterManual | None`` slot,
paralleling the lore_store amendment from Task 13. The MonsterManual is
per-genre/world and lives on
:class:`~sidequest.server.session_handler.SessionHandler` (loaded via
:meth:`MonsterManual.load`), not on the SqliteStore save layer — so it
cannot be reached via ``ctx.store``. Production wiring is Phase E. v1 of
this tool tolerates ``monster_manual is None`` by returning ``found=False``
with an OTEL marker (``tool.monster.monster_manual_wired = False``) so the
GM panel can detect un-wired calls.

v1 hard-gates ``include_stat_block``: the arg is accepted forward-compat
so the SDK schema doesn't churn later, but the stat-block data is NEVER
returned to the narrator until a per-PC recognize-check system lands
(post-Phase D). The narrator gets back ``stat_block_included=False`` and a
``stat_block_gate_reason`` string so it knows it asked and didn't get it.

No perception rule registered — the v1 hard-gate inside the handler is
strictly stricter than any per-PC perception rule could be.
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
from sidequest.agents.tools import lookup_monster as _lookup_monster_module  # noqa: F401
from sidequest.game.monster_manual import EntryState, MonsterManual


def _make_ctx(
    *,
    monster_manual: MonsterManual | None = None,
    perspective_pc: str | None = "Alice",
) -> ToolContext:
    return ToolContext(
        world_id="w",
        session_id="s",
        perspective_pc=perspective_pc,
        turn_number=1,
        store=MagicMock(),
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
        monster_manual=monster_manual,
    )


async def _call(arguments: dict, ctx: ToolContext) -> ToolResult:
    """Invoke the registered handler directly (bypass dispatch + perception)."""
    registered = default_registry._tools["lookup_monster"]
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, ctx)


def _payload(r: ToolResult) -> dict[str, Any]:
    assert r.payload is not None
    return cast(dict[str, Any], r.payload)


def _seeded_manual() -> MonsterManual:
    mm = MonsterManual(genre="g", world="w")
    mm.add_npc(
        data={
            "name": "Salt Burrower",
            "role": "ambush predator",
            "culture": "Scrapborn",
            "hp": 18,
            "ocean_summary": "low O, high N",
        },
        location_tags=["desert"],
    )
    return mm


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_lookup_monster_is_registered() -> None:
    assert "lookup_monster" in default_registry.list_names()


# ---------------------------------------------------------------------------
# Un-wired MonsterManual (Phase E will wire production)
# ---------------------------------------------------------------------------


async def test_monster_manual_none_returns_unwired_marker() -> None:
    """ctx.monster_manual is None — v1 tolerance: found=False + un-wired flag."""
    ctx = _make_ctx(monster_manual=None)

    r = await _call({"name": "Salt Burrower"}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["found"] is False
    assert p["name"] == "Salt Burrower"
    assert p["monster_manual_wired"] is False


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


async def test_returns_lore_safe_surface_when_found() -> None:
    """Found entry → lore-safe payload, never the data dict."""
    mm = _seeded_manual()
    ctx = _make_ctx(monster_manual=mm)

    r = await _call({"name": "Salt Burrower"}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["name"] == "Salt Burrower"
    assert p["role"] == "ambush predator"
    assert p["culture"] == "Scrapborn"
    assert p["location_tags"] == ["desert"]
    assert p["state"] == EntryState.AVAILABLE.value
    assert p["activated_location"] is None
    assert p["monster_manual_wired"] is True
    assert p["stat_block_included"] is False
    # Critically: the raw data dict (hp, ocean_summary, etc.) is NOT exposed
    # in the lore-safe payload.
    assert "data" not in p
    assert "hp" not in p
    assert "ocean_summary" not in p


async def test_fuzzy_substring_lookup() -> None:
    """MonsterManual.find_npc_by_name is fuzzy/substring; tool inherits."""
    mm = _seeded_manual()
    ctx = _make_ctx(monster_manual=mm)

    # Substring of stored name
    r = await _call({"name": "Burrower"}, ctx)
    p = _payload(r)
    assert p["name"] == "Salt Burrower"
    assert p["monster_manual_wired"] is True


async def test_unknown_name_returns_not_found() -> None:
    mm = _seeded_manual()
    ctx = _make_ctx(monster_manual=mm)

    r = await _call({"name": "Plasma Wyrm"}, ctx)
    assert r.status is ToolResultStatus.NOT_FOUND
    assert r.message is not None
    assert "Plasma Wyrm" in r.message


# ---------------------------------------------------------------------------
# Hard-gate on include_stat_block
# ---------------------------------------------------------------------------


async def test_include_stat_block_true_still_lore_safe_v1_gate() -> None:
    """v1 hard-gate: even include_stat_block=True returns lore-safe payload.

    The narrator gets back stat_block_included=False plus a gate-reason
    string so the model knows it asked and didn't get the data.
    """
    mm = _seeded_manual()
    ctx = _make_ctx(monster_manual=mm)

    r = await _call(
        {"name": "Salt Burrower", "include_stat_block": True},
        ctx,
    )
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["stat_block_included"] is False
    assert "stat_block_gate_reason" in p
    assert "recognize-check" in p["stat_block_gate_reason"]
    # And no stat-block fields leaked through.
    assert "data" not in p
    assert "hp" not in p


async def test_include_stat_block_default_false_no_gate_reason_emitted() -> None:
    """When the narrator didn't ask, no gate-reason is emitted (only the flag)."""
    mm = _seeded_manual()
    ctx = _make_ctx(monster_manual=mm)

    r = await _call({"name": "Salt Burrower"}, ctx)
    p = _payload(r)
    assert p["stat_block_included"] is False
    assert "stat_block_gate_reason" not in p


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


async def test_empty_name_is_recoverable_validation_error() -> None:
    """min_length=1 on name — dispatcher returns recoverable error."""
    ctx = _make_ctx(monster_manual=_seeded_manual())
    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-empty",
            name="lookup_monster",
            arguments={"name": ""},
        ),
        ctx,
    )
    assert out.is_error is True
    assert "validation failed" in out.content.lower()


# ---------------------------------------------------------------------------
# Dispatch + OTEL
# ---------------------------------------------------------------------------


async def test_dispatch_payload_round_trip() -> None:
    mm = _seeded_manual()
    ctx = _make_ctx(monster_manual=mm)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-disp",
            name="lookup_monster",
            arguments={"name": "Salt Burrower"},
        ),
        ctx,
    )
    assert out.is_error is False
    payload = json.loads(out.content)
    assert payload["name"] == "Salt Burrower"
    assert payload["role"] == "ambush predator"
    assert payload["monster_manual_wired"] is True
    assert payload["stat_block_included"] is False


async def test_does_not_touch_ctx_store() -> None:
    """Wiring discipline: this tool reaches into ctx.monster_manual only."""
    mm = _seeded_manual()
    ctx = _make_ctx(monster_manual=mm)
    store_mock = cast(MagicMock, ctx.store)
    store_mock.load.reset_mock()
    store_mock.reset_mock()

    await _call({"name": "Salt Burrower"}, ctx)
    store_mock.load.assert_not_called()
    # And no methods at all should have been called on the store.
    assert store_mock.method_calls == []


async def test_otel_attrs_on_hit(otel_capture) -> None:
    mm = _seeded_manual()
    ctx = _make_ctx(monster_manual=mm)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-otel",
            name="lookup_monster",
            arguments={"name": "Salt Burrower"},
        ),
        ctx,
    )
    assert out.is_error is False
    spans = otel_capture.get_finished_spans()
    read_spans = [s for s in spans if s.name == "tool.read.lookup_monster"]
    assert read_spans, f"no tool.read.lookup_monster span; got: {[s.name for s in spans]}"
    attrs = dict(read_spans[-1].attributes or {})
    assert attrs.get("tool.name") == "lookup_monster"
    assert attrs.get("tool.category") == "read"
    assert attrs.get("tool.result_status") == "ok"
    assert attrs.get("tool.monster.name") == "Salt Burrower"
    assert attrs.get("tool.monster.stat_block_included") is False
    assert attrs.get("tool.monster.monster_manual_wired") is True


async def test_otel_attrs_when_monster_manual_unwired(otel_capture) -> None:
    """ctx.monster_manual=None — OTEL still records with un-wired marker."""
    ctx = _make_ctx(monster_manual=None)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-otel-unwired",
            name="lookup_monster",
            arguments={"name": "Salt Burrower"},
        ),
        ctx,
    )
    assert out.is_error is False
    spans = otel_capture.get_finished_spans()
    read_spans = [s for s in spans if s.name == "tool.read.lookup_monster"]
    assert read_spans
    attrs = dict(read_spans[-1].attributes or {})
    assert attrs.get("tool.monster.name") == "Salt Burrower"
    assert attrs.get("tool.monster.stat_block_included") is False
    assert attrs.get("tool.monster.monster_manual_wired") is False


async def test_otel_attrs_on_not_found(otel_capture) -> None:
    mm = _seeded_manual()
    ctx = _make_ctx(monster_manual=mm)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-otel-nf",
            name="lookup_monster",
            arguments={"name": "Plasma Wyrm"},
        ),
        ctx,
    )
    assert out.is_error is False  # NOT_FOUND is non-erroring per Phase B
    spans = otel_capture.get_finished_spans()
    read_spans = [s for s in spans if s.name == "tool.read.lookup_monster"]
    assert read_spans
    attrs = dict(read_spans[-1].attributes or {})
    assert attrs.get("tool.result_status") == "not_found"
    assert attrs.get("tool.monster.name") == "Plasma Wyrm"
    assert attrs.get("tool.monster.stat_block_included") is False
    assert attrs.get("tool.monster.monster_manual_wired") is True
