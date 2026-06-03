"""RED tests for the ``record_quest`` tool — Story 77-2 (ADR-137 Option B).

``record_quest`` is the narrator-facing create/evolve affordance for the
campaign spine: mint a structured quest (id + title + objective + status +
optional anchor) on a fresh id, or update status on an existing one. It is a
``ToolCategory.WRITE`` adapter following the ADR-102 typed contract, modelled
on ``update_npc_disposition`` (load -> mutate -> save -> OTEL).

Lie-detector contract (CLAUDE.md OTEL Observability Principle):
- minting a new quest fires ``quest.created`` (quest_id, title, source,
  anchor_count)
- updating an existing quest's status fires ``quest.updated`` (quest_id,
  old_status, new_status) — the behavioural successor to the legacy
  ``SPAN_QUEST_UPDATE`` lane (the old span may co-fire until 77-4).

State-bloat guardrail (the explicit YAML AC): a cardinality cap on
``quest_log`` so a runaway narrator cannot mint unbounded quests into
Postgres. The cap blocks *minting* past the limit but never blocks updating
an existing quest. The per-call schema is bound tightly (ADR-102 / SOUL
§Cost Scales with Drama) so a quiet town walk cannot smuggle a giant payload.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from typing import Any, cast

from sidequest.agents.narrator_perception_filter import NarratorPerceptionFilter
from sidequest.agents.tool_registry import (
    ToolContext,
    ToolResult,
    ToolResultStatus,
    default_registry,
)
from sidequest.agents.tooling_protocol import ToolUseBlock
from sidequest.agents.tools import record_quest as _record_quest_module  # noqa: F401
from sidequest.game.session import GameSnapshot, QuestEntry, TurnManager


def _build_snapshot(*, quest_log: dict[str, QuestEntry] | None = None) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="wry_whimsy",
        world_slug="oz",
        turn_manager=TurnManager(interaction=1),
        quest_log=quest_log or {},
    )


def _store_with(snapshot: GameSnapshot):
    from tests.agents.tools.conftest import pg_store_with

    return pg_store_with(snapshot)


def _make_ctx(store, *, session_id: str = "s", turn: int = 4) -> ToolContext:
    from unittest.mock import MagicMock

    return ToolContext(
        world_id="w",
        session_id=session_id,
        perspective_pc="Dorothy",
        turn_number=turn,
        repository=store,
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
    )


async def _call(arguments: dict, ctx: ToolContext) -> ToolResult:
    registered = default_registry._tools["record_quest"]  # noqa: SLF001
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, ctx)


def _payload(r: ToolResult) -> dict[str, Any]:
    assert r.payload is not None
    return cast(dict[str, Any], r.payload)


_MINT = {
    "quest_id": "q_witch",
    "title": "Defeat the Witch",
    "objective": "Reach the Emerald City and confront the Witch",
}


# --------------------------------------------------------------------------- #
# Registration / wiring
# --------------------------------------------------------------------------- #


def test_record_quest_is_registered() -> None:
    assert "record_quest" in default_registry.list_names()


def test_record_quest_and_set_stakes_wired_via_barrel_subprocess() -> None:
    """Production-path wiring proof (CLAUDE.md "Verify Wiring, Not Just
    Existence" + the import-side-effect-needs-subprocess lesson): a FRESH
    interpreter that imports ONLY the tools barrel must expose both new
    tools. An in-process check is false-green because this test module
    imports the tool directly, registering it regardless of the barrel.
    """
    code = (
        "import sidequest.agents.tools  # barrel import fires @tool\n"
        "from sidequest.agents.tool_registry import default_registry\n"
        "names = set(default_registry.list_names())\n"
        "missing = {'record_quest', 'set_stakes'} - names\n"
        "assert not missing, f'not registered via barrel: {missing}'\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, (
        f"barrel did not register the new tools.\nstdout={result.stdout}\nstderr={result.stderr}"
    )


# --------------------------------------------------------------------------- #
# AC-2 — mint a structured quest
# --------------------------------------------------------------------------- #


async def test_mint_creates_structured_quest_entry() -> None:
    store = _store_with(_build_snapshot())
    ctx = _make_ctx(store)

    r = await _call(_MINT, ctx)
    assert r.status is ToolResultStatus.OK

    reloaded = store.load()
    assert reloaded is not None
    entry = reloaded.snapshot.quest_log["q_witch"]
    assert isinstance(entry, QuestEntry)
    assert entry.title == "Defeat the Witch"
    assert entry.objective == "Reach the Emerald City and confront the Witch"
    assert entry.status == "active"  # default status on mint


async def test_mint_fires_quest_created_span(otel_capture) -> None:
    store = _store_with(_build_snapshot())
    ctx = _make_ctx(store)

    out = await default_registry.dispatch(
        ToolUseBlock(id="t-mint", name="record_quest", arguments=_MINT), ctx
    )
    assert out.is_error is False

    spans = otel_capture.get_finished_spans()
    created = [s for s in spans if s.name == "quest.created"]
    assert created, f"no quest.created span; got {[s.name for s in spans]}"
    attrs = dict(created[-1].attributes or {})
    assert attrs.get("quest_id") == "q_witch"
    assert attrs.get("title") == "Defeat the Witch"
    assert attrs.get("source") == "narrator"
    assert attrs.get("anchor_count") == 0


async def test_mint_with_anchor_writes_anchor_and_counts_it(otel_capture) -> None:
    store = _store_with(_build_snapshot())
    ctx = _make_ctx(store)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-mint-anchor",
            name="record_quest",
            arguments={**_MINT, "anchor": "emerald_city_beat"},
        ),
        ctx,
    )
    assert out.is_error is False

    reloaded = store.load()
    assert reloaded is not None
    # Anchor lands on the snapshot's quest_anchors list (77-3 owns the
    # WorldStatePatch promotion; this story writes the loaded snapshot).
    assert "emerald_city_beat" in reloaded.snapshot.quest_anchors
    assert reloaded.snapshot.quest_log["q_witch"].anchor_id == "emerald_city_beat"

    created = [s for s in otel_capture.get_finished_spans() if s.name == "quest.created"]
    assert created
    assert dict(created[-1].attributes or {}).get("anchor_count") == 1


# --------------------------------------------------------------------------- #
# AC-3 — status update mode fires quest.updated
# --------------------------------------------------------------------------- #


async def test_update_existing_quest_changes_status_and_fires_quest_updated(
    otel_capture,
) -> None:
    snap = _build_snapshot(
        quest_log={
            "q_witch": QuestEntry(
                title="Defeat the Witch", objective="reach Oz", status="active"
            )
        }
    )
    store = _store_with(snap)
    ctx = _make_ctx(store)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-update",
            name="record_quest",
            arguments={**_MINT, "status": "resolved"},
        ),
        ctx,
    )
    assert out.is_error is False

    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.snapshot.quest_log["q_witch"].status == "resolved"

    updated = [s for s in otel_capture.get_finished_spans() if s.name == "quest.updated"]
    assert updated, "status change on an existing quest must fire quest.updated"
    attrs = dict(updated[-1].attributes or {})
    assert attrs.get("quest_id") == "q_witch"
    assert attrs.get("old_status") == "active"
    assert attrs.get("new_status") == "resolved"


async def test_update_does_not_fire_quest_created(otel_capture) -> None:
    snap = _build_snapshot(
        quest_log={"q_witch": QuestEntry(title="t", objective="o", status="active")}
    )
    store = _store_with(snap)
    ctx = _make_ctx(store)

    await default_registry.dispatch(
        ToolUseBlock(
            id="t-upd2", name="record_quest", arguments={**_MINT, "status": "resolved"}
        ),
        ctx,
    )
    created = [s for s in otel_capture.get_finished_spans() if s.name == "quest.created"]
    assert not created, "updating an existing quest must NOT mint (no quest.created)"


# --------------------------------------------------------------------------- #
# AC-1 — cardinality cap (state-bloat guardrail)
# --------------------------------------------------------------------------- #


def _full_quest_log(n: int = 32) -> dict[str, QuestEntry]:
    return {
        f"q{i}": QuestEntry(title=f"t{i}", objective=f"o{i}", status="active")
        for i in range(n)
    }


async def test_mint_past_cardinality_cap_is_refused_loudly() -> None:
    """At the cap, a NEW quest is refused with a structured error (No Silent
    Fallbacks) and state does not grow — the unbounded-Postgres-growth vector
    the YAML AC names."""
    snap = _build_snapshot(quest_log=_full_quest_log(32))
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({**_MINT, "quest_id": "q_overflow"}, ctx)
    assert r.status in (
        ToolResultStatus.ERROR_RECOVERABLE,
        ToolResultStatus.ERROR_FATAL,
    ), f"mint past cap must return an error, got {r.status}"

    reloaded = store.load()
    assert reloaded is not None
    assert "q_overflow" not in reloaded.snapshot.quest_log
    assert len(reloaded.snapshot.quest_log) == 32  # did not grow


async def test_update_at_cardinality_cap_still_allowed() -> None:
    """The cap blocks minting, never evolving — a full log can still take a
    status update on an existing quest."""
    snap = _build_snapshot(quest_log=_full_quest_log(32))
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call(
        {
            "quest_id": "q0",
            "title": "t0",
            "objective": "o0",
            "status": "resolved",
        },
        ctx,
    )
    assert r.status is ToolResultStatus.OK

    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.snapshot.quest_log["q0"].status == "resolved"
    assert len(reloaded.snapshot.quest_log) == 32


# --------------------------------------------------------------------------- #
# Schema discipline — tightly-bound args (lang-review #11 input validation)
# --------------------------------------------------------------------------- #


async def test_empty_title_rejected_by_args_model() -> None:
    store = _store_with(_build_snapshot())
    ctx = _make_ctx(store)
    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-empty-title",
            name="record_quest",
            arguments={"quest_id": "q1", "title": "", "objective": "o"},
        ),
        ctx,
    )
    assert out.is_error is True
    assert "argument validation failed" in out.content


async def test_empty_objective_rejected_by_args_model() -> None:
    store = _store_with(_build_snapshot())
    ctx = _make_ctx(store)
    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-empty-obj",
            name="record_quest",
            arguments={"quest_id": "q1", "title": "t", "objective": ""},
        ),
        ctx,
    )
    assert out.is_error is True
    assert "argument validation failed" in out.content


async def test_oversized_objective_rejected_by_args_model() -> None:
    """Per-call payload is bounded so a runaway narrator string can't be
    smuggled into one quest entry."""
    store = _store_with(_build_snapshot())
    ctx = _make_ctx(store)
    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-big-obj",
            name="record_quest",
            arguments={"quest_id": "q1", "title": "t", "objective": "o" * 5000},
        ),
        ctx,
    )
    assert out.is_error is True
    assert "argument validation failed" in out.content


async def test_no_active_session_returns_fatal_error() -> None:
    from tests.agents.tools.conftest import pg_empty_store

    ctx = _make_ctx(pg_empty_store())
    r = await _call(_MINT, ctx)
    assert r.status is ToolResultStatus.ERROR_FATAL
    assert r.message is not None
    assert "no active session" in r.message


# --------------------------------------------------------------------------- #
# AC-2 — routed state_transition reaches the GM-panel feed (end-to-end)
# --------------------------------------------------------------------------- #


async def test_quest_created_routes_to_gm_panel_feed() -> None:
    """The ``quest.created`` span must reach the watcher hub as a routed
    ``state_transition`` (component=quest_log) via SPAN_ROUTES, the same way
    SPAN_QUEST_UPDATE does — proving the GM-panel lie-detector sees the mint
    (not a source-text grep; an end-to-end routed-event assertion)."""
    from opentelemetry.sdk.trace import TracerProvider

    from sidequest.server.watcher import WatcherSpanProcessor
    from sidequest.telemetry import spans as spans_module
    from sidequest.telemetry.watcher_hub import watcher_hub

    watcher_hub.bind_loop(asyncio.get_running_loop())
    async with watcher_hub._lock:  # noqa: SLF001
        watcher_hub._subscribers.clear()  # noqa: SLF001

    captured: list[dict] = []

    class _Sock:
        async def send_json(self, data: dict) -> None:
            captured.append(data)

    await watcher_hub.subscribe(_Sock())  # type: ignore[arg-type]

    provider = TracerProvider()
    provider.add_span_processor(WatcherSpanProcessor(watcher_hub))
    local_tracer = provider.get_tracer("test-quest-created-route")

    import pytest

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(spans_module, "tracer", lambda: local_tracer)
        store = _store_with(_build_snapshot())
        ctx = _make_ctx(store)
        r = await _call(_MINT, ctx)
        assert r.status is ToolResultStatus.OK
        await asyncio.sleep(0.05)

    routed = [
        e
        for e in captured
        if e["event_type"] == "state_transition" and e["component"] == "quest_log"
    ]
    assert routed, (
        "quest.created never reached the hub as a routed state_transition — "
        "SPAN_ROUTES entry for quest.created is missing or the helper isn't "
        "opened through spans_module.tracer()"
    )
