"""Wire-first test for Story 45-2: chargen → PLAYING transition through
the production `_chargen_confirmation()` seam.

The CLAUDE.md "Verify Wiring" principle: tests-pass-but-nothing-is-wired
is a known failure mode. The original 45-2 test suite asserted the
state-machine API correctly (LobbyState, transition_to_playing, etc.) and
the barrier predicate correctly (playing_player_count drives the barrier),
but had a gap: NO test exercised the actual chargen-success path that
calls `room.transition_to_playing(player_id)` at session_handler.py:2999.
If Dev had simply omitted that line, the conftest fixture's auto-promote
would still run, all unit tests would still pass, but the production
seam would silently never fire — peers who completed chargen would stay
in CHARGEN forever and the barrier would never count them.

This test closes that gap. It walks the slug-connect chargen flow in MP
context with `caverns_and_claudes` content, sends a real CONFIRMATION
message through `handle_message`, and asserts the seat is PLAYING after
`_chargen_confirmation()` returns.

If session_handler.py:2999 is removed, this test fails.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sidequest.game.persistence import (
    GameMode,
    SqliteStore,
    db_path_for_slug,
    upsert_game,
)
from sidequest.protocol import GameMessage
from sidequest.protocol.messages import (
    CharacterCreationMessage,
    CharacterCreationPayload,
)
from sidequest.server.session_handler import WebSocketSessionHandler
from sidequest.server.session_room import LobbyState, RoomRegistry

CONTENT_ROOT = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


def _seed_mp_save(tmp_path: Path, slug: str, genre: str, world: str) -> None:
    """Mirror tests/server/test_seat_claim.py:_seed — empty MP save row."""
    db = db_path_for_slug(tmp_path, slug)
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(db)
    store.initialize()
    upsert_game(
        store,
        slug=slug,
        mode=GameMode.MULTIPLAYER,
        genre_slug=genre,
        world_slug=world,
    )
    store.close()


@pytest.mark.asyncio
async def test_chargen_confirmation_transitions_seat_to_playing(
    tmp_path: Path,
) -> None:
    """The killer wire-first test for AC2/AC3 chargen completion.

    Flow:
      1. Seed an empty MP save (no character yet → handler enters Creating).
      2. Attach room context + WebSocket fixtures.
      3. SESSION_EVENT.connect with game_slug → handler binds room, enters
         _State.Creating, builder constructed.
      4. PLAYER_SEAT → room.seat() puts the seat in CHARGEN. The
         returning-player-promotion path at session_handler.py:1148 does
         NOT fire (handler is Creating, not Playing).
      5. Walk chargen scenes (caverns_and_claudes has 4 short scenes).
      6. Send phase=confirmation → `_chargen_confirmation` runs →
         `self._state = _State.Playing` → `self._room.transition_to_playing(player_id)`.
      7. Assert `room._seated[player_id].state == LobbyState.PLAYING`.

    This is the seam Reviewer flagged: if session_handler.py:2999 (the
    `transition_to_playing()` call inside `_chargen_confirmation`) is
    removed, the seat stays in CHARGEN, and this test catches it.
    """
    if not (CONTENT_ROOT / "caverns_and_claudes").is_dir():
        pytest.skip("caverns_and_claudes content not found")

    slug = "wire-test-chargen-to-playing"
    genre = "caverns_and_claudes"
    world = "flickering_reach"
    player_id = "rux"

    _seed_mp_save(tmp_path, slug, genre, world)
    registry = RoomRegistry()
    handler = WebSocketSessionHandler(
        save_dir=tmp_path,
        genre_pack_search_paths=[CONTENT_ROOT],
    )
    out_queue: asyncio.Queue[object] = asyncio.Queue()
    handler.attach_room_context(
        registry=registry,
        socket_id=f"sock-{player_id}",
        out_queue=out_queue,
    )

    # 1. Slug-connect — handler enters Creating state because the save has
    #    no character yet.
    connect_msg = GameMessage.model_validate(
        {
            "type": "SESSION_EVENT",
            "player_id": player_id,
            "payload": {
                "event": "connect",
                "game_slug": slug,
                "player_name": "Rux",
            },
        }
    )
    out = await handler.handle_message(connect_msg)
    # The slug-connect handler emits a SESSION_EVENT with payload.event ==
    # "connected" (sidequest/handlers/connect.py:738). Verify that specific
    # message rather than a bare truthy `assert out` — otherwise any
    # response (including an error envelope) would silently pass.
    assert any(
        getattr(m, "type", None) == "SESSION_EVENT"
        and getattr(getattr(m, "payload", None), "event", None) == "connected"
        for m in out
    ), (
        "connect must produce a SESSION_EVENT with payload.event='connected'; "
        f"got {[(getattr(m, 'type', None), getattr(getattr(m, 'payload', None), 'event', None)) for m in out]}"
    )

    # 2. PLAYER_SEAT — seat goes to CHARGEN.
    seat_msg = GameMessage.model_validate(
        {
            "type": "PLAYER_SEAT",
            "player_id": player_id,
            "payload": {"character_slot": "Rux"},
        }
    )
    await handler.handle_message(seat_msg)
    room = registry.get(slug)
    assert room is not None, "room must be created on slug-connect"
    assert room._seated[player_id].state == LobbyState.CHARGEN, (  # noqa: SLF001
        "After PLAYER_SEAT (handler in Creating state), seat must be in "
        "CHARGEN — the returning-player promotion at session_handler.py:1148 "
        "should NOT fire because state is not yet Playing"
    )

    # 3. Walk chargen scenes via CHARACTER_CREATION messages until builder
    #    reaches confirmation.
    sd = handler._session_data  # type: ignore[attr-defined]
    assert sd is not None and sd.builder is not None, (
        "connect to caverns must construct a chargen builder"
    )
    builder = sd.builder

    # caverns_and_claudes has 4 chargen scenes — exact step count varies.
    # Walk until is_confirmation() with a safety bound.
    max_steps = 20
    for _step in range(max_steps):
        if builder.is_confirmation():
            break
        if builder.is_in_progress():
            scene = builder.current_scene()
            if scene.choices:
                payload = CharacterCreationPayload(phase="scene", choice="1")
            elif scene.allows_freeform:
                payload = CharacterCreationPayload(phase="scene", choice="Rux")
            else:
                payload = CharacterCreationPayload(phase="continue")
            msg = CharacterCreationMessage(payload=payload, player_id=player_id)
            await handler.handle_message(msg)
        else:
            pytest.fail(
                f"unexpected chargen phase while walking scenes: {builder._phase!r}"  # noqa: SLF001
            )
    else:
        pytest.fail(f"chargen did not reach confirmation within {max_steps} steps")

    # PRE-CONDITION: builder ready, seat still CHARGEN, handler still
    # _State.Creating. The next message is the load-bearing one.
    assert builder.is_confirmation()
    assert room._seated[player_id].state == LobbyState.CHARGEN, (  # noqa: SLF001
        "Pre-confirmation: seat must still be CHARGEN until _chargen_confirmation fires"
    )

    # 4. THE WIRE TEST — send confirmation message. _chargen_confirmation
    #    runs, builds the character, and (per Story 45-2) calls
    #    self._room.transition_to_playing(player_id) at line 2999.
    confirm_msg = CharacterCreationMessage(
        payload=CharacterCreationPayload(phase="confirmation"),
        player_id=player_id,
    )
    await handler.handle_message(confirm_msg)

    # POST-CONDITION: the seat MUST be in PLAYING state. If
    # session_handler.py:2999 was removed, this assertion fails.
    assert room._seated[player_id].state == LobbyState.PLAYING, (  # noqa: SLF001
        f"After _chargen_confirmation success, seat MUST transition to "
        f"PLAYING — this is the wire-test for session_handler.py:2999. "
        f"Current state: {room._seated[player_id].state!r}. "  # noqa: SLF001
        f"If this is CHARGEN, the transition_to_playing() call inside "
        f"_chargen_confirmation is missing or unreachable."
    )
    # Sanity: predicate reflects the new state.
    assert player_id in room.playing_player_ids(), (
        "playing_player_ids() must include the freshly-PLAYING peer"
    )
    assert room.playing_player_count() == 1
