"""Tests for the apply_spell_effect tool — Phase C Task 23.

WRITE tool. v1 scope: record a ``WorkingRecord`` into
``MagicState.working_log`` with ``plugin='narrator_declared'`` and
optionally decrement the caster's character-scoped ``mana`` bar by the
declared cost. Does NOT invoke ``learned_ops.cast`` — the real resolver
hookup is Phase D/E when the SDK becomes the production narrator path.
"""

from __future__ import annotations

import asyncio
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
from sidequest.agents.tools import apply_spell_effect as _apply_spell_effect_module  # noqa: F401
from sidequest.game.persistence import SqliteStore
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.magic.models import (
    HardLimit,
    LedgerBarSpec,
    WorldKnowledge,
    WorldMagicConfig,
)
from sidequest.magic.state import (
    BarKey,
    MagicState,
    _serialize_bar_key,
)


def _world_config(*, include_mana: bool = True) -> WorldMagicConfig:
    """Minimal config; optionally includes a character-scope ``mana`` bar."""
    ledger_bars: list[LedgerBarSpec] = []
    if include_mana:
        ledger_bars.append(
            LedgerBarSpec(
                id="mana",
                scope="character",
                direction="down",
                range=(0.0, 10.0),
                threshold_low=2.0,
                consequence_on_low_cross="exhausted",
                starts_at_chargen=10.0,
            )
        )
    else:
        # World still needs at least one bar for from_config to be useful;
        # use a non-character bar so add_character creates no per-actor
        # rows. ``hegemony_heat`` is the canonical world-scope name.
        ledger_bars.append(
            LedgerBarSpec(
                id="hegemony_heat",
                scope="world",
                direction="up",
                range=(0.0, 100.0),
                threshold_high=80.0,
                consequence_on_high_cross="crackdown",
                starts_at_chargen=0.0,
            )
        )
    return WorldMagicConfig(
        world_slug="testworld",
        genre_slug="caverns_and_claudes",
        allowed_sources=["innate"],
        active_plugins=["innate_v1"],
        intensity=0.5,
        world_knowledge=WorldKnowledge(primary="acknowledged", local_register="folkloric"),
        visibility={"primary": "acknowledged", "local_register": "folkloric"},
        hard_limits=[HardLimit(id="x", description="x")],
        cost_types=["mana"] if include_mana else ["hegemony_heat"],
        ledger_bars=ledger_bars,
        narrator_register="plain",
    )


def _build_snapshot(*, magic_state: MagicState | None = None) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="testworld",
        turn_manager=TurnManager(interaction=1),
        characters=[],
        magic_state=magic_state,
    )


def _store_with(snapshot: GameSnapshot) -> SqliteStore:
    store = SqliteStore.open_in_memory()
    store.initialize()
    store.init_session(genre_slug=snapshot.genre_slug, world_slug=snapshot.world_slug)
    store.save(snapshot)
    return store


def _make_ctx(store: SqliteStore, *, session_id: str = "s") -> ToolContext:
    return ToolContext(
        world_id="testworld",
        session_id=session_id,
        perspective_pc="Alice",
        turn_number=3,
        store=store,
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
    )


async def _call(arguments: dict, ctx: ToolContext) -> ToolResult:
    """Invoke the registered handler directly (bypass dispatch + perception)."""
    registered = default_registry._tools["apply_spell_effect"]
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, ctx)


def _payload(r: ToolResult) -> dict[str, Any]:
    assert r.payload is not None
    return cast(dict[str, Any], r.payload)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_apply_spell_effect_is_registered() -> None:
    assert "apply_spell_effect" in default_registry.list_names()


# ---------------------------------------------------------------------------
# Happy path — working_log gets a record
# ---------------------------------------------------------------------------


async def test_happy_path_records_working_log_entry() -> None:
    ms = MagicState.from_config(_world_config())
    ms.add_character("Alice")
    snap = _build_snapshot(magic_state=ms)
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call(
        {
            "spell_id": "magic_missile",
            "caster": "Alice",
            "targets": ["goblin"],
            "cost": 0,
        },
        ctx,
    )
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["spell_id"] == "magic_missile"
    assert p["caster"] == "Alice"
    assert p["targets"] == ["goblin"]
    assert p["cost"] == 0
    assert p["mana_decremented"] is False
    assert p["mana_remaining_after"] is None
    assert p["working_log_size"] == 1

    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.snapshot.magic_state is not None
    log = reloaded.snapshot.magic_state.working_log
    assert len(log) == 1
    record = log[0]
    assert record.plugin == "narrator_declared"
    assert record.mechanism == "apply_spell_effect_tool"
    assert record.actor == "Alice"
    assert record.spell_id == "magic_missile"
    # cost=0 means costs dict is empty (no mana decrement attempted).
    assert record.costs == {}


# ---------------------------------------------------------------------------
# Mana decrement when bar exists
# ---------------------------------------------------------------------------


async def test_cost_decrements_mana_bar_when_present() -> None:
    ms = MagicState.from_config(_world_config(include_mana=True))
    ms.add_character("Alice")
    snap = _build_snapshot(magic_state=ms)
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call(
        {
            "spell_id": "fireball",
            "caster": "Alice",
            "targets": ["goblin", "kobold"],
            "cost": 5,
        },
        ctx,
    )
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["mana_decremented"] is True
    assert p["mana_remaining_after"] == 5.0

    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.snapshot.magic_state is not None
    key = _serialize_bar_key(BarKey(scope="character", owner_id="Alice", bar_id="mana"))
    assert reloaded.snapshot.magic_state.ledger[key].value == 5.0
    record = reloaded.snapshot.magic_state.working_log[0]
    assert record.costs == {"mana": 5.0}


async def test_cost_decrement_clamps_at_zero() -> None:
    """Spending more mana than available clamps the bar at 0 (never negative)."""
    ms = MagicState.from_config(_world_config(include_mana=True))
    ms.add_character("Alice")
    snap = _build_snapshot(magic_state=ms)
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call(
        {"spell_id": "meteor", "caster": "Alice", "targets": [], "cost": 99},
        ctx,
    )
    p = _payload(r)
    assert p["mana_decremented"] is True
    assert p["mana_remaining_after"] == 0.0


# ---------------------------------------------------------------------------
# No mana bar — log entry still appended, no decrement
# ---------------------------------------------------------------------------


async def test_cost_without_mana_bar_skips_decrement_but_appends_record() -> None:
    ms = MagicState.from_config(_world_config(include_mana=False))
    # No character-scope bars in this config → add_character is a no-op for
    # ledger rows but valid for the API contract.
    ms.add_character("Alice")
    snap = _build_snapshot(magic_state=ms)
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call(
        {"spell_id": "blink", "caster": "Alice", "targets": [], "cost": 5},
        ctx,
    )
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["mana_decremented"] is False
    assert p["mana_remaining_after"] is None
    assert p["working_log_size"] == 1

    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.snapshot.magic_state is not None
    # Log entry is appended even though decrement was skipped.
    assert len(reloaded.snapshot.magic_state.working_log) == 1
    # Cost still recorded — v1 surfaces the narrator-declared cost even
    # when there's no bar to deduct against; the GM panel can see the gap.
    assert reloaded.snapshot.magic_state.working_log[0].costs == {"mana": 5.0}


async def test_cost_zero_never_attempts_decrement() -> None:
    """cost=0 → no mana mutation attempted even when bar exists."""
    ms = MagicState.from_config(_world_config(include_mana=True))
    ms.add_character("Alice")
    snap = _build_snapshot(magic_state=ms)
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call(
        {"spell_id": "cantrip", "caster": "Alice", "targets": [], "cost": 0},
        ctx,
    )
    p = _payload(r)
    assert p["mana_decremented"] is False
    assert p["mana_remaining_after"] is None

    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.snapshot.magic_state is not None
    key = _serialize_bar_key(BarKey(scope="character", owner_id="Alice", bar_id="mana"))
    # Bar untouched.
    assert reloaded.snapshot.magic_state.ledger[key].value == 10.0


# ---------------------------------------------------------------------------
# Targets + overrides round-trip
# ---------------------------------------------------------------------------


async def test_targets_list_round_trips() -> None:
    ms = MagicState.from_config(_world_config())
    ms.add_character("Alice")
    snap = _build_snapshot(magic_state=ms)
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call(
        {
            "spell_id": "mass_heal",
            "caster": "Alice",
            "targets": ["Bob", "Carol", "Dave"],
            "cost": 0,
        },
        ctx,
    )
    p = _payload(r)
    assert p["targets"] == ["Bob", "Carol", "Dave"]

    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.snapshot.magic_state is not None
    flavor_blob = reloaded.snapshot.magic_state.working_log[0].flavor
    assert flavor_blob is not None
    parsed = json.loads(flavor_blob)
    assert parsed["targets"] == ["Bob", "Carol", "Dave"]


async def test_overrides_dict_round_trips_through_flavor_json() -> None:
    ms = MagicState.from_config(_world_config())
    ms.add_character("Alice")
    snap = _build_snapshot(magic_state=ms)
    store = _store_with(snap)
    ctx = _make_ctx(store)

    overrides = {"range": "120ft", "duration": "1 minute", "save_dc": 15}
    r = await _call(
        {
            "spell_id": "hold_person",
            "caster": "Alice",
            "targets": ["bandit"],
            "cost": 0,
            "overrides": overrides,
        },
        ctx,
    )
    assert r.status is ToolResultStatus.OK

    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.snapshot.magic_state is not None
    flavor_blob = reloaded.snapshot.magic_state.working_log[0].flavor
    assert flavor_blob is not None
    parsed = json.loads(flavor_blob)
    assert parsed["overrides"] == overrides


# ---------------------------------------------------------------------------
# Fatal errors
# ---------------------------------------------------------------------------


async def test_no_magic_state_returns_fatal_error() -> None:
    """magic_state=None means the world has no magic config — fatal."""
    snap = _build_snapshot(magic_state=None)
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call(
        {"spell_id": "fireball", "caster": "Alice", "targets": [], "cost": 0},
        ctx,
    )
    assert r.status is ToolResultStatus.ERROR_FATAL
    assert r.message is not None
    assert "no magic_state" in r.message


async def test_no_active_session_returns_fatal_error() -> None:
    store = SqliteStore.open_in_memory()
    store.initialize()
    # No init_session/save → load() returns None.
    ctx = _make_ctx(store)

    r = await _call(
        {"spell_id": "fireball", "caster": "Alice", "targets": [], "cost": 0},
        ctx,
    )
    assert r.status is ToolResultStatus.ERROR_FATAL
    assert r.message is not None
    assert "no active session" in r.message


# ---------------------------------------------------------------------------
# Validator errors
# ---------------------------------------------------------------------------


async def test_empty_spell_id_rejected_by_args_model() -> None:
    ms = MagicState.from_config(_world_config())
    snap = _build_snapshot(magic_state=ms)
    store = _store_with(snap)
    ctx = _make_ctx(store)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-empty-spell",
            name="apply_spell_effect",
            arguments={"spell_id": "", "caster": "Alice", "targets": [], "cost": 0},
        ),
        ctx,
    )
    assert out.is_error is True
    assert "argument validation failed" in out.content


async def test_empty_caster_rejected_by_args_model() -> None:
    ms = MagicState.from_config(_world_config())
    snap = _build_snapshot(magic_state=ms)
    store = _store_with(snap)
    ctx = _make_ctx(store)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-empty-caster",
            name="apply_spell_effect",
            arguments={"spell_id": "fireball", "caster": "", "targets": [], "cost": 0},
        ),
        ctx,
    )
    assert out.is_error is True
    assert "argument validation failed" in out.content


async def test_negative_cost_rejected_by_args_model() -> None:
    ms = MagicState.from_config(_world_config())
    snap = _build_snapshot(magic_state=ms)
    store = _store_with(snap)
    ctx = _make_ctx(store)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-neg-cost",
            name="apply_spell_effect",
            arguments={"spell_id": "x", "caster": "Alice", "targets": [], "cost": -1},
        ),
        ctx,
    )
    assert out.is_error is True
    assert "argument validation failed" in out.content


# ---------------------------------------------------------------------------
# OTEL — dispatch span carries the per-tool attrs
# ---------------------------------------------------------------------------


async def test_otel_span_carries_spell_attrs_with_mana(otel_capture) -> None:
    ms = MagicState.from_config(_world_config(include_mana=True))
    ms.add_character("Alice")
    snap = _build_snapshot(magic_state=ms)
    store = _store_with(snap)
    ctx = _make_ctx(store)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-otel-mana",
            name="apply_spell_effect",
            arguments={
                "spell_id": "fireball",
                "caster": "Alice",
                "targets": ["g1", "g2", "g3"],
                "cost": 3,
            },
        ),
        ctx,
    )
    assert out.is_error is False

    spans = otel_capture.get_finished_spans()
    write_spans = [s for s in spans if s.name == "tool.write.apply_spell_effect"]
    assert write_spans, f"no tool.write.apply_spell_effect span; got: {[s.name for s in spans]}"
    attrs = dict(write_spans[-1].attributes or {})
    assert attrs.get("tool.name") == "apply_spell_effect"
    assert attrs.get("tool.category") == "write"
    assert attrs.get("tool.result_status") == "ok"
    assert attrs.get("tool.spell.id") == "fireball"
    assert attrs.get("tool.spell.caster") == "Alice"
    assert attrs.get("tool.spell.target_count") == 3
    assert attrs.get("tool.spell.cost") == 3
    assert attrs.get("tool.spell.mana_decremented") is True
    assert attrs.get("tool.spell.mana_remaining_after") == 7.0


async def test_otel_span_records_no_decrement_when_bar_missing(otel_capture) -> None:
    ms = MagicState.from_config(_world_config(include_mana=False))
    snap = _build_snapshot(magic_state=ms)
    store = _store_with(snap)
    ctx = _make_ctx(store)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-otel-no-bar",
            name="apply_spell_effect",
            arguments={
                "spell_id": "blink",
                "caster": "Alice",
                "targets": [],
                "cost": 2,
            },
        ),
        ctx,
    )
    assert out.is_error is False

    spans = otel_capture.get_finished_spans()
    write_spans = [s for s in spans if s.name == "tool.write.apply_spell_effect"]
    assert write_spans
    attrs = dict(write_spans[-1].attributes or {})
    assert attrs.get("tool.spell.mana_decremented") is False
    # mana_remaining_after is None → attribute should not be set.
    assert "tool.spell.mana_remaining_after" not in attrs


# ---------------------------------------------------------------------------
# Sequential WRITE-lock — concurrent dispatches don't tear the log
# ---------------------------------------------------------------------------


async def test_parallel_apply_spell_effect_runs_sequentially() -> None:
    ms = MagicState.from_config(_world_config(include_mana=True))
    ms.add_character("Alice")
    snap = _build_snapshot(magic_state=ms)
    store = _store_with(snap)
    ctx = _make_ctx(store, session_id="shared-session")

    results = await asyncio.gather(
        default_registry.dispatch(
            ToolUseBlock(
                id="s1",
                name="apply_spell_effect",
                arguments={
                    "spell_id": "magic_missile",
                    "caster": "Alice",
                    "targets": ["g1"],
                    "cost": 1,
                },
            ),
            ctx,
        ),
        default_registry.dispatch(
            ToolUseBlock(
                id="s2",
                name="apply_spell_effect",
                arguments={
                    "spell_id": "shield",
                    "caster": "Alice",
                    "targets": [],
                    "cost": 2,
                },
            ),
            ctx,
        ),
    )
    assert all(r.is_error is False for r in results)

    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.snapshot.magic_state is not None
    log = reloaded.snapshot.magic_state.working_log
    assert len(log) == 2
    assert {r.spell_id for r in log} == {"magic_missile", "shield"}
    # Both costs deducted: 10 - 1 - 2 = 7.
    key = _serialize_bar_key(BarKey(scope="character", owner_id="Alice", bar_id="mana"))
    assert reloaded.snapshot.magic_state.ledger[key].value == 7.0


# ---------------------------------------------------------------------------
# Dispatch path returns serialized payload
# ---------------------------------------------------------------------------


async def test_dispatch_path_returns_json_payload() -> None:
    ms = MagicState.from_config(_world_config())
    ms.add_character("Alice")
    snap = _build_snapshot(magic_state=ms)
    store = _store_with(snap)
    ctx = _make_ctx(store)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-dispatch",
            name="apply_spell_effect",
            arguments={
                "spell_id": "light",
                "caster": "Alice",
                "targets": [],
                "cost": 0,
            },
        ),
        ctx,
    )
    assert out.is_error is False
    payload = json.loads(out.content)
    assert payload["spell_id"] == "light"
    assert payload["caster"] == "Alice"
    assert payload["working_log_size"] == 1
