"""Wiring test — `_dispatch_player_action` emits a per-turn watcher→OTLP
bridge diagnostic and force-flushes the tracer provider.

Closes the "is the bridge live during gameplay?" question raised in the
2026-04-30 Parsley/Sage playtest, where Jaeger only showed
``watcher.state_transition`` / ``watcher.turn_complete`` spans timestamped
at session resume — never during gameplay turns. The diagnostic adds a
hard, grep-able truth-value (``turn.bridge_diagnostic minted=N``) plus a
post-turn force_flush so that even a generous BatchSpanProcessor delay
doesn't hide turn spans from a live Jaeger session.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.persistence import (
    GameMode,
    SqliteStore,
    db_path_for_slug,
    upsert_game,
)
from sidequest.game.session import GameSnapshot
from sidequest.protocol import GameMessage
from sidequest.server.session_handler import WebSocketSessionHandler
from sidequest.server.session_room import RoomRegistry

_GENRE = "test_genre"
_WORLD = "flickering_reach"
_SLUG = "bridge-diagnostic-fixture"
_FIXTURE_PACKS = Path(__file__).resolve().parents[1] / "fixtures" / "packs"


def _seed_with_character(tmp_path: Path, slug: str) -> None:
    db = db_path_for_slug(tmp_path, slug)
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(db)
    store.initialize()
    upsert_game(
        store,
        slug=slug,
        mode=GameMode.SOLO,
        genre_slug=_GENRE,
        world_slug=_WORLD,
    )
    core = CreatureCore(
        name="Thorn",
        description="A wandering fighter",
        personality="Grim",
        inventory=Inventory(),
    )
    char = Character(
        core=core,
        char_class="Fighter",
        race="Human",
        backstory="A wanderer.",
    )
    snap = GameSnapshot(genre_slug=_GENRE, world_slug=_WORLD)
    snap.characters = [char]
    store.init_session(_GENRE, _WORLD)
    store.save(snap)
    store.close()


def _fake_narration_result():
    from sidequest.agents.orchestrator import NarrationTurnResult

    return NarrationTurnResult(
        narration="The dungeon echoes with your footsteps.",
        location=None,
        quest_updates={},
        lore_established=[],
        npcs_present=[],
        is_degraded=False,
        agent_duration_ms=42,
    )


@pytest.mark.asyncio
async def test_dispatch_logs_bridge_diagnostic_with_minted_count(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PLAYER_ACTION with the bridge flag set produces a
    ``turn.bridge_diagnostic`` INFO with ``minted>0`` — proof the bridge
    fired during gameplay (not just resume)."""
    monkeypatch.setenv("SIDEQUEST_WATCHER_AS_SPANS", "1")
    _seed_with_character(tmp_path, _SLUG)
    registry = RoomRegistry()
    handler = WebSocketSessionHandler(
        save_dir=tmp_path,
        genre_pack_search_paths=[_FIXTURE_PACKS],
    )
    queue: asyncio.Queue[object] = asyncio.Queue()
    handler.attach_room_context(
        registry=registry,
        socket_id="sock-thorn",
        out_queue=queue,
    )

    connect = GameMessage.model_validate(
        {
            "type": "SESSION_EVENT",
            "player_id": "thorn",
            "payload": {
                "event": "connect",
                "game_slug": _SLUG,
                "last_seen_seq": 0,
            },
        }
    )
    action = GameMessage.model_validate(
        {
            "type": "PLAYER_ACTION",
            "player_id": "thorn",
            "payload": {"action": "I look around."},
        }
    )

    with (
        caplog.at_level(
            logging.INFO,
            logger="sidequest.server.websocket_session_handler",
        ),
        patch(
            "sidequest.agents.orchestrator.Orchestrator.run_narration_turn",
            new=AsyncMock(return_value=_fake_narration_result()),
        ),
    ):
        await handler.handle_message(connect)
        await handler.handle_message(action)

    diagnostic_records = [r for r in caplog.records if "turn.bridge_diagnostic" in r.getMessage()]
    assert diagnostic_records, (
        "expected at least one turn.bridge_diagnostic INFO line — "
        "the per-turn bridge probe never fired"
    )
    msg = diagnostic_records[-1].getMessage()
    # The dispatch handler emits state_transition / turn_complete /
    # game_state_snapshot during a turn, so the minted delta MUST be > 0
    # when the bridge flag is set. A 0 here means the bridge silently
    # broke between publish_event and _emit_watcher_span.
    assert "minted=0" not in msg, (
        f"bridge minted=0 with flag set — bridge isn't firing during gameplay: {msg}"
    )


@pytest.mark.asyncio
async def test_dispatch_force_flushes_tracer_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-turn force_flush guarantees Jaeger sees turn spans within
    ~200 ms instead of waiting for the BatchSpanProcessor's default
    schedule. Verified by patching the tracer provider — the dispatch
    handler must call ``force_flush`` on its way out of every turn."""
    monkeypatch.setenv("SIDEQUEST_WATCHER_AS_SPANS", "1")
    _seed_with_character(tmp_path, _SLUG)
    registry = RoomRegistry()
    handler = WebSocketSessionHandler(
        save_dir=tmp_path,
        genre_pack_search_paths=[_FIXTURE_PACKS],
    )
    queue: asyncio.Queue[object] = asyncio.Queue()
    handler.attach_room_context(
        registry=registry,
        socket_id="sock-thorn",
        out_queue=queue,
    )

    connect = GameMessage.model_validate(
        {
            "type": "SESSION_EVENT",
            "player_id": "thorn",
            "payload": {
                "event": "connect",
                "game_slug": _SLUG,
                "last_seen_seq": 0,
            },
        }
    )
    action = GameMessage.model_validate(
        {
            "type": "PLAYER_ACTION",
            "player_id": "thorn",
            "payload": {"action": "I look around."},
        }
    )

    fake_provider = MagicMock()
    fake_provider.force_flush = MagicMock()

    with (
        patch(
            "sidequest.agents.orchestrator.Orchestrator.run_narration_turn",
            new=AsyncMock(return_value=_fake_narration_result()),
        ),
        patch(
            "sidequest.server.websocket_session_handler.trace.get_tracer_provider",
            return_value=fake_provider,
        ),
    ):
        await handler.handle_message(connect)
        await handler.handle_message(action)

    assert fake_provider.force_flush.call_count >= 1, (
        "dispatch handler must call provider.force_flush at turn end so "
        "Jaeger sees turn spans within the ~200 ms window, not the 2 s "
        "BatchSpanProcessor schedule"
    )
    # Verify the timeout is the agreed budget — slow flush would block the
    # turn return path; we trade Jaeger latency for hot-path safety.
    flush_kwargs = fake_provider.force_flush.call_args
    timeout = flush_kwargs.kwargs.get("timeout_millis")
    assert timeout is not None and timeout <= 500, (
        f"force_flush timeout must stay <=500 ms; got {timeout}"
    )
