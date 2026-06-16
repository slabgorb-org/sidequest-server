"""Tests for the commit_known_fact tool — Phase C Task 11.

WRITE tool. Appends a :class:`~sidequest.game.character.KnownFact` to
the perspective PC's ``known_facts`` list and persists the snapshot.

Plan deviations exercised here:
    * **Confidence** — the plan listed
      ``Literal["suspected", "known", "certain"]`` (lowercase, three
      tiers). The real model uses capitalised four-tier
      ``Literal["Certain", "Suspected", "Rumored", "Discovered"]``. We
      forward the real scale.
    * **topic_tags** — the plan listed ``topic_tags: list[str]``. The
      real model carries a single ``category: FactCategory`` enum
      instead. v1 takes ``category: Literal["Lore","Place","Person",
      "Quest","Ability"] = "Lore"``.
    * **Default confidence** — ``"Discovered"`` is chosen as the
      default since it matches the scenario-clue intake path (see
      ``KnownFact`` docstring), not the plan's ``"known"`` which
      doesn't map to a real tier.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, cast

from sidequest.agents.narrator_perception_filter import NarratorPerceptionFilter
from sidequest.agents.tool_registry import (
    ToolContext,
    ToolResult,
    ToolResultStatus,
    default_registry,
)
from sidequest.agents.tooling_protocol import ToolUseBlock
from sidequest.agents.tools import commit_known_fact as _commit_known_fact_module  # noqa: F401
from sidequest.game.character import Character, KnownFact
from sidequest.game.creature_core import (
    CreatureCore,
    HpPool,
    Inventory,
)
from sidequest.game.persistence import SqliteStore
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager


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


def _store_with(snapshot: GameSnapshot) -> SqliteStore:
    store = SqliteStore.open_in_memory()
    store.initialize()
    store.init_session(genre_slug=snapshot.genre_slug, world_slug=snapshot.world_slug)
    store.save(snapshot)
    return store


def _make_ctx(
    store: SqliteStore,
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
        store=store,
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
    )


async def _call(arguments: dict, ctx: ToolContext) -> ToolResult:
    """Invoke the registered handler directly (bypass dispatch span)."""
    registered = default_registry._tools["commit_known_fact"]
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, ctx)


def _payload(r: ToolResult) -> dict[str, Any]:
    assert r.payload is not None
    return cast(dict[str, Any], r.payload)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_commit_known_fact_is_registered() -> None:
    assert "commit_known_fact" in default_registry.list_names()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_writes_fact_and_persists_across_reload() -> None:
    alice = _character("Alice")
    snap = _build_snapshot(characters=[alice])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice", turn=5)

    r = await _call(
        {
            "text": "The well is poisoned",
            "confidence": "Suspected",
            "source": "narrator",
            "category": "Place",
        },
        ctx,
    )
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["content"] == "The well is poisoned"
    assert p["confidence"] == "Suspected"
    assert p["source"] == "narrator"
    assert p["category"] == "Place"
    assert p["learned_turn"] == 5
    assert p["perspective_pc"] == "Alice"
    assert "fact_id" in p
    assert p["fact_id"]

    # Persisted across reread.
    reloaded = store.load()
    assert reloaded is not None
    pc = next(c for c in reloaded.snapshot.characters if c.core.name == "Alice")
    assert len(pc.known_facts) == 1
    fact = pc.known_facts[0]
    assert fact.content == "The well is poisoned"
    assert fact.confidence == "Suspected"
    assert fact.source == "narrator"
    assert fact.category.value == "Place"
    assert fact.learned_turn == 5
    assert fact.fact_id == p["fact_id"]


async def test_defaults_are_discovered_and_lore() -> None:
    alice = _character("Alice")
    snap = _build_snapshot(characters=[alice])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    r = await _call({"text": "Something happened"}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["confidence"] == "Discovered"
    assert p["category"] == "Lore"
    assert p["source"] == "narrator"


async def test_learned_turn_pulled_from_ctx_turn_number() -> None:
    alice = _character("Alice")
    snap = _build_snapshot(characters=[alice])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice", turn=42)

    r = await _call({"text": "A fact"}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["learned_turn"] == 42


async def test_fact_id_is_auto_minted_uuid_hex() -> None:
    alice = _character("Alice")
    snap = _build_snapshot(characters=[alice])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    r = await _call({"text": "A fact"}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    # UUID hex is 32 lowercase hex chars.
    assert re.fullmatch(r"[0-9a-f]{32}", p["fact_id"]) is not None


async def test_appending_does_not_lose_existing_facts() -> None:
    existing = KnownFact(
        content="Pre-existing",
        confidence="Certain",
        source="GameEvent",
        learned_turn=1,
        fact_id="existing-1",
    )
    alice = _character("Alice", known_facts=[existing])
    snap = _build_snapshot(characters=[alice])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    r = await _call({"text": "Brand new"}, ctx)
    assert r.status is ToolResultStatus.OK

    reloaded = store.load()
    assert reloaded is not None
    pc = next(c for c in reloaded.snapshot.characters if c.core.name == "Alice")
    contents = [f.content for f in pc.known_facts]
    assert contents == ["Pre-existing", "Brand new"]


# ---------------------------------------------------------------------------
# Enum coverage
# ---------------------------------------------------------------------------


async def test_all_four_confidence_values_accepted() -> None:
    for tier in ("Rumored", "Suspected", "Discovered", "Certain"):
        alice = _character("Alice")
        snap = _build_snapshot(characters=[alice])
        store = _store_with(snap)
        ctx = _make_ctx(store, perspective_pc="Alice")

        r = await _call({"text": f"Fact at {tier}", "confidence": tier}, ctx)
        assert r.status is ToolResultStatus.OK
        assert _payload(r)["confidence"] == tier


async def test_all_five_category_values_accepted() -> None:
    for cat in ("Lore", "Place", "Person", "Quest", "Ability"):
        alice = _character("Alice")
        snap = _build_snapshot(characters=[alice])
        store = _store_with(snap)
        ctx = _make_ctx(store, perspective_pc="Alice")

        r = await _call({"text": f"Fact in {cat}", "category": cat}, ctx)
        assert r.status is ToolResultStatus.OK
        assert _payload(r)["category"] == cat


# ---------------------------------------------------------------------------
# Scoping
# ---------------------------------------------------------------------------


async def test_perspective_none_returns_fatal_error() -> None:
    """Cannot commit a fact without a PC to attach it to."""
    alice = _character("Alice")
    snap = _build_snapshot(characters=[alice])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc=None)

    r = await _call({"text": "Orphan fact"}, ctx)
    assert r.status is ToolResultStatus.ERROR_FATAL
    assert r.message is not None
    assert "perspective_pc" in r.message


async def test_perspective_pc_not_in_session_returns_not_found() -> None:
    alice = _character("Alice")
    snap = _build_snapshot(characters=[alice])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Ghost")

    r = await _call({"text": "A fact"}, ctx)
    assert r.status is ToolResultStatus.NOT_FOUND
    assert r.message is not None
    assert "Ghost" in r.message


async def test_other_pc_facts_are_untouched() -> None:
    """Writing to Alice must not touch Bob's known_facts."""
    alice = _character("Alice")
    bob = _character(
        "Bob",
        known_facts=[
            KnownFact(
                content="Bob fact",
                confidence="Certain",
                source="GameEvent",
                learned_turn=1,
                fact_id="b1",
            )
        ],
    )
    snap = _build_snapshot(characters=[alice, bob])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    r = await _call({"text": "Alice fact"}, ctx)
    assert r.status is ToolResultStatus.OK

    reloaded = store.load()
    assert reloaded is not None
    bob_post = next(c for c in reloaded.snapshot.characters if c.core.name == "Bob")
    assert len(bob_post.known_facts) == 1
    assert bob_post.known_facts[0].content == "Bob fact"
    assert bob_post.known_facts[0].fact_id == "b1"
    alice_post = next(c for c in reloaded.snapshot.characters if c.core.name == "Alice")
    assert len(alice_post.known_facts) == 1
    assert alice_post.known_facts[0].content == "Alice fact"


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


async def test_empty_text_rejected_by_args_model() -> None:
    alice = _character("Alice")
    snap = _build_snapshot(characters=[alice])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-empty-text",
            name="commit_known_fact",
            arguments={"text": ""},
        ),
        ctx,
    )
    assert out.is_error is True
    assert "argument validation failed" in out.content


async def test_invalid_confidence_rejected_by_args_model() -> None:
    alice = _character("Alice")
    snap = _build_snapshot(characters=[alice])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-bad-conf",
            name="commit_known_fact",
            arguments={"text": "x", "confidence": "kinda-sure"},
        ),
        ctx,
    )
    assert out.is_error is True
    assert "argument validation failed" in out.content


async def test_invalid_category_rejected_by_args_model() -> None:
    alice = _character("Alice")
    snap = _build_snapshot(characters=[alice])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-bad-cat",
            name="commit_known_fact",
            arguments={"text": "x", "category": "Weather"},
        ),
        ctx,
    )
    assert out.is_error is True
    assert "argument validation failed" in out.content


async def test_no_active_session_returns_fatal_error() -> None:
    store = SqliteStore.open_in_memory()
    store.initialize()
    # No init_session/save — load() returns None.
    ctx = _make_ctx(store, perspective_pc="Alice")

    r = await _call({"text": "A fact"}, ctx)
    assert r.status is ToolResultStatus.ERROR_FATAL
    assert r.message is not None
    assert "no active session" in r.message


# ---------------------------------------------------------------------------
# OTEL
# ---------------------------------------------------------------------------


async def test_otel_span_carries_belief_attrs(otel_capture) -> None:
    alice = _character("Alice")
    snap = _build_snapshot(characters=[alice])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice", turn=7)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-otel-commit",
            name="commit_known_fact",
            arguments={
                "text": "The dragon sleeps",
                "confidence": "Rumored",
                "source": "narrator",
                "category": "Lore",
            },
        ),
        ctx,
    )
    assert out.is_error is False
    payload = json.loads(out.content)
    fact_id = payload["fact_id"]

    spans = otel_capture.get_finished_spans()
    write_spans = [s for s in spans if s.name == "tool.write.commit_known_fact"]
    assert write_spans, f"no tool.write.commit_known_fact span; got: {[s.name for s in spans]}"
    attrs = dict(write_spans[-1].attributes or {})
    # Dispatcher-seeded standard attrs
    assert attrs.get("tool.name") == "commit_known_fact"
    assert attrs.get("tool.category") == "write"
    assert attrs.get("tool.result_status") == "ok"
    # Handler-set per-tool attrs — must land on the dispatch span
    assert attrs.get("tool.belief.fact_id") == fact_id
    assert attrs.get("tool.belief.confidence") == "Rumored"
    assert attrs.get("tool.belief.category") == "Lore"
    assert attrs.get("tool.belief.source") == "narrator"


# ---------------------------------------------------------------------------
# WRITE-lock serialization
# ---------------------------------------------------------------------------


async def test_parallel_commits_run_sequentially() -> None:
    """Concurrent dispatches for the same session share a WRITE lock —
    both facts must land cleanly (no torn read-modify-write)."""
    alice = _character("Alice")
    snap = _build_snapshot(characters=[alice])
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice", session_id="shared-session")

    results = await asyncio.gather(
        default_registry.dispatch(
            ToolUseBlock(
                id="c1",
                name="commit_known_fact",
                arguments={"text": "first fact"},
            ),
            ctx,
        ),
        default_registry.dispatch(
            ToolUseBlock(
                id="c2",
                name="commit_known_fact",
                arguments={"text": "second fact"},
            ),
            ctx,
        ),
    )
    assert all(r.is_error is False for r in results)

    reloaded = store.load()
    assert reloaded is not None
    pc = next(c for c in reloaded.snapshot.characters if c.core.name == "Alice")
    contents = sorted(f.content for f in pc.known_facts)
    assert contents == ["first fact", "second fact"]
