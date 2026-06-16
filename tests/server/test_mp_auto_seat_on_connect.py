"""Regression tests for playtest 2026-04-30 — multiplayer
``playing_player_count=1`` race after returning-player reconnect.

Pre-fix flow (4-player MP playtest):
1. All 4 players complete chargen — saved with characters in
   ``snapshot.characters`` and ``snapshot.player_seats``.
2. Local-storage clear / refresh on each tab triggers a fresh slug-
   connect for each player_id with ``has_character=True``.
3. ``connect.py`` auto-seat block skips MP (gated on
   ``mode == GameMode.SOLO``) — so neither ``room.seat()`` nor
   ``transition_to_playing()`` runs in the connect path for MP.
4. The PLAYER_SEAT path (``handlers/player_seat.py``) is the only
   MP promotion site, but it requires the client to (re-)send
   ``PLAYER_SEAT`` AND ``session._state is _State.Playing`` at that
   moment. After a reconnect with an existing character, those
   conditions don't always coincide — so most MP returning players
   stay in CHARGEN with their seat untransitioned.
5. ``room.playing_player_count()`` returns 1 (or 0). The cinematic
   barrier (``ADR-036`` wired in ``handlers/player_action.py``)
   fires on the first player's submission with ``player_count=1``,
   the narrator dispatches solo-style, and the other three players'
   submissions vanish into a closed round.

The lobby state machine (CONNECTED → CHARGEN → PLAYING → ABANDONED)
is mode-agnostic. Solo got its connect-path auto-promote fixed in
``test_solo_auto_seat_on_connect.py``. MP returning needs the same
treatment — without breaking the explicit-PLAYER_SEAT contract for
**new** MP players (no character yet).

Fix:
- Connect auto-seats when ``mode == SOLO`` OR ``has_character``.
- ``has_character`` returning players (solo OR MP) immediately
  transition CHARGEN → PLAYING. Mirrors the
  ``_handle_player_seat`` returning-player path that was MP-only
  before. Idempotent — already-seated players (reconnect, test
  fixtures) skip the seat() call.
- New MP connects (``has_character=False``) still do NOT auto-seat;
  they go through PLAYER_SEAT explicitly so the lobby-claim flow
  preserves intent.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.persistence import GameMode, SqliteStore, db_path_for_slug, upsert_game
from sidequest.game.session import GameSnapshot
from sidequest.protocol.messages import (
    SessionEventMessage,
    SessionEventPayload,
)
from sidequest.server.session_handler import WebSocketSessionHandler
from sidequest.server.session_room import LobbyState, RoomRegistry

_GENRE = "space_opera"
_WORLD = "coyote_star"
_CONTENT_SEARCH_PATH = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


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


def _make_handler_on_registry(
    save_dir: Path, registry: RoomRegistry, socket_id: str
) -> WebSocketSessionHandler:
    """Make a handler that shares ``registry`` so multiple connects
    against the same slug see the same SessionRoom (mirrors the
    real server's RoomRegistry singleton).
    """
    handler = WebSocketSessionHandler(
        save_dir=save_dir,
        genre_pack_search_paths=[_CONTENT_SEARCH_PATH],
    )
    handler.attach_room_context(
        registry=registry,
        socket_id=socket_id,
        out_queue=asyncio.Queue(),
    )
    return handler


def _seed_mp_game_with_characters(
    tmp_path: Path,
    slug: str,
    *,
    seats: list[tuple[str, str]],
) -> Path:
    """Seed an MP save with N committed characters.

    ``seats`` is a list of ``(player_id, character_name)`` pairs.
    Each character gets a minimal CreatureCore + Character record
    plus a player_seats entry — matching the post-chargen save
    shape that the connect handler reads to set ``has_character``.
    """
    db = db_path_for_slug(tmp_path, slug)
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(db)
    store.initialize()
    upsert_game(
        store,
        slug=slug,
        mode=GameMode.MULTIPLAYER,
        genre_slug=_GENRE,
        world_slug=_WORLD,
    )
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
            Character(
                core=core,
                char_class="Smuggler",
                race="Coreworlder",
                backstory="Came Through the Gate",
            )
        )
        snap.player_seats[player_id] = char_name
    snap.characters = chars
    store.init_session(_GENRE, _WORLD)
    store.save(snap)
    store.close()
    return tmp_path


def _seed_mp_game_no_characters(tmp_path: Path, slug: str) -> Path:
    """Seed an MP save with no characters yet — the new-MP-player
    fixture used to confirm explicit PLAYER_SEAT is preserved.
    """
    db = db_path_for_slug(tmp_path, slug)
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(db)
    store.initialize()
    upsert_game(
        store,
        slug=slug,
        mode=GameMode.MULTIPLAYER,
        genre_slug=_GENRE,
        world_slug=_WORLD,
    )
    store.close()
    return tmp_path


@pytest.mark.asyncio
async def test_mp_returning_player_connect_auto_seats_and_transitions_to_playing(
    tmp_path: Path,
):
    """MP returning player (has_character=True): connect must seat
    AND immediately transition to PLAYING. Mirrors the solo
    returning case — the lobby state machine is mode-agnostic;
    only the new-player slot-claim flow differs between modes.
    """
    slug = "2026-04-30-mp-returning-test"
    save_dir = _seed_mp_game_with_characters(tmp_path, slug, seats=[("john-pid", "John")])

    handler = _make_handler(save_dir)
    msg = SessionEventMessage(
        type="SESSION_EVENT",
        player_id="john-pid",
        payload=SessionEventPayload(
            event="connect",
            game_slug=slug,
            player_name="John",
        ),
    )
    await handler.handle_message(msg)

    room = handler._room
    assert room is not None, "slug-connect must bind a room"
    assert room.mode == GameMode.MULTIPLAYER
    assert "john-pid" in room.seated_player_ids(), (
        "MP returning player must auto-seat on connect — playtest "
        "2026-04-30 playing_player_count=1 race"
    )
    assert room.non_abandoned_player_count() == 1
    assert room.playing_player_count() == 1, (
        "MP returning player must transition CHARGEN → PLAYING on "
        "connect — their character is committed, the seat should "
        "reflect that. Without this the cinematic barrier fires on "
        "playing_player_count=1 even when 4 players are seated."
    )


@pytest.mark.asyncio
async def test_mp_four_player_returning_connect_all_to_playing(tmp_path: Path):
    """4-player MP playtest cast (Beatles): all four reconnect with
    has_character=True. ``playing_player_count()`` must equal 4
    after all four connect — the cinematic barrier needs every
    PLAYING peer counted before it can wait correctly.
    """
    slug = "2026-04-30-mp-beatles-test"
    seats = [
        ("john-pid", "John"),
        ("paul-pid", "Paul"),
        ("george-pid", "George"),
        ("ringo-pid", "Ringo"),
    ]
    save_dir = _seed_mp_game_with_characters(tmp_path, slug, seats=seats)

    registry = RoomRegistry()
    for i, (pid, char_name) in enumerate(seats):
        handler = _make_handler_on_registry(save_dir, registry=registry, socket_id=f"sock-{i}")
        msg = SessionEventMessage(
            type="SESSION_EVENT",
            player_id=pid,
            payload=SessionEventPayload(
                event="connect",
                game_slug=slug,
                player_name=char_name,
            ),
        )
        await handler.handle_message(msg)

    # All four players sit in the same shared room (RoomRegistry
    # singleton matches production). Pull it from the registry.
    rooms = list(registry._rooms.values()) if hasattr(registry, "_rooms") else []
    assert len(rooms) == 1, (
        "all four MP connects must converge on a single SessionRoom "
        f"keyed by slug; got {len(rooms)}"
    )
    room = rooms[0]
    assert room.playing_player_count() == 4, (
        "all four returning MP players must be in PLAYING after their "
        "connects — playing_player_count is the cinematic-barrier "
        "input. Without this, John's first action fires the barrier "
        "with player_count=1 and Paul/George/Ringo's submissions land "
        "into an already-closed round (playtest 2026-04-30)."
    )
    for pid, _ in seats:
        assert pid in room.seated_player_ids(), f"player {pid!r} must be seated after connect"
        seat = room._seated.get(pid)
        assert seat is not None and seat.state == LobbyState.PLAYING, (
            f"player {pid!r} seat state must be PLAYING (got {seat.state if seat else None!r})"
        )


@pytest.mark.asyncio
async def test_mp_new_player_connect_does_not_auto_seat(tmp_path: Path):
    """MP NEW player (has_character=False): connect must NOT
    auto-seat. New MP players claim slots via the PLAYER_SEAT
    message (handlers/player_seat.py) so the lobby-roster flow
    preserves intent — auto-seat for new MP would short-circuit
    the slot-claim contract.

    Coverage gap: this is the case ``test_mp_connect_does_not_
    auto_seat`` in test_solo_auto_seat_on_connect.py covers; this
    test re-asserts it from the MP test file's perspective so
    a future refactor doesn't accidentally promote new MP players
    while wiring returning auto-seat.
    """
    slug = "2026-04-30-mp-new-no-auto-seat-test"
    save_dir = _seed_mp_game_no_characters(tmp_path, slug)

    handler = _make_handler(save_dir)
    msg = SessionEventMessage(
        type="SESSION_EVENT",
        player_id="alice-new",
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
    assert "alice-new" not in room.seated_player_ids(), (
        "MP NEW player (no character yet) must NOT auto-seat — they "
        "claim slots via PLAYER_SEAT explicitly"
    )
    assert room.non_abandoned_player_count() == 0


@pytest.mark.asyncio
async def test_mp_returning_reconnect_does_not_reset_seat_state(tmp_path: Path):
    """MP reconnect (same player_id, has_character=True): the
    second connect must NOT reset the seat to CHARGEN. Idempotency
    guard — the auto-seat block skips when ``player_id in
    seated_player_ids()``, so reconnect must not flip back.
    """
    slug = "2026-04-30-mp-reconnect-test"
    save_dir = _seed_mp_game_with_characters(tmp_path, slug, seats=[("john-pid", "John")])

    registry = RoomRegistry()
    handler1 = _make_handler_on_registry(save_dir, registry=registry, socket_id="sock-a")
    msg = SessionEventMessage(
        type="SESSION_EVENT",
        player_id="john-pid",
        payload=SessionEventPayload(
            event="connect",
            game_slug=slug,
            player_name="John",
        ),
    )
    await handler1.handle_message(msg)
    room = handler1._room
    assert room is not None
    assert room.playing_player_count() == 1

    # Second connect on same registry — must not reset state.
    handler2 = _make_handler_on_registry(save_dir, registry=registry, socket_id="sock-b")
    await handler2.handle_message(msg)

    assert room.playing_player_count() == 1, (
        "MP reconnect must not reset seat to CHARGEN — idempotency "
        "guard via seated_player_ids() check"
    )


@pytest.mark.asyncio
async def test_mp_returning_seat_uses_saved_character_name(tmp_path: Path):
    """Returning MP: the seat's ``character_slot`` should be the
    saved character_name (from snapshot.player_seats), not the
    lobby display_name. Keeps the seat record consistent with
    what chargen-complete would have written.
    """
    slug = "2026-04-30-mp-slot-label-test"
    save_dir = _seed_mp_game_with_characters(tmp_path, slug, seats=[("john-pid", "John")])

    handler = _make_handler(save_dir)
    msg = SessionEventMessage(
        type="SESSION_EVENT",
        player_id="john-pid",
        payload=SessionEventPayload(
            event="connect",
            game_slug=slug,
            player_name="John-LobbyName",
        ),
    )
    await handler.handle_message(msg)

    room = handler._room
    assert room is not None
    seat = room._seated.get("john-pid")
    assert seat is not None
    assert seat.character_slot == "John", (
        "returning MP seat should label with the saved character_name "
        "from snapshot.player_seats, not the lobby display_name"
    )
    assert seat.state == LobbyState.PLAYING
