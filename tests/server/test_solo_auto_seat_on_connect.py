"""Regression tests for playtest 2026-04-30 — notorious_party_gate
``player_count=0`` race at solo session boot.

Pre-fix flow:
1. Solo session connects via slug-connect.
2. ``room.connect(player_id, ...)`` is called — adds to ``_connected``
   (transport-level) but does NOT add to ``_seated`` (lobby-level).
3. The UI never sends PLAYER_SEAT for solo (PLAYER_SEAT is the MP
   slot-claim message), so ``_handle_player_seat`` never runs and
   ``room._seated`` stays empty for the entire session.
4. On turn 1, ``_build_turn_context`` reads
   ``room.non_abandoned_player_count()`` → 0 → ``orchestrator.notorious_
   party_gate`` warns ``player_count=0 (<= 0) — impossible state,
   defaulting to safe-empty``. Party context unavailable for narrator.

The lobby state machine (CONNECTED → CHARGEN → PLAYING → ABANDONED) is
mode-agnostic: solo IS in the lobby, the difference is only that the
UI doesn't send the explicit slot-claim message. The fix mirrors the
mode in the server.

Fix:
- Solo connect auto-seats the player in the room. Idempotent — already-
  seated players (reconnect, test fixtures) skip the seat() call.
- Returning solo (has_character=True) immediately transitions
  CHARGEN → PLAYING. Mirrors the ``_handle_player_seat`` returning-
  player path. New solo (state=Creating) is promoted later by the
  chargen-complete flow's existing ``transition_to_playing`` call.
- NEW MP connects (has_character=False) do NOT auto-seat — they
  still require the explicit PLAYER_SEAT message (preserves the
  lobby-claim flow). Returning MP (has_character=True) auto-seats
  AND transitions to PLAYING — see test_mp_auto_seat_on_connect.py
  for the playtest 2026-04-30 MP regression that motivated extending
  the gate.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.persistence import GameMode
from sidequest.game.session import GameSnapshot
from sidequest.protocol.messages import (
    SessionEventMessage,
    SessionEventPayload,
)
from sidequest.server.session_handler import WebSocketSessionHandler
from sidequest.server.session_room import LobbyState, RoomRegistry

_GENRE = "caverns_and_claudes"
_WORLD = "grimvault"
_CONTENT_SEARCH_PATH = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Bind the process pool to a per-worker throwaway PG db, clean per test.

    Slug-connect (ADR-115 D2) loads the authoritative snapshot from PG via
    db_pool.get_pool(); seed and connect must share one isolated database.
    """
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


def _seed_pg_for_slug(
    slug: str,
    snap: GameSnapshot,
    *,
    mode: GameMode = GameMode.SOLO,
) -> None:
    """Mirror a seeded snapshot into PG — the store the slug-resume path loads."""
    from sidequest.game import db_pool
    from sidequest.server.session_state import _build_pg_repos_for_slug

    repo, _dungeon, _sink = _build_pg_repos_for_slug(
        db_pool.get_pool(),
        slug=slug,
        mode=str(mode),
        genre_slug=_GENRE,
        world_slug=_WORLD,
    )
    repo.save(snap)


def _ensure_pg_session(slug: str, *, mode: GameMode) -> None:
    """Register the PG session row without a snapshot (ADR-115 F1)."""
    from sidequest.game import db_pool
    from sidequest.server.session_state import _build_pg_repos_for_slug

    _build_pg_repos_for_slug(
        db_pool.get_pool(),
        slug=slug,
        mode=str(mode),
        genre_slug=_GENRE,
        world_slug=_WORLD,
    )


def _make_handler(save_dir: Path) -> WebSocketSessionHandler:
    handler = WebSocketSessionHandler(
        save_dir=save_dir,
        genre_pack_search_paths=[_CONTENT_SEARCH_PATH],
    )
    handler.attach_room_context(
        registry=RoomRegistry(),
        socket_id="sock-test",
        out_queue=asyncio.Queue(),
    )
    return handler


def _seed_solo_game(tmp_path: Path, slug: str, *, with_character: bool) -> Path:
    """Register a SOLO session in Postgres (ADR-115 F1 — connect reads PG).

    ``with_character=False`` registers the session row only (no snapshot →
    chargen); ``with_character=True`` also persists a snapshot carrying one PC.
    """
    if with_character:
        core = CreatureCore(
            name="Parsley",
            description="An engineer with the wrong kind of luck",
            personality="trouble-magnet",
            inventory=Inventory(),
        )
        char = Character(
            core=core,
            char_class="Engineer",
            race="Coreworlder",
            backstory="Outsystem-arrived",
        )
        snap = GameSnapshot(genre_slug=_GENRE, world_slug=_WORLD, location="Far Landing")
        snap.characters = [char]
        snap.player_seats["parsley-pid"] = "Parsley"
        _seed_pg_for_slug(slug, snap, mode=GameMode.SOLO)
    else:
        _ensure_pg_session(slug, mode=GameMode.SOLO)
    return tmp_path


def _seed_mp_game(tmp_path: Path, slug: str) -> Path:
    """Register an empty MP session in Postgres (no snapshot → chargen)."""
    _ensure_pg_session(slug, mode=GameMode.MULTIPLAYER)
    return tmp_path


@pytest.mark.asyncio
async def test_solo_new_connect_auto_seats_player_in_chargen():
    """Solo new session: connect must seat the player so the room's
    lobby state machine reflects truth, ``non_abandoned_player_count()``
    returns 1, and ``orchestrator.notorious_party_gate`` doesn't fire
    its ``player_count=0`` warning on turn 1.
    """
    slug = "2026-04-30-solo-new-test"
    save_dir = _seed_solo_game(Path(__file__).parent / "_tmp_solo_new", slug, with_character=False)
    save_dir.mkdir(parents=True, exist_ok=True)
    save_dir = _seed_solo_game(save_dir, slug, with_character=False)

    handler = _make_handler(save_dir)
    msg = SessionEventMessage(
        type="SESSION_EVENT",
        player_id="parsley-pid",
        payload=SessionEventPayload(
            event="connect",
            game_slug=slug,
            player_name="Parsley",
        ),
    )
    await handler.handle_message(msg)

    room = handler._room
    assert room is not None, "slug-connect must bind a room"
    assert "parsley-pid" in room.seated_player_ids(), (
        "solo connect must auto-seat the player — playtest 2026-04-30 notorious_party_gate=0 race"
    )
    assert room.non_abandoned_player_count() == 1, (
        "non_abandoned_player_count must reflect the seated solo player; "
        "this is the value notorious_party_gate reads"
    )
    # New solo (no existing character) → seat starts in CHARGEN.
    # Chargen-complete will transition to PLAYING; not yet here.
    assert room.playing_player_count() == 0, "new solo session is in CHARGEN, not yet PLAYING"


@pytest.mark.asyncio
async def test_solo_returning_connect_auto_seats_and_transitions_to_playing(tmp_path: Path):
    """Solo returning session: connect must seat AND immediately
    transition to PLAYING so ``playing_player_count()`` is 1 from
    turn 1 (the turn barrier and gate inputs).
    """
    slug = "2026-04-30-solo-returning-test"
    save_dir = _seed_solo_game(tmp_path, slug, with_character=True)

    handler = _make_handler(save_dir)
    msg = SessionEventMessage(
        type="SESSION_EVENT",
        player_id="parsley-pid",
        payload=SessionEventPayload(
            event="connect",
            game_slug=slug,
            player_name="Parsley",
        ),
    )
    await handler.handle_message(msg)

    room = handler._room
    assert room is not None
    assert "parsley-pid" in room.seated_player_ids()
    assert room.non_abandoned_player_count() == 1
    assert room.playing_player_count() == 1, (
        "returning solo player must transition to PLAYING on connect — "
        "their character is committed, the seat should reflect that"
    )


@pytest.mark.asyncio
async def test_solo_reconnect_does_not_reset_seat_state(tmp_path: Path):
    """Solo reconnect (same player_id): the second connect must NOT
    reset the seat to CHARGEN. Idempotency guard — without it, a
    returning solo who reconnects would have their seat thrown back
    to CHARGEN and the lobby would emit a phantom state-transition
    event.
    """
    slug = "2026-04-30-solo-reconnect-test"
    save_dir = _seed_solo_game(tmp_path, slug, with_character=True)

    # First connect — seats and transitions to PLAYING.
    handler1 = _make_handler(save_dir)
    msg = SessionEventMessage(
        type="SESSION_EVENT",
        player_id="parsley-pid",
        payload=SessionEventPayload(
            event="connect",
            game_slug=slug,
            player_name="Parsley",
        ),
    )
    await handler1.handle_message(msg)
    room = handler1._room
    assert room is not None
    assert room.playing_player_count() == 1

    # Second connect on the same registry (reconnect via slug). The
    # registry is shared so the second handler sees the same room.
    handler2 = WebSocketSessionHandler(
        save_dir=save_dir,
        genre_pack_search_paths=[_CONTENT_SEARCH_PATH],
    )
    handler2.attach_room_context(
        registry=handler1._room_registry,  # share the registry
        socket_id="sock-test-2",
        out_queue=asyncio.Queue(),
    )
    await handler2.handle_message(msg)

    # Seat must still be PLAYING — not reset to CHARGEN by re-seating.
    assert room.playing_player_count() == 1, (
        "reconnect must not reset solo seat to CHARGEN — idempotency "
        "guard via seated_player_ids() check"
    )


@pytest.mark.asyncio
async def test_mp_connect_does_not_auto_seat(tmp_path: Path):
    """NEW MP players (has_character=False) preserve the explicit-
    PLAYER_SEAT contract — they claim a slot via the PLAYER_SEAT
    message (handlers/player_seat.py) so the lobby-roster flow
    preserves intent. Returning MP (has_character=True) DOES auto-
    seat now — see test_mp_auto_seat_on_connect.py for that case.
    """
    slug = "2026-04-30-mp-no-auto-seat-test"
    save_dir = _seed_mp_game(tmp_path, slug)

    handler = _make_handler(save_dir)
    msg = SessionEventMessage(
        type="SESSION_EVENT",
        player_id="alice",
        payload=SessionEventPayload(
            event="connect",
            game_slug=slug,
            player_name="Alice",
        ),
    )
    await handler.handle_message(msg)

    room = handler._room
    assert room is not None
    assert room.mode == GameMode.MULTIPLAYER
    assert "alice" not in room.seated_player_ids(), (
        "MP connect must NOT auto-seat — players claim slots via "
        "PLAYER_SEAT explicitly so the lobby roster reflects intent"
    )
    assert room.non_abandoned_player_count() == 0


@pytest.mark.asyncio
async def test_solo_auto_seat_uses_player_seat_character_name_when_returning(tmp_path: Path):
    """Returning solo: the seat's ``character_slot`` should be the
    saved character_name (from snapshot.player_seats), not the lobby
    display_name. Keeps the seat record consistent with what the
    chargen path would have written.
    """
    slug = "2026-04-30-solo-slot-label-test"
    save_dir = _seed_solo_game(tmp_path, slug, with_character=True)

    handler = _make_handler(save_dir)
    msg = SessionEventMessage(
        type="SESSION_EVENT",
        player_id="parsley-pid",
        payload=SessionEventPayload(
            event="connect",
            game_slug=slug,
            player_name="Parsley-LobbyName",
        ),
    )
    await handler.handle_message(msg)

    room = handler._room
    assert room is not None
    seat = room._seated.get("parsley-pid")
    assert seat is not None
    assert seat.character_slot == "Parsley", (
        "returning solo seat should label with the saved character_name "
        "from snapshot.player_seats, not the lobby display_name"
    )
    assert seat.state == LobbyState.PLAYING
