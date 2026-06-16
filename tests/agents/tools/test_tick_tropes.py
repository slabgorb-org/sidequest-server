"""Tests for the tick_tropes tool — Phase C Task 20.

WRITE tool. Wraps :func:`sidequest.game.trope_tick.tick_tropes`. The v1
engine does not text-match on narration; the ``narration_text`` arg is
forward-compat only. The Phase B amendment (#3) adds ``genre_pack: Any``
to :class:`ToolContext` — Phase C tests cover both the wired path (with
a duck-typed pack carrying a single :class:`TropeDefinition`) and the
unwired path (the OTEL marker no-op).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
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
from sidequest.agents.tools import tick_tropes as _tick_tropes_module  # noqa: F401
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.persistence import SqliteStore
from sidequest.game.session import GameSnapshot, TropeState
from sidequest.game.turn import TurnManager
from sidequest.genre.models.tropes import (
    PassiveProgression,
    TropeDefinition,
    TropeEscalation,
)

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _character(name: str) -> Character:
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
        backstory="bs",
        char_class="Delver",
        race="Human",
    )


def _build_snapshot(
    *,
    active_tropes: list[TropeState] | None = None,
) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=1),
        characters=[_character("Alice")],
        npcs=[],
        active_tropes=active_tropes or [],
    )


def _store_with(snapshot: GameSnapshot) -> SqliteStore:
    store = SqliteStore.open_in_memory()
    store.initialize()
    store.init_session(genre_slug=snapshot.genre_slug, world_slug=snapshot.world_slug)
    store.save(snapshot)
    return store


@dataclass
class _FakePack:
    """Minimal duck-typed stand-in for GenrePack.

    ``trope_tick.tick_tropes`` only reads ``pack.tropes`` — a list of
    :class:`TropeDefinition`. The full :class:`GenrePack` is not required.
    """

    tropes: list[TropeDefinition] = field(default_factory=list)


def _make_ctx(
    store: SqliteStore,
    *,
    session_id: str = "s",
    turn_number: int = 1,
    genre_pack: Any | None = None,
) -> ToolContext:
    return ToolContext(
        world_id="w",
        session_id=session_id,
        perspective_pc="Alice",
        turn_number=turn_number,
        store=store,
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
        genre_pack=genre_pack,
    )


async def _call(arguments: dict, ctx: ToolContext) -> ToolResult:
    registered = default_registry._tools["tick_tropes"]
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, ctx)


def _payload(r: ToolResult) -> dict[str, Any]:
    assert r.payload is not None
    return cast(dict[str, Any], r.payload)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_tick_tropes_is_registered() -> None:
    assert "tick_tropes" in default_registry.list_names()


# ---------------------------------------------------------------------------
# Unwired (genre_pack=None) path
# ---------------------------------------------------------------------------


async def test_unwired_pack_no_ops_with_marker() -> None:
    snap = _build_snapshot(
        active_tropes=[TropeState(id="rivalry", status="dormant", progress=0.0)],
    )
    store = _store_with(snap)
    ctx = _make_ctx(store)  # genre_pack defaults to None

    r = await _call({"narration_text": "the duel begins"}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["engaged_count"] == 0
    assert p["engaged_names"] == []
    assert p["engaged_ids"] == []
    assert p["genre_pack_wired"] is False
    assert p["active_total"] == 1

    # Snapshot must NOT have been mutated when the pack is unwired.
    reloaded = store.load()
    assert reloaded is not None
    # No save was emitted on the unwired path either; the trope stays dormant.
    assert reloaded.snapshot.active_tropes[0].status == "dormant"


async def test_unwired_otel_marker_set() -> None:
    snap = _build_snapshot()
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"narration_text": "hello world", "days_advanced": 0}, ctx)
    assert r.status is ToolResultStatus.OK

    span = cast(MagicMock, ctx.otel_span)
    recorded = {call.args[0]: call.args[1] for call in span.set_attribute.call_args_list}
    assert recorded["tool.tropes.engaged_count"] == 0
    assert recorded["tool.tropes.engaged_names"] == []
    assert recorded["tool.tropes.genre_pack_wired"] is False
    assert recorded["tool.tropes.days_advanced"] == 0
    assert recorded["tool.tropes.narration_text_len"] == len("hello world")


# ---------------------------------------------------------------------------
# Wired path
# ---------------------------------------------------------------------------


async def test_wired_empty_pack_no_engagements() -> None:
    """With a pack whose tropes list is empty, the engine ticks but no
    trope can advance — engaged_count=0, but wired=True."""
    snap = _build_snapshot(active_tropes=[])
    store = _store_with(snap)
    pack = _FakePack(tropes=[])
    ctx = _make_ctx(store, genre_pack=pack)

    r = await _call({}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["engaged_count"] == 0
    assert p["genre_pack_wired"] is True
    assert p["active_total"] == 0


async def test_wired_dormant_activates_and_persists() -> None:
    """A dormant trope with no cooldown and headroom under the cap
    transitions to progressing — the diff surfaces that as engagement."""
    snap = _build_snapshot(
        active_tropes=[TropeState(id="rivalry", status="dormant", progress=0.0)],
    )
    store = _store_with(snap)
    pack = _FakePack(
        tropes=[
            TropeDefinition(
                id="rivalry",
                name="Rivalry",
                passive_progression=PassiveProgression(rate_per_turn=0.1),
                escalation=[TropeEscalation(at=0.5, event="duel called")],
            )
        ]
    )
    ctx = _make_ctx(store, genre_pack=pack, turn_number=1)

    r = await _call({}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["genre_pack_wired"] is True
    # rivalry went dormant → progressing this tick.
    assert "rivalry" in p["engaged_ids"]
    assert "rivalry" in p["engaged_names"]
    assert p["engaged_count"] == 1

    # Persistence: the new status must round-trip through the store.
    reloaded = store.load()
    assert reloaded is not None
    states = {t.id: t.status for t in reloaded.snapshot.active_tropes}
    assert states["rivalry"] == "progressing"


async def test_wired_already_progressing_no_new_engagement() -> None:
    """A trope already in ``progressing`` is not a *new* engagement on
    this tick — engaged_count stays 0 even though the engine ran."""
    snap = _build_snapshot(
        active_tropes=[
            TropeState(id="rivalry", status="progressing", progress=0.2),
        ],
    )
    store = _store_with(snap)
    pack = _FakePack(
        tropes=[
            TropeDefinition(
                id="rivalry",
                name="Rivalry",
                passive_progression=PassiveProgression(rate_per_turn=0.05),
                escalation=[TropeEscalation(at=0.9, event="duel")],
            )
        ]
    )
    ctx = _make_ctx(store, genre_pack=pack)

    r = await _call({}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["engaged_count"] == 0
    assert p["genre_pack_wired"] is True


async def test_wired_otel_attrs_full() -> None:
    snap = _build_snapshot(
        active_tropes=[TropeState(id="rivalry", status="dormant", progress=0.0)],
    )
    store = _store_with(snap)
    pack = _FakePack(
        tropes=[
            TropeDefinition(
                id="rivalry",
                name="Rivalry",
                passive_progression=PassiveProgression(rate_per_turn=0.1),
                escalation=[TropeEscalation(at=0.5, event="duel")],
            )
        ]
    )
    ctx = _make_ctx(store, genre_pack=pack)

    r = await _call({"narration_text": "tension rises", "days_advanced": 2}, ctx)
    assert r.status is ToolResultStatus.OK

    span = cast(MagicMock, ctx.otel_span)
    recorded = {call.args[0]: call.args[1] for call in span.set_attribute.call_args_list}
    assert recorded["tool.tropes.engaged_count"] == 1
    assert recorded["tool.tropes.engaged_names"] == ["rivalry"]
    assert recorded["tool.tropes.genre_pack_wired"] is True
    assert recorded["tool.tropes.days_advanced"] == 2
    assert recorded["tool.tropes.narration_text_len"] == len("tension rises")


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


async def test_no_active_session_returns_fatal_error() -> None:
    store = SqliteStore.open_in_memory()
    store.initialize()
    # No init_session/save — load() returns None.
    ctx = _make_ctx(store)

    r = await _call({}, ctx)
    assert r.status is ToolResultStatus.ERROR_FATAL
    assert r.message is not None
    assert "no active session" in r.message


async def test_negative_days_advanced_rejected_by_args_model() -> None:
    snap = _build_snapshot()
    store = _store_with(snap)
    ctx = _make_ctx(store)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-neg",
            name="tick_tropes",
            arguments={"days_advanced": -1},
        ),
        ctx,
    )
    assert out.is_error is True
    assert "argument validation failed" in out.content


# ---------------------------------------------------------------------------
# Concurrency — sequential WRITE-lock
# ---------------------------------------------------------------------------


async def test_parallel_tick_against_same_session_runs_sequentially() -> None:
    """Two concurrent dispatches share the per-session WRITE lock — both
    succeed without corrupting state."""
    snap = _build_snapshot(
        active_tropes=[TropeState(id="rivalry", status="dormant", progress=0.0)],
    )
    store = _store_with(snap)
    pack = _FakePack(
        tropes=[
            TropeDefinition(
                id="rivalry",
                name="Rivalry",
                passive_progression=PassiveProgression(rate_per_turn=0.1),
                escalation=[TropeEscalation(at=0.5, event="duel")],
            )
        ]
    )
    ctx = _make_ctx(store, session_id="shared", genre_pack=pack)

    results = await asyncio.gather(
        default_registry.dispatch(
            ToolUseBlock(id="d1", name="tick_tropes", arguments={}),
            ctx,
        ),
        default_registry.dispatch(
            ToolUseBlock(id="d2", name="tick_tropes", arguments={}),
            ctx,
        ),
    )
    assert all(r.is_error is False for r in results)

    # First call activates rivalry (engaged_count=1); second sees it already
    # progressing (engaged_count=0). Order can vary, but the multiset is fixed.
    counts = sorted(json.loads(r.content)["engaged_count"] for r in results)
    assert counts == [0, 1]

    reloaded = store.load()
    assert reloaded is not None
    states = {t.id: t.status for t in reloaded.snapshot.active_tropes}
    assert states["rivalry"] == "progressing"
