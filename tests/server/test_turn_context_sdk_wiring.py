"""Interrupt fix — plumb world_id/session_id/store/lore_store into the SDK
TurnContext so the narrator's ``query_lore`` / ``lookup_monster`` tools
actually see world lore + the monster manual.

Diagnosed bug (Jaeger + ``narrator.sdk_path.context_missing_ids`` ~103×/
session): ``TurnContext`` carried no ``world_id``/``session_id``/``store``/
``lore_store`` fields, so ``_run_narration_turn_sdk`` degraded every turn to
``world_id=unknown session_id=adhoc turn_number=0`` and built a
``ToolContext`` with ``lore_store=None`` — ``query_lore`` returned
``hit_count=0`` and the narrator confabulated canon.

The code itself documented this as deferred: ``ToolContext.lore_store``'s
docstring says *"Phase E wires this at the production call site"*. This test
proves Phase E is now wired:

  * unit — ``_build_turn_context`` populates the four ids + ``turn_number``
    from ``_SessionData``;
  * wiring — the production ``_run_narration_turn_sdk`` path constructs a
    ``ToolContext`` with the real ids AND a non-None ``lore_store`` (and the
    ``monster_manual``), i.e. the change is reachable from
    ``run_narration_turn``, not just present on the dataclass;
  * regression — the ``context_missing_ids`` warning does NOT fire when ids
    are present.

The fake-SDK shape mirrors ``test_narrator_sdk_hybrid_split.py`` exactly.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# Importing the tools package wires the 26 adapters onto default_registry.
import sidequest.agents.tools  # noqa: F401
from sidequest.agents.anthropic_sdk_client import AnthropicSdkClient
from sidequest.agents.orchestrator import Orchestrator, TurnContext
from sidequest.agents.tool_registry import ToolContext, default_registry
from sidequest.agents.tooling_protocol import ToolResultBlock, ToolUseBlock
from sidequest.game.lore_store import LoreFragment, LoreStore
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.loader import load_genre_pack
from sidequest.server.session_handler import _build_turn_context, _SessionData
from tests._helpers.session_room import room_for

CONTENT_GENRE_PACKS = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


# ---------------------------------------------------------------------------
# _SessionData fixture (mirrors test_session_helpers_narrative_strip.py)
# ---------------------------------------------------------------------------


def _seeded_lore_store() -> LoreStore:
    store = LoreStore()
    store.add(
        LoreFragment.new(
            id="lore-1",
            category="setting",
            content="The Mawdeep is a flooded ossuary beneath the city.",
            source="seed",
        )
    )
    store.add(
        LoreFragment.new(
            id="lore-2",
            category="faction",
            content="The Drowned Wardens guard the lower vaults.",
            source="seed",
        )
    )
    return store


def _make_snapshot() -> GameSnapshot:
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=7),
    )
    return snap


def _build_sd(*, with_monster_manual: bool = True) -> _SessionData:
    snap = _make_snapshot()
    pack = load_genre_pack(CONTENT_GENRE_PACKS / snap.genre_slug)
    sd = _SessionData(
        genre_slug=snap.genre_slug,
        world_slug=snap.world_slug,
        player_name="Alice",
        player_id="player:alice",
        snapshot=snap,
        store=MagicMock(),
        genre_pack=pack,
        orchestrator=MagicMock(),
    )
    sd.store.recent_narrative.return_value = []
    sd.game_slug = "2026-05-14-caverns_mawdeep-28"
    sd.lore_store = _seeded_lore_store()
    if with_monster_manual:
        sd.monster_manual = MagicMock(name="MonsterManual")
    sd._room = room_for(snap, slug="mawdeep")
    return sd


# ---------------------------------------------------------------------------
# 1. Unit — _build_turn_context populates the ids + turn_number + stores
# ---------------------------------------------------------------------------


def test_build_turn_context_populates_world_session_store_lore() -> None:
    """``_build_turn_context`` MUST copy ``world_slug``/``game_slug``/
    ``store``/``lore_store`` (and the per-turn ``interaction`` count) off
    ``_SessionData`` onto the TurnContext. Without this the SDK path
    degrades to unknown/adhoc/None/0 (the diagnosed bug)."""
    sd = _build_sd()

    ctx = _build_turn_context(sd, room=sd._room)

    assert ctx.world_id == "mawdeep", (
        f"world_id not plumbed from sd.world_slug; got {ctx.world_id!r}"
    )
    assert ctx.session_id == "2026-05-14-caverns_mawdeep-28", (
        f"session_id not plumbed from sd.game_slug; got {ctx.session_id!r}"
    )
    assert ctx.store is sd.store, "store reference not plumbed from sd.store"
    assert ctx.lore_store is sd.lore_store, (
        "lore_store reference not plumbed from sd.lore_store — query_lore "
        "would see no world lore (hit_count=0) and the narrator confabulates"
    )
    assert len(ctx.lore_store) == 2
    # turn_number must track snapshot.turn_manager.interaction (Jaeger
    # currently shows this stuck at 0).
    assert ctx.turn_number == 7, (
        f"turn_number not sourced from snapshot.turn_manager.interaction; "
        f"got {ctx.turn_number!r} (Jaeger bug: stuck at 0)"
    )
    # MonsterManual rides the same Phase-E seam as lore_store.
    assert ctx.monster_manual is sd.monster_manual


def test_build_turn_context_monster_manual_none_when_unbound() -> None:
    """A session whose genre never loaded a manual leaves
    ``monster_manual=None`` — no synthetic placeholder (No Stubbing)."""
    sd = _build_sd(with_monster_manual=False)
    ctx = _build_turn_context(sd, room=sd._room)
    assert ctx.monster_manual is None


# ---------------------------------------------------------------------------
# Fake SDK — mirrors test_narrator_sdk_hybrid_split.py
# ---------------------------------------------------------------------------


@dataclass
class _Usage:
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class _TextBlock:
    type: str
    text: str


@dataclass
class _Resp:
    content: list[Any]
    stop_reason: str
    usage: _Usage
    model: str


class _Msgs:
    def __init__(self, responses: list[_Resp]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _Resp:
        self.calls.append(kwargs)
        return self._responses.pop(0)


class _Sdk:
    def __init__(self, responses: list[_Resp]) -> None:
        self.messages = _Msgs(responses)


class _FakeRegistry:
    def compose_split(self, agent_name: str) -> tuple[str, str]:
        return ("system text", "user text")

    def compose_split_by_zone(self, agent_name: str):
        from sidequest.agents.prompt_framework.types import AttentionZone

        return ({AttentionZone.Primacy: "system text"}, "user text")

    def registry(self, agent_name: str) -> list:
        # Story 60-2: the SDK path emits an enriched prompt_assembled post-call
        # that reads registry sections; this stub registers none.
        return []


def _single_turn_sdk(prose: str) -> _Sdk:
    return _Sdk(
        responses=[
            _Resp(
                content=[_TextBlock(type="text", text=prose)],
                stop_reason="end_turn",
                usage=_Usage(input_tokens=120, output_tokens=20),
                model="claude-sonnet-4-6",
            ),
        ]
    )


async def _run_sdk_and_capture_ctx(
    monkeypatch: pytest.MonkeyPatch,
    context: TurnContext,
) -> ToolContext:
    """Drive ``run_narration_turn`` through the SDK path with a no-tool
    response and return the ``ToolContext`` the production code built."""
    monkeypatch.delenv("SIDEQUEST_NARRATOR_STREAMING", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    captured: dict[str, ToolContext] = {}

    async def _spy_dispatch(block: ToolUseBlock, ctx: ToolContext) -> ToolResultBlock:
        captured["ctx"] = ctx
        return ToolResultBlock(tool_use_id=block.id, content="ok", is_error=False)

    # complete_with_tools always invokes tool_dispatch via the registry
    # surface; a one-tool round lets us capture the ctx the production code
    # constructed. Use a tool_use round so dispatch fires.
    sdk = _Sdk(
        responses=[
            _Resp(
                content=[
                    type(
                        "TU",
                        (),
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "query_lore",
                            "input": {"query": "the wardens"},
                        },
                    )()
                ],
                stop_reason="tool_use",
                usage=_Usage(input_tokens=120, output_tokens=10),
                model="claude-sonnet-4-6",
            ),
            _Resp(
                content=[_TextBlock(type="text", text="The water rises.")],
                stop_reason="end_turn",
                usage=_Usage(input_tokens=130, output_tokens=14),
                model="claude-sonnet-4-6",
            ),
        ]
    )
    client = AnthropicSdkClient(sdk=sdk)
    orch = Orchestrator(client=client)

    monkeypatch.setattr(default_registry, "dispatch", _spy_dispatch)

    async def _fake_build_prompt(
        self: Orchestrator, action: str, ctx: TurnContext
    ) -> tuple[str, _FakeRegistry]:
        return ("prompt-text", _FakeRegistry())

    monkeypatch.setattr(Orchestrator, "build_narrator_prompt", _fake_build_prompt)

    await orch.run_narration_turn("look around", context)
    assert "ctx" in captured, "tool_dispatch never fired — cannot assert ToolContext wiring"
    return captured["ctx"]


# ---------------------------------------------------------------------------
# 2. Wiring — the production SDK path builds a real, lore-bearing ToolContext
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sdk_path_builds_toolcontext_with_real_ids_and_lore_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The "every test suite needs a wiring test" gate: prove
    ``_run_narration_turn_sdk`` passes the real ``world_id``/``session_id``/
    ``turn_number``/``store``/``lore_store``/``monster_manual`` through to
    the ``ToolContext`` the registry dispatches with. ``lore_store`` being
    non-None is THE fix for ``query_lore`` hit_count=0."""
    lore = _seeded_lore_store()
    manual = MagicMock(name="MonsterManual")
    store = MagicMock(name="SqliteStore")
    ctx = TurnContext(
        character_name="Alice",
        genre="caverns_and_claudes",
        world_id="mawdeep",
        session_id="2026-05-14-caverns_mawdeep-28",
        turn_number=7,
        store=store,
        lore_store=lore,
        monster_manual=manual,
    )

    tool_ctx = await _run_sdk_and_capture_ctx(monkeypatch, ctx)

    assert tool_ctx.world_id == "mawdeep"
    assert tool_ctx.session_id == "2026-05-14-caverns_mawdeep-28"
    assert tool_ctx.turn_number == 7
    assert tool_ctx.store is store
    assert tool_ctx.lore_store is lore, (
        "ToolContext.lore_store is not the wired LoreStore — query_lore "
        "would return hit_count=0 and the narrator confabulates canon"
    )
    assert len(tool_ctx.lore_store) == 2
    assert tool_ctx.monster_manual is manual


@pytest.mark.asyncio
async def test_sdk_path_no_context_missing_ids_warning_when_ids_present(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression: with ids present, the fail-loud
    ``narrator.sdk_path.context_missing_ids`` warning MUST NOT fire (it was
    firing ~103×/session pre-fix)."""
    ctx = TurnContext(
        character_name="Alice",
        world_id="mawdeep",
        session_id="2026-05-14-caverns_mawdeep-28",
        turn_number=7,
        store=MagicMock(),
        lore_store=_seeded_lore_store(),
    )
    with caplog.at_level(logging.WARNING):
        await _run_sdk_and_capture_ctx(monkeypatch, ctx)

    assert not any("context_missing_ids" in rec.message for rec in caplog.records), (
        "context_missing_ids warning fired even though world_id/session_id were present"
    )


@pytest.mark.asyncio
async def test_sdk_path_context_missing_ids_still_fires_when_unwired(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fail-loud guard preserved (No Silent Fallbacks): a genuinely
    unwired TurnContext (no ids) MUST still emit the anomaly warning so a
    regression in ``_build_turn_context`` is visible in the GM panel."""
    ctx = TurnContext(character_name="Alice", turn_number=0)
    with caplog.at_level(logging.WARNING):
        await _run_sdk_and_capture_ctx(monkeypatch, ctx)

    assert any("context_missing_ids" in rec.message for rec in caplog.records), (
        "context_missing_ids warning did NOT fire for a genuinely unwired "
        "TurnContext — the fail-loud guard was lost"
    )


@pytest.mark.asyncio
async def test_sdk_path_context_missing_lore_store_fires_when_ids_present_but_lore_unwired(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Story 61-8 §A — defense-in-depth on the Phase-E lore_store seam.

    A regression in ``_build_turn_context`` that drops
    ``lore_store=sd.lore_store`` would slip past the umbrella
    ``context_missing_ids`` guard because world_id/session_id are still
    populated. The dedicated ``context_missing_lore_store`` warning
    catches this partial-wiring failure mode — the same one that
    originally produced the 61-1 ``hit_count=0`` confabulation bug.
    """
    ctx = TurnContext(
        character_name="Alice",
        world_id="mawdeep",
        session_id="2026-05-23-caverns_mawdeep-1",
        turn_number=3,
        store=MagicMock(),
        lore_store=None,
    )
    with caplog.at_level(logging.WARNING):
        await _run_sdk_and_capture_ctx(monkeypatch, ctx)

    assert any("context_missing_lore_store" in rec.message for rec in caplog.records), (
        "context_missing_lore_store warning did NOT fire even though "
        "lore_store=None with present ids — the defense-in-depth guard "
        "must catch a partial-wiring regression that the umbrella "
        "context_missing_ids check would miss."
    )
    # The umbrella warning MUST NOT fire (ids ARE present); this is the
    # whole point of separating the two warnings — distinct GM-panel signals.
    assert not any("context_missing_ids" in rec.message for rec in caplog.records), (
        "context_missing_ids fired despite ids being present — that "
        "warning is reserved for the umbrella unwired-TurnContext case."
    )


@pytest.mark.asyncio
async def test_sdk_path_context_missing_lore_store_silent_when_fully_wired(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression guard: a fully-wired TurnContext (ids + lore_store)
    MUST NOT fire either defense-in-depth warning. Both guards together
    must stay quiet on the green path or they devolve into noise."""
    ctx = TurnContext(
        character_name="Alice",
        world_id="mawdeep",
        session_id="2026-05-23-caverns_mawdeep-1",
        turn_number=3,
        store=MagicMock(),
        lore_store=_seeded_lore_store(),
    )
    with caplog.at_level(logging.WARNING):
        await _run_sdk_and_capture_ctx(monkeypatch, ctx)

    assert not any("context_missing_lore_store" in rec.message for rec in caplog.records), (
        "lore_store warning fired despite lore_store being wired"
    )
    assert not any("context_missing_ids" in rec.message for rec in caplog.records), (
        "umbrella context_missing_ids warning fired on the green path"
    )


@pytest.mark.asyncio
async def test_sdk_path_unwired_ids_fire_only_umbrella_not_lore_store_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Story 61-8 §A (review-fix round 2) — boundary case: when the
    TurnContext is genuinely unwired (world_id/session_id missing AND
    lore_store missing), ONLY the umbrella ``context_missing_ids``
    warning must fire — NOT the dedicated ``context_missing_lore_store``
    warning. The lore_store warning's exclusion condition
    ``world_id != "unknown" and session_id != "adhoc"`` is the
    architectural pivot that keeps the two GM-panel signals distinct.
    Without this test, a regression that drops the exclusion would
    produce duplicate noise on every cold-start turn — the failure
    mode the round-1 review flagged.
    """
    ctx = TurnContext(character_name="Alice", turn_number=0, lore_store=None)
    with caplog.at_level(logging.WARNING):
        await _run_sdk_and_capture_ctx(monkeypatch, ctx)

    assert any("context_missing_ids" in rec.message for rec in caplog.records), (
        "umbrella context_missing_ids warning did NOT fire on a "
        "genuinely-unwired TurnContext — the fail-loud guard was lost"
    )
    assert not any("context_missing_lore_store" in rec.message for rec in caplog.records), (
        "context_missing_lore_store fired despite ids being unwired — "
        "the two warnings must be mutually exclusive; the exclusion "
        "condition (world_id != 'unknown' and session_id != 'adhoc') "
        "in orchestrator.py:3568 was bypassed."
    )


class _FakeSocket:
    """Minimal `_Sendable` for watcher_hub subscription — same shape as
    `tests/agents/test_61_3_hard_cap_oversized_canary.py::_FakeSocket`."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any]) -> None:
        self.events.append(data)


@pytest.mark.asyncio
async def test_sdk_path_lore_store_warning_publishes_watcher_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Story 61-8 §A (review-fix round 2) — the warning is not just a
    logger.warning, it MUST publish a watcher event so the GM panel
    surfaces the partial-wiring regression (CLAUDE.md OTEL
    Observability Principle). The logger.warning is invisible to the
    GM panel; the watcher event is the GM-panel-actionable signal."""
    from sidequest.telemetry.watcher_hub import watcher_hub

    watcher_hub.bind_loop(asyncio.get_running_loop())
    async with watcher_hub._lock:  # noqa: SLF001
        watcher_hub._subscribers.clear()  # noqa: SLF001

    sock = _FakeSocket()
    await watcher_hub.subscribe(sock)  # type: ignore[arg-type]
    try:
        ctx = TurnContext(
            character_name="Alice",
            world_id="mawdeep",
            session_id="2026-05-23-caverns_mawdeep-1",
            turn_number=3,
            store=MagicMock(),
            lore_store=None,
        )
        await _run_sdk_and_capture_ctx(monkeypatch, ctx)
        # Give the publish a moment to fan out via async queue.
        await asyncio.sleep(0.05)
    finally:
        await watcher_hub.unsubscribe(sock)  # type: ignore[arg-type]

    lore_events = [
        e for e in sock.events if e.get("event_type") == "narrator_context_missing_lore_store"
    ]
    assert lore_events, (
        "narrator_context_missing_lore_store watcher event was NOT "
        "published — only logger.warning fired, which is invisible to "
        "the GM panel. CLAUDE.md OTEL Observability Principle requires "
        "the watcher event for GM-panel-actionable subsystem decisions."
    )
