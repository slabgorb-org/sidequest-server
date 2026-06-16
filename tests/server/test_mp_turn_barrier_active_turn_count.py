"""Wire-first boundary tests for Story 45-2 — turn barrier counts active
turn-takers, not lobby connections.

This file is the wire-first gate for the story. The unit-level state
machine lives in `tests/server/test_lobby_state_machine.py`; here we
exercise the actual barrier-decision seam via `_handle_player_action`
through the MP fixture.

The evropi scenario (Playtest 3, 2026-04-19): four save files clustered
at 16:30–16:31 UTC; only Rux played, but the structured-mode turn barrier
waited on all four lobby connections, so Rux hit barriers mid-solo. The
fix: the predicate at `session_handler.py:3222` reads
`room.playing_player_count()` instead of `room.seated_player_count()`,
and disconnect-during-chargen abandons the seat.

These tests are RED today: `playing_player_count()` does not exist;
`set_player_count()` is fed `seated_player_count()`; `barrier.wait` span
is not emitted; `LobbyState` is not defined.

See `sprint/context/context-story-45-2.md` for the full design.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from sidequest.game.persistence import GameMode
from sidequest.protocol.messages import PlayerActionMessage, PlayerActionPayload
from sidequest.protocol.types import NonBlankString

# ---------------------------------------------------------------------------
# AC1 — barrier fires only on playing peers (the evropi scenario, wire-first)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_barrier_fires_when_only_playing_peer_submits_evropi_scenario(
    session_handler_factory,
) -> None:
    """The killer test: 4 seated peers, 3 in CHARGEN, 1 in PLAYING. The
    PLAYING peer submits an action → the structured-mode turn barrier
    fires on the single submission.

    Today: barrier waits for `seated_player_count()` (= 4) submissions and
    never fires from one. RED: this assertion fails because
    `_execute_narration_turn` is never called.
    """
    from sidequest.server.session_room import LobbyState  # type: ignore[attr-defined]

    handler, sd, room = session_handler_factory(
        slug="evropi-scenario",
        mode=GameMode.MULTIPLAYER,
        seat_players=[
            ("rux", "Rux"),
            ("prot_thokk", "ProtThokk"),
            ("hant", "Hant"),
            ("pumblestone", "Pumblestone"),
        ],
        active_player=("rux", "Rux"),
    )
    # Mark exactly one peer (rux) as PLAYING. The other three stay in their
    # default post-seat state (CHARGEN per spec). This is the evropi
    # situation collapsed to its load-bearing variables.
    room._seated["rux"].state = LobbyState.PLAYING  # noqa: SLF001
    # The other three explicitly marked CHARGEN (defensive — the conftest
    # fixture default may evolve, but the test contract is what we set).
    for pid in ("prot_thokk", "hant", "pumblestone"):
        room._seated[pid].state = LobbyState.CHARGEN  # noqa: SLF001

    captured: list[str] = []

    async def fake_execute(sd, action, turn_context):
        captured.append(action)
        return []

    handler._execute_narration_turn = fake_execute  # type: ignore[method-assign]

    # Rux submits — playing_player_count() == 1, barrier fires immediately.
    msg = PlayerActionMessage(
        payload=PlayerActionPayload(
            action=NonBlankString.model_validate("I check the door for traps"),
        ),
        player_id="rux",
    )
    await handler._handle_player_action(msg)

    # The narrator dispatched on Rux's single submission — the bug was that
    # the barrier waited on the 3 phantom peers. Today this assertion fails
    # because the barrier never fires.
    assert len(captured) == 1, (
        f"Barrier must fire on Rux's solo submission when 3 peers are still "
        f"in CHARGEN; instead the narrator was called {len(captured)} times. "
        f"This is the evropi mid-solo phantom-peer wait."
    )
    assert "Rux: I check the door for traps" in captured[0]


@pytest.mark.asyncio
async def test_barrier_does_not_fire_on_one_submission_when_all_are_playing(
    session_handler_factory,
) -> None:
    """Negative test (AC1): the same 4-peer room, but THIS time all 4 are
    PLAYING. One submission must NOT fire the barrier — the other three
    still owe their actions.

    Distinguishes correctness from the trivial "always fire on 1" failure.
    """
    from sidequest.server.session_room import LobbyState  # type: ignore[attr-defined]

    handler, sd, room = session_handler_factory(
        slug="all-playing",
        mode=GameMode.MULTIPLAYER,
        seat_players=[
            ("rux", "Rux"),
            ("prot_thokk", "ProtThokk"),
            ("hant", "Hant"),
            ("pumblestone", "Pumblestone"),
        ],
        active_player=("rux", "Rux"),
    )
    for pid in ("rux", "prot_thokk", "hant", "pumblestone"):
        room._seated[pid].state = LobbyState.PLAYING  # noqa: SLF001

    handler._execute_narration_turn = AsyncMock(  # type: ignore[method-assign]
        return_value=[],
    )

    msg = PlayerActionMessage(
        payload=PlayerActionPayload(
            action=NonBlankString.model_validate("I draw my sword"),
        ),
        player_id="rux",
    )
    result = await handler._handle_player_action(msg)

    # First-of-four, all playing → buffered, returns [], narrator not called.
    assert result == [], (
        "When 4 PLAYING peers are seated, one submission must buffer and "
        "return [] — barrier waits on the other three"
    )
    handler._execute_narration_turn.assert_not_called()


@pytest.mark.asyncio
async def test_barrier_fires_after_chargen_peers_abandon_via_disconnect(
    session_handler_factory,
) -> None:
    """End-to-end variant of the evropi scenario via the disconnect path:
    4 peers seated, 1 marked PLAYING, the other 3 explicitly DISCONNECT
    while in CHARGEN (so they transition to ABANDONED). Then PLAYING peer
    submits → barrier fires.

    This is fix dimension #4 (chargen-abandonment cancels the slot)
    exercised through the actual disconnect call site.
    """
    from sidequest.server.session_room import LobbyState  # type: ignore[attr-defined]

    handler, sd, room = session_handler_factory(
        slug="abandon-via-disconnect",
        mode=GameMode.MULTIPLAYER,
        seat_players=[
            ("rux", "Rux"),
            ("prot_thokk", "ProtThokk"),
            ("hant", "Hant"),
            ("pumblestone", "Pumblestone"),
        ],
        active_player=("rux", "Rux"),
    )
    # Test precondition: rux is PLAYING; the other three are in CHARGEN
    # (they connected and claimed seats but haven't committed characters).
    # The conftest fixture auto-promotes seated peers to PLAYING for
    # backward compat with existing post-chargen barrier tests, so we
    # roll the non-rux peers back to CHARGEN explicitly here — that is
    # the actual evropi situation we want to test.
    room._seated["rux"].state = LobbyState.PLAYING  # noqa: SLF001
    for pid in ("prot_thokk", "hant", "pumblestone"):
        room._seated[pid].state = LobbyState.CHARGEN  # noqa: SLF001
    # Now disconnect the three CHARGEN peers — sock ids are sock-0..sock-3
    # in fixture order.
    for sid_idx, pid in enumerate(("rux", "prot_thokk", "hant", "pumblestone")):
        if pid == "rux":
            continue
        # CHARGEN-state peer disconnects → seat moves to ABANDONED.
        room.disconnect(socket_id=f"sock-{sid_idx}")
        assert room._seated[pid].state == LobbyState.ABANDONED  # noqa: SLF001

    captured: list[str] = []

    async def fake_execute(sd, action, turn_context):
        captured.append(action)
        return []

    handler._execute_narration_turn = fake_execute  # type: ignore[method-assign]

    msg = PlayerActionMessage(
        payload=PlayerActionPayload(
            action=NonBlankString.model_validate("I push forward alone"),
        ),
        player_id="rux",
    )
    await handler._handle_player_action(msg)

    assert len(captured) == 1, (
        "After 3 CHARGEN peers abandon via disconnect, only Rux is PLAYING; "
        "his single submission must fire the barrier."
    )


# ---------------------------------------------------------------------------
# AC4 — barrier.wait OTEL span fires on every check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_barrier_wait_span_fires_when_barrier_does_not_fire(
    session_handler_factory,
) -> None:
    """`barrier.wait` must fire on EVERY barrier check, not just on
    `barrier_fired`. A wait that never fires is exactly the bug being
    fixed; if the span only emits on fire, the GM panel can't see why
    the wait persists. See AC4.

    Setup: 2 PLAYING peers. Player 1 submits → barrier check happens but
    barrier does NOT fire (still waiting on player 2). The
    `barrier.wait` span MUST still emit, with both counts.

    RED today: span does not exist.
    """
    from sidequest.server.session_room import LobbyState  # type: ignore[attr-defined]

    handler, sd, room = session_handler_factory(
        slug="wait-span-no-fire",
        mode=GameMode.MULTIPLAYER,
        seat_players=[("p1", "Gladstone"), ("p2", "Zanzibar")],
        active_player=("p1", "Gladstone"),
    )
    room._seated["p1"].state = LobbyState.PLAYING  # noqa: SLF001
    room._seated["p2"].state = LobbyState.PLAYING  # noqa: SLF001

    handler._execute_narration_turn = AsyncMock(  # type: ignore[method-assign]
        return_value=[],
    )

    with patch("sidequest.handlers.player_action._watcher_publish") as wp:
        msg = PlayerActionMessage(
            payload=PlayerActionPayload(
                action=NonBlankString.model_validate("I prepare for the dungeon"),
            ),
            player_id="p1",
        )
        await handler._handle_player_action(msg)

    event_names = [call.args[0] for call in wp.call_args_list]
    # Barrier did NOT fire (mp.barrier_fired must NOT appear).
    assert "mp.barrier_fired" not in event_names, (
        "Pre-condition: only 1 of 2 PLAYING peers submitted; barrier should "
        "still be in InputCollection"
    )
    # ...but barrier.wait MUST fire on this check.
    assert "barrier.wait" in event_names, (
        f"barrier.wait must fire on every barrier check, including waits "
        f"that don't fire. Captured events: {event_names}"
    )


@pytest.mark.asyncio
async def test_barrier_wait_span_carries_lobby_and_active_counts(
    session_handler_factory,
) -> None:
    """`barrier.wait` payload must carry both `lobby_participant_count` and
    `active_turn_count` (AC4 attributes). The whole point — Sebastien's
    lie-detector reads the divergence: if `lobby > active`, that's the
    phantom-peer story being told in real time.

    Story 45-2 spec (OTEL spans table): "lobby_participant_count (sum across
    all non-`abandoned` states), active_turn_count (= `playing` count)".
    The fixture intentionally includes a 5th seated peer who was disconnected
    mid-CHARGEN (state=ABANDONED) so the assertion catches the
    seated-vs-non-abandoned distinction:

      _seated dict size       = 5 (rux + prot_thokk + hant + pumblestone + flake)
      non-ABANDONED count     = 4 (excludes flake)
      PLAYING count           = 1 (rux only)

    Without the ABANDONED peer in the fixture, `lobby_participant_count == 4`
    is satisfied by EITHER the spec-correct "non-abandoned sum" OR the
    incorrect "raw seated sum" — the test cannot distinguish. Adding the
    5th ABANDONED peer pins the contract.
    """
    from sidequest.server.session_room import LobbyState  # type: ignore[attr-defined]

    handler, sd, room = session_handler_factory(
        slug="wait-span-counts",
        mode=GameMode.MULTIPLAYER,
        seat_players=[
            ("rux", "Rux"),
            ("prot_thokk", "ProtThokk"),
            ("hant", "Hant"),
            ("pumblestone", "Pumblestone"),
        ],
        active_player=("rux", "Rux"),
    )
    room._seated["rux"].state = LobbyState.PLAYING  # noqa: SLF001
    for pid in ("prot_thokk", "hant", "pumblestone"):
        room._seated[pid].state = LobbyState.CHARGEN  # noqa: SLF001

    # Add a 5th peer who was seated, entered chargen, and disconnected
    # mid-chargen — their seat is now ABANDONED. This is the load-bearing
    # case that distinguishes "non-abandoned sum" from "raw seated sum".
    # Use the public connect/seat/disconnect API so the state-machine
    # transition runs through production code paths.
    room.connect("flake", socket_id="sock-flake")
    room.seat("flake", character_slot="Flake")  # → CHARGEN
    room.disconnect(socket_id="sock-flake")  # CHARGEN → ABANDONED
    assert room._seated["flake"].state == LobbyState.ABANDONED, (  # noqa: SLF001
        "Test precondition: flake must be ABANDONED for this fixture to "
        "test the seated-vs-non-abandoned distinction"
    )

    handler._execute_narration_turn = AsyncMock(  # type: ignore[method-assign]
        return_value=[],
    )

    with patch("sidequest.handlers.player_action._watcher_publish") as wp:
        msg = PlayerActionMessage(
            payload=PlayerActionPayload(
                action=NonBlankString.model_validate("I check for traps"),
            ),
            player_id="rux",
        )
        await handler._handle_player_action(msg)

    # Find the barrier.wait call.
    wait_calls = [c for c in wp.call_args_list if c.args[0] == "barrier.wait"]
    assert wait_calls, f"barrier.wait must fire; captured {[c.args[0] for c in wp.call_args_list]}"
    # The payload (second positional arg) must carry both counts.
    payload = wait_calls[0].args[1]
    assert payload.get("lobby_participant_count") == 4, (
        f"lobby_participant_count must equal sum of non-ABANDONED peers "
        f"(4: rux PLAYING + 3 CHARGEN; flake ABANDONED is excluded); "
        f"got {payload}. If this is 5, the implementation is using raw "
        f"seated_player_count() which incorrectly counts ABANDONED slots."
    )
    assert payload.get("active_turn_count") == 1, (
        f"active_turn_count must equal playing peer count (1: rux only); got {payload}"
    )


@pytest.mark.asyncio
async def test_barrier_wait_span_fires_when_barrier_does_fire(
    session_handler_factory,
) -> None:
    """Companion to the no-fire test: `barrier.wait` must ALSO fire on the
    submission that flips the barrier. Both `barrier.wait` and
    `mp.barrier_fired` should appear in the same call sequence — wait is
    the per-check span, fired is the transition event.

    This pins the contract: wait fires on EVERY check (including the one
    where the barrier flips). The two events are not mutually exclusive.
    """
    from sidequest.server.session_room import LobbyState  # type: ignore[attr-defined]

    handler, sd, room = session_handler_factory(
        slug="wait-span-on-fire",
        mode=GameMode.MULTIPLAYER,
        seat_players=[("rux", "Rux")],
        active_player=("rux", "Rux"),
    )
    room._seated["rux"].state = LobbyState.PLAYING  # noqa: SLF001

    async def fake_execute(sd, action, turn_context):
        return []

    handler._execute_narration_turn = fake_execute  # type: ignore[method-assign]

    with patch("sidequest.handlers.player_action._watcher_publish") as wp:
        msg = PlayerActionMessage(
            payload=PlayerActionPayload(
                action=NonBlankString.model_validate("I open the door"),
            ),
            player_id="rux",
        )
        await handler._handle_player_action(msg)

    event_names = [call.args[0] for call in wp.call_args_list]
    # Both events fire — wait is per-check, fired is the transition.
    assert "barrier.wait" in event_names, (
        f"barrier.wait must fire even on the submission that flips the barrier; "
        f"captured={event_names}"
    )
    assert "mp.barrier_fired" in event_names, (
        f"Sanity: existing mp.barrier_fired event must continue to fire on "
        f"the transition; captured={event_names}"
    )


# ---------------------------------------------------------------------------
# AC1 / AC4 cross-event consistency — Sebastien's lie-detector pins
# the same number across barrier.wait, mp.barrier_fired, and mp.round_dispatched.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mp_round_dispatched_player_count_matches_barrier_predicate(
    session_handler_factory,
) -> None:
    """`mp.round_dispatched.player_count` must equal `barrier.wait.active_turn_count`
    for the same round — both are reading the same underlying invariant
    (how many PLAYING peers were active when the barrier evaluated).

    The Story 45-2 fix swapped the barrier predicate from `seated_player_count()`
    to `playing_player_count()`. The companion OTEL event `mp.barrier_fired`
    was correctly updated to report `playing_count`. But `mp.round_dispatched`
    fires immediately after — and if it still reports `seated_player_count()`,
    Sebastien's lie-detector reads two different numbers for the same round:
    `barrier.wait.active_turn_count=1` (correct) vs
    `mp.round_dispatched.player_count=4` (stale).

    Test scenario: 4 seated peers, 1 PLAYING (rux), 3 CHARGEN. Rux's
    submission fires the barrier. All three OTEL events for this round
    must report consistent counts:
      barrier.wait.active_turn_count == 1
      mp.barrier_fired.player_count == 1
      mp.round_dispatched.player_count == 1

    GREEN: Story 45-2 wired `mp.round_dispatched.player_count` to
    `playing_count` (the same value the barrier itself used) at
    `sidequest/handlers/player_action.py:356`. This test pins that
    invariant so a future regression — switching back to
    `seated_player_count()` or any other source — would surface here
    instead of silently telling the GM panel two different numbers
    about the same dispatch.
    """
    from sidequest.server.session_room import LobbyState  # type: ignore[attr-defined]

    handler, sd, room = session_handler_factory(
        slug="round-dispatched-consistency",
        mode=GameMode.MULTIPLAYER,
        seat_players=[
            ("rux", "Rux"),
            ("prot_thokk", "ProtThokk"),
            ("hant", "Hant"),
            ("pumblestone", "Pumblestone"),
        ],
        active_player=("rux", "Rux"),
    )
    room._seated["rux"].state = LobbyState.PLAYING  # noqa: SLF001
    for pid in ("prot_thokk", "hant", "pumblestone"):
        room._seated[pid].state = LobbyState.CHARGEN  # noqa: SLF001

    async def fake_execute(sd, action, turn_context):
        return []

    handler._execute_narration_turn = fake_execute  # type: ignore[method-assign]

    with patch("sidequest.handlers.player_action._watcher_publish") as wp:
        msg = PlayerActionMessage(
            payload=PlayerActionPayload(
                action=NonBlankString.model_validate("I press forward alone"),
            ),
            player_id="rux",
        )
        await handler._handle_player_action(msg)

    # Extract payloads for the three load-bearing events.
    wait_payload = next(
        (c.args[1] for c in wp.call_args_list if c.args[0] == "barrier.wait"),
        None,
    )
    fired_payload = next(
        (c.args[1] for c in wp.call_args_list if c.args[0] == "mp.barrier_fired"),
        None,
    )
    dispatched_payload = next(
        (c.args[1] for c in wp.call_args_list if c.args[0] == "mp.round_dispatched"),
        None,
    )

    assert wait_payload is not None, "barrier.wait must fire on the submission"
    assert fired_payload is not None, "mp.barrier_fired must fire when barrier flips"
    assert dispatched_payload is not None, (
        "mp.round_dispatched must fire when the elected dispatcher runs"
    )

    # Pin the cross-event invariant: every event reads from the same
    # post-Story-45-2 source of truth (playing_player_count).
    active_turn_count = wait_payload.get("active_turn_count")
    fired_count = fired_payload.get("player_count")
    dispatched_count = dispatched_payload.get("player_count")

    assert active_turn_count == 1, (
        f"barrier.wait.active_turn_count must be 1 (rux is sole PLAYING peer); "
        f"got {active_turn_count}"
    )
    assert fired_count == 1, (
        f"mp.barrier_fired.player_count must be 1 (already correct in code); got {fired_count}"
    )
    assert dispatched_count == 1, (
        f"mp.round_dispatched.player_count must equal "
        f"barrier.wait.active_turn_count for the same round. Got "
        f"{dispatched_count} (vs active_turn_count={active_turn_count}). "
        f"If this is 4, sidequest/handlers/player_action.py:356 has "
        f"regressed to reading seated_player_count() (or any source other "
        f"than `playing_count`) — the post-barrier OTEL event will tell "
        f"Sebastien a different number than the barrier itself just used."
    )
