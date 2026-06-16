"""WIRING (Story 97-2): the first reconnector after a server reload must
reconcile against the DURABLE seated-PC roster, so it is told the true table
size (0/2) — never the solo 0/1 the live-socket roster produces.

Measured 3x in server log ``.20260607-090551`` (lines 652/668, 863/878): after
a reload both seats reconnect; the FIRST reconnector receives
``turn_status.reconciled_on_connect sealed=0/1`` because
``build_seal_reconcile_roster`` is fed ``room.playing_player_ids()`` and at that
instant the first reconnector is the only live PLAYING socket in the
freshly-rebuilt room. The second reconnector then sees 0/2. A first reconnector
told 0/1 believes nobody is waiting on anyone — plausibly the seat-2 "Enter
enabled / no waiting lock" MP-desync symptom that filed this story (#740).

This is the refactor-stable, call-site-agnostic proof: it asserts what the
connecting socket actually RECEIVES and what the GM-panel watcher event reports,
regardless of whether the durable-seat fix lands inside
``build_seal_reconcile_roster`` or at its connect call site.

Fixtures mirror ``test_seal_reconcile_on_connect.py`` (the established
connect-driving harness). A fresh ``RoomRegistry`` with the snapshot seeded only
in Postgres is the post-reload state: the room is rebuilt from the durable
snapshot as each socket reconnects.
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


def _seed_seats(slug: str, seats: list[tuple[str, str]]) -> None:
    """Persist a MULTIPLAYER snapshot whose ``player_seats`` is the durable
    seated-PC roster — the exact state a reload restores from Postgres."""
    snap = GameSnapshot(genre_slug=_GENRE, world_slug=_WORLD, location="Far Landing")
    chars: list[Character] = []
    for player_id, char_name in seats:
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


def _handler(save_dir: Path, registry: RoomRegistry, socket_id: str) -> WebSocketSessionHandler:
    handler = WebSocketSessionHandler(
        save_dir=save_dir,
        genre_pack_search_paths=[_CONTENT_SEARCH_PATH],
    )
    handler.attach_room_context(
        registry=registry, socket_id=socket_id, out_queue=asyncio.Queue()
    )
    return handler


async def _connect(
    handler: WebSocketSessionHandler, player_id: str, name: str, slug: str
) -> list[object]:
    return await handler.handle_message(
        SessionEventMessage(
            type="SESSION_EVENT",
            player_id=player_id,
            payload=SessionEventPayload(event="connect", game_slug=slug, player_name=name),
        )
    )


def _turn_status_frames(messages: list[object]) -> list[object]:
    return [m for m in messages if str(getattr(m, "type", "")).endswith("TURN_STATUS")]


def _roster_ids(msg: object) -> set[str]:
    entries = getattr(getattr(msg, "payload", None), "entries", None) or []
    out: set[str] = set()
    for e in entries:
        pid = getattr(e, "player_id", None)
        out.add(pid.as_str() if hasattr(pid, "as_str") else str(pid))
    return out


# ---------------------------------------------------------------------------
# AC1 — first reconnector after a reload sees the full durable roster (0/2).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_reconnector_after_reload_reconciles_full_durable_roster(
    tmp_path: Path,
) -> None:
    """Both Adam and Eve are durably seated in Postgres. A reload wiped the
    in-memory room; Adam reconnects FIRST. His socket is the only live PLAYING
    peer, so ``room.playing_player_ids()`` is ``[adam]`` — but his
    reconciled TURN_STATUS must carry BOTH seats (denominator 2), telling him
    Eve is still out there rather than that he is solo."""
    slug = "97-2-first-reconnector"
    _seed_seats(slug, [("adam-pid", "Adam"), ("eve-pid", "Eve")])

    registry = RoomRegistry()  # fresh = post-reload, room rebuilt from Postgres
    adam = _handler(tmp_path, registry, "sock-adam-reconnect")
    outbound = await _connect(adam, "adam-pid", "Adam", slug)

    frames = _turn_status_frames(outbound)
    assert frames, "first reconnector must receive a reconcile TURN_STATUS frame"
    rosters = [_roster_ids(f) for f in frames]
    assert any(r == {"adam-pid", "eve-pid"} for r in rosters), (
        "the first reconnector's reconcile roster must contain BOTH durably "
        f"seated PCs (denominator 2), not the live-socket solo roster — saw {rosters}"
    )
    assert all("eve-pid" in r for r in rosters if r), (
        "no reconcile frame may report the solo 0/1 roster — Eve is durably "
        "seated and must appear even before her socket reconnects"
    )


@pytest.mark.asyncio
async def test_first_reconnector_watcher_reports_durable_roster_size(
    tmp_path: Path,
) -> None:
    """OTEL lie-detector (CLAUDE.md): the reconcile watcher event must report
    ``roster_size=2`` for the first reconnector of a 2-seat table — and name
    which roster source produced the count (context Scope: 'the reconcile
    span/log should name which roster source produced the count, so the next
    desync forensics can tell live-socket from durable-seat derivation'). The
    ``roster_source`` field is a TDD contract — Dev may rename the literal, but
    the event MUST disclose that the count came from the durable seats."""
    slug = "97-2-first-reconnector-watcher"
    _seed_seats(slug, [("adam-pid", "Adam"), ("eve-pid", "Eve")])

    published: list[tuple[str, dict]] = []

    def _record(event_type, fields, **kwargs):
        published.append((event_type, dict(fields)))

    mp = pytest.MonkeyPatch()
    mp.setattr(connect_module, "_watcher_publish", _record)
    try:
        registry = RoomRegistry()
        adam = _handler(tmp_path, registry, "sock-adam-reconnect")
        await _connect(adam, "adam-pid", "Adam", slug)
    finally:
        mp.undo()

    reconcile = [
        f for (_et, f) in published if f.get("field") == "turn_status.reconciled_on_connect"
    ]
    assert reconcile, "the reconcile must emit its watcher event for the first reconnector"
    assert any(f.get("roster_size") == 2 for f in reconcile), (
        "watcher must report roster_size=2 for the first reconnector of a "
        f"2-seat table, not the solo 1 — saw {[f.get('roster_size') for f in reconcile]}"
    )
    assert any(f.get("roster_source") for f in reconcile), (
        "the reconcile event must name which roster source produced the count "
        "(durable seats vs live sockets) so desync forensics can tell them "
        "apart — context Scope, CLAUDE.md OTEL principle"
    )


# ---------------------------------------------------------------------------
# 45-2 regression (load-bearing negative) — a mid-chargen phantom peer holds a
# CHARGEN seat in the live room but is NOT in the durable ``player_seats``. The
# durable-seat denominator must exclude it; the table never waits on someone
# still rolling a character. This guard fails iff the fix over-reaches to
# ``seated_player_ids()`` (which includes CHARGEN) instead of the committed
# durable seats.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phantom_chargen_peer_excluded_from_reconnect_roster(tmp_path: Path) -> None:
    """Only Adam is durably seated. Eve is mid-chargen — she holds a CHARGEN
    seat in the live room but has not committed, so she is absent from the
    durable ``player_seats``. Adam's reconnect reconcile must count ONLY Adam
    (1), never the phantom Eve (the 45-2 guard: ``playing_*`` exists precisely
    so a chargen phantom cannot inflate the barrier roster)."""
    slug = "97-2-phantom-guard"
    _seed_seats(slug, [("adam-pid", "Adam")])

    registry = RoomRegistry()
    adam1 = _handler(tmp_path, registry, "sock-adam-1")
    await _connect(adam1, "adam-pid", "Adam", slug)

    # Eve claims a seat but never commits chargen — a CHARGEN-state phantom in
    # the live room, deliberately NOT written to the durable player_seats.
    room = next(iter(registry._rooms.values()))
    room.seat("eve-pid", character_slot=None)
    assert "eve-pid" not in room.snapshot.player_seats, (
        "precondition: the phantom is a CHARGEN seat with no committed durable seat"
    )
    room.snapshot.turn_manager.phase = TurnPhase.InputCollection

    # Adam reconnects on a fresh socket; the phantom now exists in the room.
    adam2 = _handler(tmp_path, registry, "sock-adam-2")
    outbound = await _connect(adam2, "adam-pid", "Adam", slug)

    for frame in _turn_status_frames(outbound):
        ids = _roster_ids(frame)
        assert "eve-pid" not in ids, (
            "a mid-chargen phantom (CHARGEN seat, absent from durable "
            f"player_seats) must never enter the reconcile roster — saw {ids}"
        )
        assert ids in ({"adam-pid"}, set()), f"reconcile must count only committed seats, saw {ids}"
