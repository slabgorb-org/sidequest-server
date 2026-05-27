"""WIRING: a peer's (re)connect must reconcile current seal state from the
canonical room snapshot — so a dropped ACTION_REVEAL/TURN_STATUS can never
strand them at "Composing" (Story 67-2, AC2 + AC5).

This is the refactor-stable proof that the seal-reconcile is reachable from the
REAL ``connect`` handler against a REAL ``SessionRoom`` + Postgres, not just a
green unit helper (server CLAUDE.md: every test suite needs a wiring test;
prefer fixture-driven behavior + OTEL-span assertions, never source-text grep).

Repro fidelity (the 2026-05-27 beneath_sunden ping-pong):
  1. Adam + Eve are both PLAYING in one MP room.
  2. Adam seals. The seal lives in ``room.snapshot.turn_manager._submitted``
     — a *runtime-only* set (turn.py:63) carried on the CANONICAL snapshot the
     room binds via ``room.bind_world`` (connect.py:638-642), so it survives a
     peer's reconnect even though it never hits Postgres.
  3. The single ACTION_REVEAL{submitted} + TURN_STATUS{submitted} frame to Eve
     is dropped (socket churn) — she has no signal Adam sealed.
  4. Eve reconnects. Her connect must re-derive the roster and send her a
     TURN_STATUS marking Adam ``submitted`` — independent of any replayed
     ACTION_REVEAL (which is not event-sourced and never replays).

Fixtures mirror ``test_mp_auto_seat_on_connect.py`` (the established
connect-driving harness).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.persistence import GameMode
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnPhase
from sidequest.handlers import connect as connect_module
from sidequest.protocol.messages import SessionEventMessage, SessionEventPayload
from sidequest.server.session_handler import WebSocketSessionHandler
from sidequest.server.session_room import RoomRegistry

_GENRE = "space_opera"
_WORLD = "coyote_star"
_CONTENT_SEARCH_PATH = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Per-worker throwaway PG db, cleaned per test (ADR-115 D2)."""
    import psycopg

    from sidequest.game import db_pool

    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(plain, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename <> 'alembic_version'"
        ).fetchall()
        if rows:
            names = ", ".join(f'"{r[0]}"' for r in rows)
            conn.execute(f"TRUNCATE {names} RESTART IDENTITY CASCADE")
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    yield
    db_pool.close_pool()


def _seed_mp_two_players(slug: str) -> None:
    snap = GameSnapshot(genre_slug=_GENRE, world_slug=_WORLD, location="Far Landing")
    chars: list[Character] = []
    for player_id, char_name in [("adam-pid", "Adam"), ("eve-pid", "Eve")]:
        core = CreatureCore(
            name=char_name,
            description=f"Playtest character for {player_id}",
            personality="reach-tested",
            inventory=Inventory(),
        )
        chars.append(
            Character(core=core, char_class="Smuggler", race="Coreworlder", backstory="Gate")
        )
        snap.player_seats[player_id] = char_name
    snap.characters = chars

    from sidequest.game import db_pool
    from sidequest.server.session_state import _build_pg_repos_for_slug

    repo, _dungeon, _sink = _build_pg_repos_for_slug(
        db_pool.get_pool(),
        slug=slug,
        mode=str(GameMode.MULTIPLAYER),
        genre_slug=_GENRE,
        world_slug=_WORLD,
    )
    repo.save(snap)


def _handler_with_queue(
    save_dir: Path, registry: RoomRegistry, socket_id: str
) -> tuple[WebSocketSessionHandler, asyncio.Queue]:
    """Connect-driving handler sharing ``registry``; returns its out_queue so
    the test can inspect exactly what the connecting socket received."""
    out_queue: asyncio.Queue = asyncio.Queue()
    handler = WebSocketSessionHandler(
        save_dir=save_dir,
        genre_pack_search_paths=[_CONTENT_SEARCH_PATH],
    )
    handler.attach_room_context(registry=registry, socket_id=socket_id, out_queue=out_queue)
    return handler, out_queue


async def _connect(handler: WebSocketSessionHandler, player_id: str, name: str, slug: str) -> None:
    await handler.handle_message(
        SessionEventMessage(
            type="SESSION_EVENT",
            player_id=player_id,
            payload=SessionEventPayload(event="connect", game_slug=slug, player_name=name),
        )
    )


def _drain(queue: asyncio.Queue) -> list[object]:
    out: list[object] = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


def _turn_status_frames(messages: list[object]) -> list[object]:
    return [m for m in messages if str(getattr(m, "type", "")).endswith("TURN_STATUS")]


def _sealed_ids_in_frame(msg: object) -> set[str]:
    entries = getattr(getattr(msg, "payload", None), "entries", None) or []
    sealed: set[str] = set()
    for e in entries:
        pid = getattr(e, "player_id", None)
        status = getattr(e, "status", None)
        pid_str = pid.as_str() if hasattr(pid, "as_str") else str(pid)
        if status == "submitted":
            sealed.add(pid_str)
    return sealed


async def _seat_both_and_seal_adam(tmp_path: Path, slug: str) -> tuple[RoomRegistry, object]:
    """Bring Adam + Eve to PLAYING, then mark Adam sealed on the canonical
    room snapshot (his TURN_STATUS to Eve is about to be 'dropped')."""
    _seed_mp_two_players(slug)
    registry = RoomRegistry()

    adam_h, _ = _handler_with_queue(tmp_path, registry, "sock-adam")
    await _connect(adam_h, "adam-pid", "Adam", slug)
    eve_h1, _ = _handler_with_queue(tmp_path, registry, "sock-eve-1")
    await _connect(eve_h1, "eve-pid", "Eve", slug)

    room = next(iter(registry._rooms.values()))
    snapshot = room.snapshot
    snapshot.turn_manager.phase = TurnPhase.InputCollection
    snapshot.turn_manager.player_count = 2
    object.__getattribute__(snapshot.turn_manager, "_submitted").add("adam-pid")
    return registry, room


@pytest.mark.asyncio
async def test_reconnect_reconciles_sealed_peer_into_turn_status(tmp_path: Path) -> None:
    """AC2: Eve reconnects after Adam sealed (and her seal frame dropped). Her
    reconnect out_queue must carry a TURN_STATUS marking Adam ``submitted`` —
    derived from the canonical roster, not a replayed ACTION_REVEAL."""
    slug = "67-2-reconnect-reconcile"
    registry, _room = await _seat_both_and_seal_adam(tmp_path, slug)

    # Eve's socket churned; she reconnects on a fresh socket + queue.
    eve_h2, eve_q = _handler_with_queue(tmp_path, registry, "sock-eve-2")
    await _connect(eve_h2, "eve-pid", "Eve", slug)

    frames = _turn_status_frames(_drain(eve_q))
    assert frames, (
        "Eve's reconnect must send her at least one TURN_STATUS carrying the "
        "reconciled seal roster — without it a dropped seal frame strands her "
        "at 'Composing' forever"
    )
    assert any("adam-pid" in _sealed_ids_in_frame(f) for f in frames), (
        "the reconciled TURN_STATUS must mark Adam 'submitted' so Eve's tab "
        "flips him to '✓ Sealed' on reconnect"
    )


@pytest.mark.asyncio
async def test_reconnect_seal_reconcile_emits_watcher_event(tmp_path: Path) -> None:
    """AC5 / OTEL lie-detector: the connect-time reconcile must emit a watcher
    event so the GM panel can confirm the recovery engaged. Proposed event
    name ``turn_status.reconciled_on_connect`` (TDD contract — adjust the
    literal here if Dev/SM pick a different name, but the seam MUST emit)."""
    slug = "67-2-reconcile-watcher"
    published: list[tuple[str, dict, dict]] = []

    def _record(event_type, fields, **kwargs):
        published.append((event_type, dict(fields), dict(kwargs)))

    registry, _room = await _seat_both_and_seal_adam(tmp_path, slug)

    import pytest as _pytest  # local monkeypatch ctx for an async test

    mp = _pytest.MonkeyPatch()
    mp.setattr(connect_module, "_watcher_publish", _record)
    try:
        eve_h2, _eve_q = _handler_with_queue(tmp_path, registry, "sock-eve-2")
        await _connect(eve_h2, "eve-pid", "Eve", slug)
    finally:
        mp.undo()

    reconcile_events = [
        (et, f, k)
        for (et, f, k) in published
        if f.get("field") == "turn_status.reconciled_on_connect"
        or f.get("event") == "turn_status.reconciled_on_connect"
        or "reconcile" in str(et).lower()
    ]
    assert reconcile_events, (
        "the connect-time seal reconcile must publish a watcher event "
        "(proposed: turn_status.reconciled_on_connect) — an un-instrumented "
        "recovery seam is invisible to the GM panel (CLAUDE.md OTEL principle)"
    )
