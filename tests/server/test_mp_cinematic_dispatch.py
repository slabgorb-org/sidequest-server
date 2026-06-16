"""Wiring tests for ADR-036 Cinematic mode — see
docs/superpowers/specs/2026-04-26-mp-cinematic-mode-wiring-design.md.

These tests verify the multiplayer barrier + dispatch election. Each test
either calls SessionRoom helpers directly (unit) or drives
``_handle_player_action`` end-to-end with mocked Claude (integration).
"""

from __future__ import annotations

import asyncio

import pytest

from sidequest.game.persistence import GameMode
from sidequest.server.session_room import PendingAction, SessionRoom


def test_pending_action_dataclass_holds_character_and_action() -> None:
    pa = PendingAction(character_name="Gladstone", action="I prepare for the dungeon")
    assert pa.character_name == "Gladstone"
    assert pa.action == "I prepare for the dungeon"


def test_record_and_drain_returns_in_submission_order() -> None:
    room = SessionRoom(slug="test-slug", mode=GameMode.MULTIPLAYER)
    room.record_pending_action("p1", "Gladstone", "I prepare for the dungeon")
    room.record_pending_action("p2", "Zanzibar Jones", "I get my pole")
    drained = room.drain_pending_actions()
    assert [pid for pid, _ in drained] == ["p1", "p2"]
    assert drained[0][1].character_name == "Gladstone"
    assert drained[0][1].action == "I prepare for the dungeon"
    assert drained[1][1].character_name == "Zanzibar Jones"
    assert drained[1][1].action == "I get my pole"


def test_drain_empties_the_buffer() -> None:
    room = SessionRoom(slug="test-slug", mode=GameMode.MULTIPLAYER)
    room.record_pending_action("p1", "Glad", "act1")
    room.drain_pending_actions()
    assert room.drain_pending_actions() == []


def test_record_same_player_twice_is_last_write_wins() -> None:
    room = SessionRoom(slug="test-slug", mode=GameMode.MULTIPLAYER)
    room.record_pending_action("p1", "Gladstone", "I changed my mind")
    room.record_pending_action("p1", "Gladstone", "I really changed my mind")
    drained = room.drain_pending_actions()
    assert len(drained) == 1
    assert drained[0][1].action == "I really changed my mind"


def test_first_pending_at_monotonic_starts_none() -> None:
    room = SessionRoom(slug="test-slug", mode=GameMode.MULTIPLAYER)
    assert room.first_pending_at_monotonic() is None


def test_first_pending_at_monotonic_set_on_first_submission() -> None:
    """Stamp is set on first submission, stays stable across additions."""
    room = SessionRoom(slug="test-slug", mode=GameMode.MULTIPLAYER)
    room.record_pending_action("p1", "Gladstone", "act1")
    first = room.first_pending_at_monotonic()
    assert first is not None
    room.record_pending_action("p2", "Zanzibar", "act2")
    assert room.first_pending_at_monotonic() == first


def test_first_pending_at_monotonic_cleared_on_drain() -> None:
    room = SessionRoom(slug="test-slug", mode=GameMode.MULTIPLAYER)
    room.record_pending_action("p1", "G", "act1")
    assert room.first_pending_at_monotonic() is not None
    room.drain_pending_actions()
    assert room.first_pending_at_monotonic() is None


def test_first_pending_restamped_after_drain() -> None:
    """Round N+1 gets a fresh stamp after round N drained."""
    import time

    room = SessionRoom(slug="test-slug", mode=GameMode.MULTIPLAYER)
    room.record_pending_action("p1", "G", "act1")
    first = room.first_pending_at_monotonic()
    room.drain_pending_actions()
    time.sleep(0.001)  # ensure monotonic has advanced
    room.record_pending_action("p1", "G", "act2")
    second = room.first_pending_at_monotonic()
    assert second is not None
    assert first is not None
    assert second > first


def test_dispatch_lock_is_an_asyncio_lock() -> None:
    room = SessionRoom(slug="test-slug", mode=GameMode.MULTIPLAYER)
    assert isinstance(room.dispatch_lock, asyncio.Lock)


def test_last_dispatched_round_starts_at_zero() -> None:
    room = SessionRoom(slug="test-slug", mode=GameMode.MULTIPLAYER)
    assert room.last_dispatched_round == 0


def test_last_dispatched_round_is_writable() -> None:
    room = SessionRoom(slug="test-slug", mode=GameMode.MULTIPLAYER)
    room.last_dispatched_round = 5
    assert room.last_dispatched_round == 5


def test_seated_player_count_returns_zero_when_no_seats() -> None:
    room = SessionRoom(slug="test-slug", mode=GameMode.MULTIPLAYER)
    assert room.seated_player_count() == 0


def test_seated_player_count_after_seat() -> None:
    room = SessionRoom(slug="test-slug", mode=GameMode.MULTIPLAYER)
    room.connect("p1", socket_id="s1")
    room.seat("p1", character_slot="Gladstone")
    room.connect("p2", socket_id="s2")
    room.seat("p2", character_slot="Zanzibar Jones")
    assert room.seated_player_count() == 2


# ---------------------------------------------------------------------------
# ADR-036 Task 3 — buffer+barrier wiring
# ---------------------------------------------------------------------------

from unittest.mock import AsyncMock  # noqa: E402

from sidequest.protocol.messages import PlayerActionMessage, PlayerActionPayload  # noqa: E402
from sidequest.protocol.types import NonBlankString  # noqa: E402


@pytest.mark.asyncio
async def test_first_of_two_players_buffers_and_returns_empty(
    session_handler_factory,
) -> None:
    """When player 1 submits in a 2-seat room, the action is buffered and
    the handler returns [] (still waiting on player 2). The narrator must
    NOT run yet."""
    handler, sd, room = session_handler_factory(
        slug="test-mp-grimvault",
        mode=GameMode.MULTIPLAYER,
        seat_players=[("p1", "Gladstone"), ("p2", "Zanzibar Jones")],
        active_player=("p1", "Gladstone"),
    )
    # Spy on _execute_narration_turn — it must NOT be called this turn.
    handler._execute_narration_turn = AsyncMock(  # type: ignore[method-assign]
        return_value=[],
    )

    msg = PlayerActionMessage(
        payload=PlayerActionPayload(
            action=NonBlankString.model_validate("I prepare for the dungeon"),
        ),
        player_id="p1",
    )
    result = await handler._handle_player_action(msg)

    assert result == []
    handler._execute_narration_turn.assert_not_called()
    # Buffer holds Gladstone's action.
    drained = room.drain_pending_actions()
    assert len(drained) == 1
    assert drained[0][0] == "p1"
    assert drained[0][1].character_name == "Gladstone"
    assert drained[0][1].action == "I prepare for the dungeon"


# ---------------------------------------------------------------------------
# ADR-036 Task 4 — elected-dispatch branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_players_combine_into_one_narrator_dispatch(
    session_handler_factory,
) -> None:
    """When player 2 submits in a 2-seat room (player 1 already submitted),
    the barrier fires and exactly one narrator dispatch happens with both
    actions concatenated as labeled prose."""
    handler1, sd1, room = session_handler_factory(
        slug="test-mp-grimvault",
        mode=GameMode.MULTIPLAYER,
        seat_players=[("p1", "Gladstone"), ("p2", "Zanzibar Jones")],
        active_player=("p1", "Gladstone"),
    )
    handler2, sd2, _ = session_handler_factory(
        slug="test-mp-grimvault",
        mode=GameMode.MULTIPLAYER,
        seat_players=[("p1", "Gladstone"), ("p2", "Zanzibar Jones")],
        active_player=("p2", "Zanzibar Jones"),
        existing_room=room,
    )
    # Spy on _execute_narration_turn for both handlers — same room, both
    # methods bound to the room's snapshot.
    captured: list[str] = []

    async def fake_execute(sd, action, turn_context):
        captured.append(action)
        return []

    handler1._execute_narration_turn = fake_execute  # type: ignore[method-assign]
    handler2._execute_narration_turn = fake_execute  # type: ignore[method-assign]

    # Player 1 submits — buffers and returns [].
    msg1 = PlayerActionMessage(
        payload=PlayerActionPayload(
            action=NonBlankString.model_validate("I prepare for the dungeon"),
        ),
        player_id="p1",
    )
    r1 = await handler1._handle_player_action(msg1)
    assert r1 == []
    assert captured == []  # narrator NOT called yet

    # Player 2 submits — barrier fires, elected branch combines and dispatches.
    msg2 = PlayerActionMessage(
        payload=PlayerActionPayload(
            action=NonBlankString.model_validate("I get my pole"),
        ),
        player_id="p2",
    )
    await handler2._handle_player_action(msg2)
    # Exactly one narrator call with both actions.
    assert len(captured) == 1
    combined = captured[0]
    assert "Gladstone: I prepare for the dungeon" in combined
    assert "Zanzibar Jones: I get my pole" in combined
    # interaction counter advanced (CAS now uses interaction, not round)
    assert room.last_dispatched_round == room.snapshot.turn_manager.interaction


@pytest.mark.asyncio
async def test_mp_round_advances_interaction_exactly_once(
    session_handler_factory,
) -> None:
    """`record_interaction()` was being called twice per MP round (once
    inside _execute_narration_turn, once in the elected-dispatch branch).
    This test pins down the fix: real _execute_narration_turn body runs to
    completion and turn_manager.interaction advances by exactly 1."""
    handler1, sd1, room = session_handler_factory(
        slug="test-mp-interaction",
        mode=GameMode.MULTIPLAYER,
        seat_players=[("p1", "Gladstone"), ("p2", "Zanzibar Jones")],
        active_player=("p1", "Gladstone"),
    )
    handler2, sd2, _ = session_handler_factory(
        slug="test-mp-interaction",
        mode=GameMode.MULTIPLAYER,
        seat_players=[("p1", "Gladstone"), ("p2", "Zanzibar Jones")],
        active_player=("p2", "Zanzibar Jones"),
        existing_room=room,
    )

    # Capture interaction count via a non-mocked passthrough that ALSO calls
    # the real record_interaction internally. To avoid pulling the entire
    # narrator stack into the test, we replace _execute_narration_turn with
    # a stub that calls record_interaction once (matching the real method's
    # behavior at session_handler.py:3631) and returns an empty list.
    async def fake_execute_with_record(sd, action, turn_context):
        sd.snapshot.turn_manager.record_interaction()
        return []

    handler1._execute_narration_turn = fake_execute_with_record  # type: ignore[method-assign]
    handler2._execute_narration_turn = fake_execute_with_record  # type: ignore[method-assign]

    initial_interaction = room.snapshot.turn_manager.interaction

    msg1 = PlayerActionMessage(
        payload=PlayerActionPayload(
            action=NonBlankString.model_validate("I prepare for the dungeon"),
        ),
        player_id="p1",
    )
    await handler1._handle_player_action(msg1)

    msg2 = PlayerActionMessage(
        payload=PlayerActionPayload(
            action=NonBlankString.model_validate("I get my pole"),
        ),
        player_id="p2",
    )
    await handler2._handle_player_action(msg2)

    final_interaction = room.snapshot.turn_manager.interaction
    assert final_interaction - initial_interaction == 1, (
        f"interaction advanced by {final_interaction - initial_interaction}, "
        f"expected exactly 1 per multiplayer round"
    )


# ---------------------------------------------------------------------------
# ADR-036 Task 5 — solo immediate dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_solo_room_dispatches_immediately_no_buffering_observable(
    session_handler_factory,
) -> None:
    """A single seated player triggers the barrier on their first submission.
    The narrator runs exactly once on that submission with the player's
    action wrapped as labeled prose."""
    handler, sd, room = session_handler_factory(
        slug="test-solo-grimvault",
        mode=GameMode.MULTIPLAYER,
        seat_players=[("p1", "Gladstone")],
        active_player=("p1", "Gladstone"),
    )
    captured: list[str] = []

    async def fake_execute(sd, action, turn_context):
        captured.append(action)
        return []

    handler._execute_narration_turn = fake_execute  # type: ignore[method-assign]

    msg = PlayerActionMessage(
        payload=PlayerActionPayload(
            action=NonBlankString.model_validate("I look around"),
        ),
        player_id="p1",
    )
    result = await handler._handle_player_action(msg)

    assert result == []
    assert len(captured) == 1
    # With one seated player, the combined-prose builder still runs but the
    # output is just one line.
    assert "Gladstone: I look around" in captured[0]
    assert room.last_dispatched_round == 1


# ---------------------------------------------------------------------------
# ADR-036 Task 6 — concurrent-dispatch race test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_submissions_dispatch_exactly_once(
    session_handler_factory,
) -> None:
    """Two _handle_player_action calls awaited concurrently via asyncio.gather.
    The dispatch_lock + last_dispatched_round CAS must guarantee exactly one
    narrator call."""
    handler1, sd1, room = session_handler_factory(
        slug="test-mp-grimvault-race",
        mode=GameMode.MULTIPLAYER,
        seat_players=[("p1", "Gladstone"), ("p2", "Zanzibar Jones")],
        active_player=("p1", "Gladstone"),
    )
    handler2, sd2, _ = session_handler_factory(
        slug="test-mp-grimvault-race",
        mode=GameMode.MULTIPLAYER,
        seat_players=[("p1", "Gladstone"), ("p2", "Zanzibar Jones")],
        active_player=("p2", "Zanzibar Jones"),
        existing_room=room,
    )
    captured: list[str] = []

    async def fake_execute(sd, action, turn_context):
        # Yield to the event loop so the two handlers can interleave.
        await asyncio.sleep(0)
        captured.append(action)
        return []

    handler1._execute_narration_turn = fake_execute  # type: ignore[method-assign]
    handler2._execute_narration_turn = fake_execute  # type: ignore[method-assign]

    msg1 = PlayerActionMessage(
        payload=PlayerActionPayload(
            action=NonBlankString.model_validate("I prepare for the dungeon"),
        ),
        player_id="p1",
    )
    msg2 = PlayerActionMessage(
        payload=PlayerActionPayload(
            action=NonBlankString.model_validate("I get my pole"),
        ),
        player_id="p2",
    )
    r1, r2 = await asyncio.gather(
        handler1._handle_player_action(msg1),
        handler2._handle_player_action(msg2),
    )
    assert r1 == [] and r2 == []
    assert len(captured) == 1, f"expected exactly one dispatch, got {len(captured)}"


# ---------------------------------------------------------------------------
# ADR-036 Task 7 — disconnect-buffer-survival
# ---------------------------------------------------------------------------


def test_buffered_action_survives_buffer_owner_disconnect() -> None:
    """If a player submits, then disconnects before the barrier fires, the
    buffered PendingAction stays in the room buffer. (Pause-gate semantics
    happen at the handler entry point — this test covers the buffer-state
    invariant.)

    Story 45-2 update: pause now keys on PLAYING-but-disconnected (not on
    every seated peer). Promoting both peers to PLAYING here preserves
    this test's original authorial intent — a paused game with a
    buffered action — under the new lobby state machine.
    """
    room = SessionRoom(slug="test-disc", mode=GameMode.MULTIPLAYER)
    room.connect("p1", socket_id="s1")
    room.seat("p1", character_slot="Gladstone")
    room.transition_to_playing("p1")
    room.connect("p2", socket_id="s2")
    room.seat("p2", character_slot="Zanzibar Jones")
    room.transition_to_playing("p2")

    # p1 submits.
    room.record_pending_action("p1", "Gladstone", "I prepare for the dungeon")

    # p1 disconnects (simulating WS drop). PLAYING peer disconnect → seat
    # stays held in PLAYING (pause kicks in), NOT abandoned.
    room.disconnect(socket_id="s1")
    # Still seated despite disconnect (seat survives socket drop).
    assert "p1" in room.seated_player_ids()
    assert "p1" in room.absent_seated_player_ids()
    assert room.is_paused()

    # Buffered action survives.
    drained = room.drain_pending_actions()
    assert len(drained) == 1
    assert drained[0][0] == "p1"
    assert drained[0][1].action == "I prepare for the dungeon"


# ---------------------------------------------------------------------------
# ADR-036 Task 8 — OTEL watcher events
# ---------------------------------------------------------------------------

from unittest.mock import patch  # noqa: E402


@pytest.mark.asyncio
async def test_otel_events_emitted_on_barrier_fire_and_dispatch(
    session_handler_factory,
) -> None:
    """The GM panel needs to see when the barrier fires and when the
    elected dispatcher runs the narrator (CLAUDE.md OTEL principle)."""
    handler1, sd1, room = session_handler_factory(
        slug="test-otel-grimvault",
        mode=GameMode.MULTIPLAYER,
        seat_players=[("p1", "Gladstone"), ("p2", "Zanzibar Jones")],
        active_player=("p1", "Gladstone"),
    )
    handler2, sd2, _ = session_handler_factory(
        slug="test-otel-grimvault",
        mode=GameMode.MULTIPLAYER,
        seat_players=[("p1", "Gladstone"), ("p2", "Zanzibar Jones")],
        active_player=("p2", "Zanzibar Jones"),
        existing_room=room,
    )

    async def fake_execute(sd, action, turn_context):
        return []

    handler1._execute_narration_turn = fake_execute  # type: ignore[method-assign]
    handler2._execute_narration_turn = fake_execute  # type: ignore[method-assign]

    # Patch the _watcher_publish symbol used inside the PLAYER_ACTION
    # first-class handler (where the mp.barrier_fired / mp.round_dispatched
    # / turn_status events are emitted from).
    with patch("sidequest.handlers.player_action._watcher_publish") as wp:
        msg1 = PlayerActionMessage(
            payload=PlayerActionPayload(
                action=NonBlankString.model_validate("I prepare for the dungeon"),
            ),
            player_id="p1",
        )
        await handler1._handle_player_action(msg1)

        msg2 = PlayerActionMessage(
            payload=PlayerActionPayload(
                action=NonBlankString.model_validate("I get my pole"),
            ),
            player_id="p2",
        )
        await handler2._handle_player_action(msg2)

    event_names = [call.args[0] for call in wp.call_args_list]
    # turn_status broadcasts (per-submission) + mp.barrier_fired (once on
    # last submission) + mp.round_dispatched (once on dispatch entry).
    assert "mp.barrier_fired" in event_names
    assert "mp.round_dispatched" in event_names
    # Each fires exactly once for this round.
    assert event_names.count("mp.barrier_fired") == 1
    assert event_names.count("mp.round_dispatched") == 1


# ---------------------------------------------------------------------------
# ADR-036 Fix 1 regression — multi-round CAS must use interaction not round
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_fires_in_round_two_after_round_one_completes(
    session_handler_factory,
) -> None:
    """The CAS guard must allow the second round's dispatch to fire after
    the first round completed. Catches the dead-counter bug where
    last_dispatched_round was checked against turn_manager.round (which
    never advances) instead of turn_manager.interaction."""
    handler1, sd1, room = session_handler_factory(
        slug="test-mp-twoturn",
        mode=GameMode.MULTIPLAYER,
        seat_players=[("p1", "Gladstone"), ("p2", "Zanzibar Jones")],
        active_player=("p1", "Gladstone"),
    )
    handler2, sd2, _ = session_handler_factory(
        slug="test-mp-twoturn",
        mode=GameMode.MULTIPLAYER,
        seat_players=[("p1", "Gladstone"), ("p2", "Zanzibar Jones")],
        active_player=("p2", "Zanzibar Jones"),
        existing_room=room,
    )
    captured: list[str] = []

    async def fake_execute(sd, action, turn_context):
        captured.append(action)
        # Match the real method's behavior: advance interaction.
        sd.snapshot.turn_manager.record_interaction()
        return []

    handler1._execute_narration_turn = fake_execute  # type: ignore[method-assign]
    handler2._execute_narration_turn = fake_execute  # type: ignore[method-assign]

    # Round 1
    msg1a = PlayerActionMessage(
        payload=PlayerActionPayload(
            action=NonBlankString.model_validate("Round 1 — Gladstone"),
        ),
        player_id="p1",
    )
    msg1b = PlayerActionMessage(
        payload=PlayerActionPayload(
            action=NonBlankString.model_validate("Round 1 — Zanzibar"),
        ),
        player_id="p2",
    )
    await handler1._handle_player_action(msg1a)
    await handler2._handle_player_action(msg1b)
    assert len(captured) == 1, f"round 1 dispatch failed: {captured}"
    assert "Gladstone: Round 1 — Gladstone" in captured[0]
    assert "Zanzibar Jones: Round 1 — Zanzibar" in captured[0]

    # Round 2 — different actions, must dispatch a SECOND time.
    msg2a = PlayerActionMessage(
        payload=PlayerActionPayload(
            action=NonBlankString.model_validate("Round 2 — Gladstone"),
        ),
        player_id="p1",
    )
    msg2b = PlayerActionMessage(
        payload=PlayerActionPayload(
            action=NonBlankString.model_validate("Round 2 — Zanzibar"),
        ),
        player_id="p2",
    )
    await handler1._handle_player_action(msg2a)
    await handler2._handle_player_action(msg2b)
    assert len(captured) == 2, (
        f"round 2 dispatch silently skipped — CAS guard regression. captured: {captured}"
    )
    assert "Gladstone: Round 2 — Gladstone" in captured[1]
    assert "Zanzibar Jones: Round 2 — Zanzibar" in captured[1]
