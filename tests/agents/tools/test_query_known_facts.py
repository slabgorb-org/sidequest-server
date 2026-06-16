"""Tests for the query_known_facts tool — Phase C Task 10.

READ tool. Returns the perspective PC's ``known_facts`` with substring
+ confidence-floor filtering. No perception rule is registered — scoping
happens entirely inside the handler (only the perspective PC's facts are
ever returned).

Plan deviations exercised here:
    * Confidence is the real four-tier scale
      ``Rumored < Suspected < Discovered < Certain``, not the plan's
      speculative three-tier ``suspected/known/certain``.
    * Added a ``limit`` cap (default 20, max 100) — facts accumulate
      monotonically; an unbounded dump would blow the prompt window.
"""

from __future__ import annotations

import json
from typing import Any, cast

from sidequest.agents.narrator_perception_filter import NarratorPerceptionFilter
from sidequest.agents.tool_registry import (
    ToolContext,
    ToolResult,
    ToolResultStatus,
    default_registry,
)
from sidequest.agents.tooling_protocol import ToolUseBlock
from sidequest.agents.tools import query_known_facts as _query_known_facts_module  # noqa: F401
from sidequest.game.character import Character, KnownFact
from sidequest.game.creature_core import (
    CreatureCore,
    HpPool,
    Inventory,
)
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.protocol.models import FactCategory


def _character(
    name: str,
    *,
    known_facts: list[KnownFact] | None = None,
) -> Character:
    core = CreatureCore(
        name=name,
        description="d",
        personality="p",
        inventory=Inventory(items=[], gold=0),
        statuses=[],
        hp=HpPool(current=10, max=10, base_max=10),
    )
    return Character(
        core=core,
        backstory="A test hero.",
        char_class="Delver",
        race="Human",
        pronouns="they/them",
        stats={"str": 10},
        is_friendly=True,
        known_facts=known_facts or [],
    )


def _build_snapshot(*, characters: list[Character] | None = None) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=1),
        characters=characters or [],
    )


def _store_with(snapshot: GameSnapshot):
    from tests.agents.tools.conftest import pg_store_with

    return pg_store_with(snapshot)


def _make_ctx(
    store,
    *,
    perspective_pc: str | None = "Alice",
    session_id: str = "s",
    turn: int = 3,
) -> ToolContext:
    from unittest.mock import MagicMock

    return ToolContext(
        world_id="w",
        session_id=session_id,
        perspective_pc=perspective_pc,
        turn_number=turn,
        repository=store,
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
    )


async def _call(arguments: dict, ctx: ToolContext) -> ToolResult:
    """Invoke the registered handler directly (bypass dispatch)."""
    registered = default_registry._tools["query_known_facts"]
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, ctx)


def _payload(r: ToolResult) -> dict[str, Any]:
    assert r.payload is not None
    return cast(dict[str, Any], r.payload)


def _fact(
    content: str,
    *,
    confidence: str = "Suspected",
    source: str = "GameEvent",
    learned_turn: int = 1,
    fact_id: str | None = None,
    category: FactCategory = FactCategory.Lore,
) -> KnownFact:
    kwargs: dict[str, Any] = {
        "content": content,
        "confidence": confidence,
        "source": source,
        "learned_turn": learned_turn,
        "category": category,
    }
    if fact_id is not None:
        kwargs["fact_id"] = fact_id
    return KnownFact(**kwargs)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_query_known_facts_is_registered() -> None:
    assert "query_known_facts" in default_registry.list_names()


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


async def test_happy_path_returns_all_facts_for_perspective_pc() -> None:
    alice = _character(
        "Alice",
        known_facts=[
            _fact("The dragon sleeps", confidence="Rumored", fact_id="f1"),
            _fact("The well is poisoned", confidence="Suspected", fact_id="f2"),
            _fact("Tomas is the killer", confidence="Certain", fact_id="f3"),
        ],
    )
    snap = _build_snapshot(characters=[alice])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    r = await _call({}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["perspective_pc"] == "Alice"
    assert p["confidence_min"] == "Rumored"
    assert len(p["facts"]) == 3
    contents = {f["content"] for f in p["facts"]}
    assert contents == {
        "The dragon sleeps",
        "The well is poisoned",
        "Tomas is the killer",
    }
    # Shape check — first fact carries the expected fields
    first = next(f for f in p["facts"] if f["fact_id"] == "f1")
    assert first["confidence"] == "Rumored"
    assert first["source"] == "GameEvent"
    assert first["learned_turn"] == 1
    assert first["category"] == "Lore"


async def test_topic_filter_is_case_insensitive_substring() -> None:
    alice = _character(
        "Alice",
        known_facts=[
            _fact("The Dragon sleeps in the deep", confidence="Rumored"),
            _fact("A goblin shaman keeps a totem", confidence="Suspected"),
            _fact("The dragonfly was an omen", confidence="Certain"),
        ],
    )
    snap = _build_snapshot(characters=[alice])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    r = await _call({"topic": "dragon"}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    contents = sorted(f["content"] for f in p["facts"])
    assert contents == [
        "The Dragon sleeps in the deep",
        "The dragonfly was an omen",
    ]


async def test_confidence_min_discovered_drops_rumored_and_suspected() -> None:
    alice = _character(
        "Alice",
        known_facts=[
            _fact("Rumor", confidence="Rumored"),
            _fact("Hunch", confidence="Suspected"),
            _fact("Found", confidence="Discovered"),
            _fact("Locked", confidence="Certain"),
        ],
    )
    snap = _build_snapshot(characters=[alice])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    r = await _call({"confidence_min": "Discovered"}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    contents = sorted(f["content"] for f in p["facts"])
    assert contents == ["Found", "Locked"]


async def test_confidence_min_certain_returns_only_certain() -> None:
    alice = _character(
        "Alice",
        known_facts=[
            _fact("Rumor", confidence="Rumored"),
            _fact("Hunch", confidence="Suspected"),
            _fact("Found", confidence="Discovered"),
            _fact("Locked", confidence="Certain"),
            _fact("AlsoLocked", confidence="Certain"),
        ],
    )
    snap = _build_snapshot(characters=[alice])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    r = await _call({"confidence_min": "Certain"}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    contents = sorted(f["content"] for f in p["facts"])
    assert contents == ["AlsoLocked", "Locked"]


async def test_limit_caps_result_size() -> None:
    alice = _character(
        "Alice",
        known_facts=[_fact(f"fact-{i}", confidence="Certain", fact_id=f"f{i}") for i in range(10)],
    )
    snap = _build_snapshot(characters=[alice])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    r = await _call({"limit": 2}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert len(p["facts"]) == 2


async def test_empty_known_facts_returns_empty_list() -> None:
    alice = _character("Alice", known_facts=[])
    snap = _build_snapshot(characters=[alice])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    r = await _call({}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["facts"] == []
    assert p["perspective_pc"] == "Alice"


# ---------------------------------------------------------------------------
# Scoping — only perspective_pc's facts, never another PC's
# ---------------------------------------------------------------------------


async def test_perspective_none_returns_empty_list() -> None:
    """No perspective PC → narrator gets no facts.

    Plan said "ignore the model's request if perspective_pc is None";
    translated to "return empty list, no error" so the narrator can
    keep going.
    """
    alice = _character(
        "Alice",
        known_facts=[_fact("Secret thing", confidence="Certain")],
    )
    snap = _build_snapshot(characters=[alice])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc=None)

    r = await _call({}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["facts"] == []
    assert p["perspective_pc"] is None


async def test_perspective_pc_not_in_session_returns_not_found() -> None:
    alice = _character("Alice")
    snap = _build_snapshot(characters=[alice])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Ghost")

    r = await _call({}, ctx)
    assert r.status is ToolResultStatus.NOT_FOUND
    assert r.message is not None
    assert "Ghost" in r.message


async def test_other_pc_facts_are_not_returned() -> None:
    """Even though Bob has facts, Alice's query never sees them."""
    alice = _character(
        "Alice",
        known_facts=[_fact("Alice fact", confidence="Certain", fact_id="a1")],
    )
    bob = _character(
        "Bob",
        known_facts=[_fact("Bob secret", confidence="Certain", fact_id="b1")],
    )
    snap = _build_snapshot(characters=[alice, bob])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    r = await _call({}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    fact_ids = {f["fact_id"] for f in p["facts"]}
    assert fact_ids == {"a1"}


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


async def test_no_active_session_returns_fatal_error() -> None:
    from tests.agents.tools.conftest import pg_empty_store

    store = pg_empty_store()
    # No init_session/save — load() returns None.
    ctx = _make_ctx(store, perspective_pc="Alice")

    r = await _call({}, ctx)
    assert r.status is ToolResultStatus.ERROR_FATAL
    assert r.message is not None
    assert "no active session" in r.message


async def test_invalid_confidence_min_rejected_by_args_model() -> None:
    snap = _build_snapshot(characters=[_character("Alice")])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-bad-conf",
            name="query_known_facts",
            arguments={"confidence_min": "kinda-sure"},
        ),
        ctx,
    )
    assert out.is_error is True
    assert "argument validation failed" in out.content


async def test_limit_out_of_range_rejected_by_args_model() -> None:
    snap = _build_snapshot(characters=[_character("Alice")])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-bad-limit",
            name="query_known_facts",
            arguments={"limit": 0},
        ),
        ctx,
    )
    assert out.is_error is True
    assert "argument validation failed" in out.content


# ---------------------------------------------------------------------------
# OTEL
# ---------------------------------------------------------------------------


async def test_otel_span_carries_fact_count_and_topic(otel_capture) -> None:
    alice = _character(
        "Alice",
        known_facts=[
            _fact("The dragon sleeps", confidence="Rumored"),
            _fact("The dragonfly was an omen", confidence="Certain"),
            _fact("Unrelated thing", confidence="Suspected"),
        ],
    )
    snap = _build_snapshot(characters=[alice])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-otel-facts",
            name="query_known_facts",
            arguments={"topic": "dragon"},
        ),
        ctx,
    )
    assert out.is_error is False
    payload = json.loads(out.content)
    assert len(payload["facts"]) == 2

    spans = otel_capture.get_finished_spans()
    read_spans = [s for s in spans if s.name == "tool.read.query_known_facts"]
    assert read_spans, f"no tool.read.query_known_facts span; got: {[s.name for s in spans]}"
    attrs = dict(read_spans[-1].attributes or {})
    assert attrs.get("tool.name") == "query_known_facts"
    assert attrs.get("tool.category") == "read"
    assert attrs.get("tool.result_status") == "ok"
    assert attrs.get("tool.belief.fact_count") == 2
    assert attrs.get("tool.belief.topic") == "dragon"


async def test_otel_span_empty_topic_attr_when_no_filter(otel_capture) -> None:
    alice = _character(
        "Alice",
        known_facts=[_fact("A fact", confidence="Certain")],
    )
    snap = _build_snapshot(characters=[alice])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-otel-no-topic",
            name="query_known_facts",
            arguments={},
        ),
        ctx,
    )
    assert out.is_error is False
    spans = otel_capture.get_finished_spans()
    read_spans = [s for s in spans if s.name == "tool.read.query_known_facts"]
    assert read_spans
    attrs = dict(read_spans[-1].attributes or {})
    assert attrs.get("tool.belief.fact_count") == 1
    assert attrs.get("tool.belief.topic") == ""


async def test_otel_span_perspective_none_records_zero_count(otel_capture) -> None:
    snap = _build_snapshot(characters=[_character("Alice")])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc=None)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-otel-none",
            name="query_known_facts",
            arguments={},
        ),
        ctx,
    )
    assert out.is_error is False
    spans = otel_capture.get_finished_spans()
    read_spans = [s for s in spans if s.name == "tool.read.query_known_facts"]
    assert read_spans
    attrs = dict(read_spans[-1].attributes or {})
    assert attrs.get("tool.belief.fact_count") == 0
