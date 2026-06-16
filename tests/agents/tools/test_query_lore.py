"""Tests for the query_lore tool — Phase C Task 13.

READ tool. ADR-048 (Lore RAG Store) — narrator-private RAG against the
in-memory :class:`~sidequest.game.lore_store.LoreStore` that lives on
:class:`~sidequest.server.session_handler.SessionHandler`.

Phase B amendment: :class:`~sidequest.agents.tool_registry.ToolContext`
gained an optional ``lore_store`` slot. Production wiring (constructing
the ctx with the session-handler's LoreStore) is Phase E. v1 of this
tool tolerates ``lore_store is None`` by returning an empty result with
an OTEL marker (``tool.lore.lore_store_wired = False``) so the GM panel
can detect un-wired calls.

v1 uses keyword substring search via
:meth:`LoreStore.query_by_keyword`; embedding-based similarity is
deferred to Phase D when an on-the-fly query embedding pipeline lands.

No perception rule registered — the plan's "hide classified/secret
unless the PC has the secret-tag" rule is deferred to Phase D when PC
tags arrive.
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
from sidequest.agents.tools import query_lore as _query_lore_module  # noqa: F401
from sidequest.game.lore_store import LoreFragment, LoreSource, LoreStore


def _make_ctx(
    *,
    lore_store: LoreStore | None = None,
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
        lore_store=lore_store,
    )


async def _call(arguments: dict, ctx: ToolContext) -> ToolResult:
    """Invoke the registered handler directly (bypass dispatch + perception)."""
    registered = default_registry._tools["query_lore"]
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, ctx)


def _payload(r: ToolResult) -> dict[str, Any]:
    assert r.payload is not None
    return cast(dict[str, Any], r.payload)


def _frag(id_: str, content: str, *, category: str = "history") -> LoreFragment:
    return LoreFragment.new(
        id=id_,
        category=category,
        content=content,
        source=LoreSource.GenrePack,
        turn_created=1,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_query_lore_is_registered() -> None:
    assert "query_lore" in default_registry.list_names()


# ---------------------------------------------------------------------------
# Un-wired LoreStore (Phase E will wire production)
# ---------------------------------------------------------------------------


async def test_lore_store_none_returns_empty_with_marker() -> None:
    """ctx.lore_store is None — v1 tolerance: empty fragments + un-wired flag."""
    ctx = _make_ctx(lore_store=None)

    r = await _call({"topic_or_query": "anything"}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["fragments"] == []
    assert p["k"] == 5
    assert p["lore_store_wired"] is False


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


async def test_returns_matching_fragments() -> None:
    """3 matching + 2 non-matching → only matching come back."""
    store = LoreStore()
    store.add(_frag("a", "The dragon of Mawdeep slumbers."))
    store.add(_frag("b", "An ancient dragon lairs beneath the keep."))
    store.add(_frag("c", "Goblins infest the lower tunnels."))
    store.add(_frag("d", "A dragon ate the village priest."))
    store.add(_frag("e", "The river floods every spring."))

    ctx = _make_ctx(lore_store=store)
    r = await _call({"topic_or_query": "dragon", "k": 10}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["lore_store_wired"] is True
    ids = sorted(f["id"] for f in p["fragments"])
    assert ids == ["a", "b", "d"]


async def test_k_caps_result_size() -> None:
    store = LoreStore()
    for i in range(5):
        store.add(_frag(f"f{i}", f"The dragon roars {i}."))

    ctx = _make_ctx(lore_store=store)
    r = await _call({"topic_or_query": "dragon", "k": 1}, ctx)
    p = _payload(r)
    assert len(p["fragments"]) == 1
    assert p["k"] == 1


async def test_case_insensitive_substring_match() -> None:
    """LoreStore.query_by_keyword is case-insensitive; this tool inherits that."""
    store = LoreStore()
    store.add(_frag("a", "The DRAGON of Mawdeep slumbers."))

    ctx = _make_ctx(lore_store=store)
    r = await _call({"topic_or_query": "dragon"}, ctx)
    p = _payload(r)
    assert len(p["fragments"]) == 1
    assert p["fragments"][0]["id"] == "a"


async def test_no_matches_returns_empty_fragments() -> None:
    store = LoreStore()
    store.add(_frag("a", "The river floods every spring."))

    ctx = _make_ctx(lore_store=store)
    r = await _call({"topic_or_query": "dragon"}, ctx)
    p = _payload(r)
    assert p["fragments"] == []
    assert p["lore_store_wired"] is True


async def test_fragment_payload_shape() -> None:
    """Each fragment dict has the expected fields."""
    store = LoreStore()
    store.add(
        LoreFragment.new(
            id="x",
            category="faction",
            content="The Black Hands run the docks.",
            source=LoreSource.CharacterCreation,
            turn_created=4,
            metadata={"scene_id": "harbor"},
        )
    )

    ctx = _make_ctx(lore_store=store)
    r = await _call({"topic_or_query": "Black Hands"}, ctx)
    p = _payload(r)
    assert len(p["fragments"]) == 1
    f = p["fragments"][0]
    assert f["id"] == "x"
    assert f["category"] == "faction"
    assert f["content"] == "The Black Hands run the docks."
    assert f["source"] == LoreSource.CharacterCreation
    assert f["turn_created"] == 4
    assert f["metadata"] == {"scene_id": "harbor"}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


async def test_empty_topic_is_recoverable_validation_error() -> None:
    """min_length=1 on topic_or_query — dispatcher returns recoverable error."""
    ctx = _make_ctx(lore_store=LoreStore())
    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-empty",
            name="query_lore",
            arguments={"topic_or_query": ""},
        ),
        ctx,
    )
    assert out.is_error is True
    assert "validation failed" in out.content.lower()


# ---------------------------------------------------------------------------
# Dispatch + OTEL
# ---------------------------------------------------------------------------


async def test_dispatch_payload_round_trip() -> None:
    store = LoreStore()
    store.add(_frag("a", "The dragon of Mawdeep slumbers."))

    ctx = _make_ctx(lore_store=store)
    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-disp",
            name="query_lore",
            arguments={"topic_or_query": "dragon"},
        ),
        ctx,
    )
    assert out.is_error is False
    payload = json.loads(out.content)
    assert payload["k"] == 5
    assert payload["lore_store_wired"] is True
    assert len(payload["fragments"]) == 1
    assert payload["fragments"][0]["content"] == "The dragon of Mawdeep slumbers."


async def test_otel_attrs_on_hit(otel_capture) -> None:
    store = LoreStore()
    store.add(_frag("a", "Dragon a."))
    store.add(_frag("b", "Dragon b."))

    ctx = _make_ctx(lore_store=store)
    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-otel",
            name="query_lore",
            arguments={"topic_or_query": "Dragon", "k": 7},
        ),
        ctx,
    )
    assert out.is_error is False
    spans = otel_capture.get_finished_spans()
    read_spans = [s for s in spans if s.name == "tool.read.query_lore"]
    assert read_spans, f"no tool.read.query_lore span; got: {[s.name for s in spans]}"
    attrs = dict(read_spans[-1].attributes or {})
    assert attrs.get("tool.name") == "query_lore"
    assert attrs.get("tool.category") == "read"
    assert attrs.get("tool.result_status") == "ok"
    assert attrs.get("tool.lore.k") == 7
    assert attrs.get("tool.lore.hit_count") == 2
    assert attrs.get("tool.lore.lore_store_wired") is True


async def test_otel_attrs_when_lore_store_unwired(otel_capture) -> None:
    """ctx.lore_store=None — OTEL still records the call with un-wired marker."""
    ctx = _make_ctx(lore_store=None)
    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-otel-unwired",
            name="query_lore",
            arguments={"topic_or_query": "dragon", "k": 3},
        ),
        ctx,
    )
    assert out.is_error is False
    spans = otel_capture.get_finished_spans()
    read_spans = [s for s in spans if s.name == "tool.read.query_lore"]
    assert read_spans
    attrs = dict(read_spans[-1].attributes or {})
    assert attrs.get("tool.lore.k") == 3
    assert attrs.get("tool.lore.hit_count") == 0
    assert attrs.get("tool.lore.lore_store_wired") is False
