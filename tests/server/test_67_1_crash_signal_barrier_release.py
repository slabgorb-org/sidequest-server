"""RED tests for Story 67-1 — a GameBoard render crash must not orphan the
table's turn.

## The bug (confirmed in code, 2026-05-26)

A client-side GameBoard React render crash is caught by the App-level
``<ErrorBoundary name="Game">`` (sidequest-ui/src/App.tsx:2019) WITHOUT
closing the WebSocket — the socket lives in the App component
(App.tsx:1126), above the boundary subtree, so it survives the crash.
Server-side the crashed player therefore stays ``PLAYING``
(session_room.py:645-660 ``playing_player_count``) and the submit-and-wait
barrier (turn.py:90-98 ``submit_input``; player_action.py:509-513) keeps
awaiting their submission **forever**. There is no timeout, heartbeat, or
abandonment path anywhere in ``sidequest/server/`` to release it:
``room.disconnect()`` never touches ``_submitted``/``player_count``, and a
render crash produces neither a submission nor a socket close. The whole
table's in-flight turn is orphaned.

## The chosen fix (Keith, 2026-05-26): client crash-signal

The crashed client's ErrorBoundary sends an explicit ``CLIENT_ERROR``
message over the still-open socket before showing its recovery UI. The
server drops that player from the barrier denominator **for the current
interaction** and re-evaluates the barrier; if the remaining submitters
satisfy it, the turn dispatches. This cleanly distinguishes a *crashed
client* from *a slow typist still composing* (CLAUDE.md: never rush a slow
typist) — the barrier is released ONLY on an explicit crash signal, never
on mere absence of a submission.

## Planned API this suite asserts (RED until Dev — Ponder — builds it)

1. ``MessageType.CLIENT_ERROR = "CLIENT_ERROR"`` (sidequest/protocol/enums.py).
2. ``ClientErrorMessage`` + ``ClientErrorPayload`` (sidequest/protocol/messages.py).
   Payload carries at least ``reason`` (e.g. "render_crash") and ``component``
   (e.g. "GameBoard"); the message carries ``player_id`` = who crashed.
3. A handler registered in ``WebSocketSessionHandler._MESSAGE_HANDLERS`` under
   the key ``"CLIENT_ERROR"`` (sidequest/handlers/client_error.py with a
   module-level ``HANDLER`` singleton, mirroring the other handlers).
4. Crash-release semantics: a ``CLIENT_ERROR`` for a player who has NOT yet
   submitted drops them from the awaited count for this interaction and
   dispatches the turn with the remaining submitters.
5. OTEL: a ``mp.player_crash_released`` watcher event fires (component
   "multiplayer") carrying ``slug``, ``player_id``, ``reason``; ``mp.barrier_fired``
   still fires on the release-driven dispatch (GM panel is the lie detector).

Test conventions mirror ``test_mp_turn_barrier_active_turn_count.py`` —
``session_handler_factory`` fixture, ``LobbyState``, ``_watcher_publish``
patch for span assertions, ``fake_execute`` stub for ``_execute_narration_turn``.
Planned-API references live INSIDE test bodies so each test fails on its own
(per-test ImportError/AttributeError), not at collection time.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from sidequest.game.persistence import GameMode
from sidequest.protocol.messages import PlayerActionMessage, PlayerActionPayload
from sidequest.protocol.types import NonBlankString


def _build_crash_message(player_id: str, *, reason: str = "render_crash", component: str = "GameBoard"):
    """Construct the planned CLIENT_ERROR message.

    Imported inside the helper so a missing protocol class raises inside the
    calling test (per-test RED), not at module collection.
    """
    from sidequest.protocol.messages import (  # type: ignore[attr-defined]
        ClientErrorMessage,
        ClientErrorPayload,
    )

    return ClientErrorMessage(
        payload=ClientErrorPayload(reason=reason, component=component),
        player_id=player_id,
    )


# ---------------------------------------------------------------------------
# Protocol + wiring
# ---------------------------------------------------------------------------


def test_client_error_message_type_exists() -> None:
    """``MessageType.CLIENT_ERROR`` must exist with wire value "CLIENT_ERROR".

    RED today: the enum has no CLIENT_ERROR member.
    """
    from sidequest.protocol.enums import MessageType

    assert hasattr(MessageType, "CLIENT_ERROR"), (
        "Story 67-1 needs a CLIENT_ERROR message type so a crashed client can "
        "signal its render failure over the still-open socket."
    )
    assert MessageType.CLIENT_ERROR.value == "CLIENT_ERROR"


@pytest.mark.asyncio
async def test_client_error_handler_is_registered(session_handler_factory) -> None:
    """A handler must be wired into the ``_MESSAGE_HANDLERS`` registry under
    "CLIENT_ERROR" so ``handle_message`` routes a crash signal instead of
    returning the "Unsupported message type" error.

    Registry-membership assertion (not a source-text grep) — refactor-stable
    per the server CLAUDE.md "No Source-Text Wiring Tests" rule.

    RED today: ``_message_handler_for("CLIENT_ERROR")`` returns None.
    """
    handler, _sd, _room = session_handler_factory(
        slug="crash-wiring",
        mode=GameMode.MULTIPLAYER,
        seat_players=[("p1", "Gladstone"), ("p2", "Zanzibar")],
        active_player=("p1", "Gladstone"),
    )
    assert type(handler)._message_handler_for("CLIENT_ERROR") is not None, (
        "CLIENT_ERROR must be registered in _MESSAGE_HANDLERS so a render-crash "
        "signal reaches its handler from the production dispatch path."
    )


# ---------------------------------------------------------------------------
# Core behavior — crash signal releases the barrier and dispatches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crash_signal_releases_barrier_and_dispatches_remaining(
    session_handler_factory,
) -> None:
    """The killer test. 2 PLAYING peers. p1 submits (barrier waits on p2).
    p2's GameBoard crashes and sends CLIENT_ERROR (socket still open). The
    barrier must release p2 from the awaited count and dispatch the turn with
    p1's submission — the table is NOT orphaned.

    RED today: there is no path that releases the barrier on a crash signal;
    ``_execute_narration_turn`` is never called and the turn hangs forever.
    """
    from sidequest.server.session_room import LobbyState  # type: ignore[attr-defined]

    handler, _sd, room = session_handler_factory(
        slug="crash-release-dispatch",
        mode=GameMode.MULTIPLAYER,
        seat_players=[("p1", "Gladstone"), ("p2", "Zanzibar")],
        active_player=("p1", "Gladstone"),
    )
    room._seated["p1"].state = LobbyState.PLAYING  # noqa: SLF001
    room._seated["p2"].state = LobbyState.PLAYING  # noqa: SLF001

    captured: list[str] = []

    async def fake_execute(sd, action, turn_context):
        captured.append(action)
        return []

    handler._execute_narration_turn = fake_execute  # type: ignore[method-assign]

    # p1 submits — barrier waits on p2 (1 of 2 PLAYING submitted).
    submit = PlayerActionMessage(
        payload=PlayerActionPayload(
            action=NonBlankString.model_validate("I bar the door"),
        ),
        player_id="p1",
    )
    result = await handler._handle_player_action(submit)
    assert result == [], "Precondition: p1's submission must buffer (barrier waits on p2)"
    assert captured == [], "Precondition: narrator must NOT have fired yet"

    # p2's GameBoard crashes → CLIENT_ERROR over the still-open socket.
    crash = _build_crash_message("p2")
    await handler.handle_message(crash)

    assert len(captured) == 1, (
        f"After p2's crash signal, the barrier must release p2 and dispatch "
        f"the turn with p1's submission. The narrator was called "
        f"{len(captured)} times — the table's turn is orphaned."
    )
    assert "I bar the door" in captured[0], (
        "The dispatched turn must carry p1's submitted action."
    )


@pytest.mark.asyncio
async def test_crash_release_emits_otel_spans(session_handler_factory) -> None:
    """The crash-release dispatch must emit ``mp.player_crash_released`` (the
    new lie-detector span) AND the existing ``mp.barrier_fired``. Per the OTEL
    Observability Principle, a barrier released with no span is indistinguishable
    from the narrator winging it.

    RED today: neither the release path nor its span exists.
    """
    from sidequest.server.session_room import LobbyState  # type: ignore[attr-defined]

    handler, _sd, room = session_handler_factory(
        slug="crash-release-otel",
        mode=GameMode.MULTIPLAYER,
        seat_players=[("p1", "Gladstone"), ("p2", "Zanzibar")],
        active_player=("p1", "Gladstone"),
    )
    room._seated["p1"].state = LobbyState.PLAYING  # noqa: SLF001
    room._seated["p2"].state = LobbyState.PLAYING  # noqa: SLF001

    handler._execute_narration_turn = AsyncMock(return_value=[])  # type: ignore[method-assign]

    await handler._handle_player_action(
        PlayerActionMessage(
            payload=PlayerActionPayload(action=NonBlankString.model_validate("I wait")),
            player_id="p1",
        )
    )

    # Patch the watcher at the crash handler's module. Dev: emit the
    # release span from sidequest/handlers/client_error.py via the same
    # _watcher_publish helper the barrier path uses.
    with patch("sidequest.handlers.client_error._watcher_publish") as wp:  # type: ignore[attr-defined]
        await handler.handle_message(_build_crash_message("p2"))

    event_names = [call.args[0] for call in wp.call_args_list]
    assert "mp.player_crash_released" in event_names, (
        f"A crash-release must emit mp.player_crash_released for the GM panel. "
        f"Captured: {event_names}"
    )
    # The release span must identify who crashed and why.
    release_payload = next(
        c.args[1] for c in wp.call_args_list if c.args[0] == "mp.player_crash_released"
    )
    assert release_payload.get("player_id") == "p2"
    assert release_payload.get("reason") == "render_crash"


# ---------------------------------------------------------------------------
# Slow-typist safety — the regression guard that protects Alex
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_barrier_does_not_release_without_a_crash_signal(
    session_handler_factory,
) -> None:
    """THE load-bearing safety guard. A player who has neither submitted nor
    crashed (Alex, slowly composing) must keep the barrier waiting. The fix
    must release the barrier ONLY on an explicit crash signal — never on mere
    absence of a submission.

    This passes today (the barrier already waits) and MUST keep passing after
    the fix. If the crash-release logic ever degrades into a timeout/liveness
    sweep that drops a quiet-but-present player, this test fails — which is
    exactly the CLAUDE.md "never rush a slow typist" violation we must catch.
    """
    from sidequest.server.session_room import LobbyState  # type: ignore[attr-defined]

    handler, _sd, room = session_handler_factory(
        slug="slow-typist-guard",
        mode=GameMode.MULTIPLAYER,
        seat_players=[("p1", "Gladstone"), ("p2", "Alex")],
        active_player=("p1", "Gladstone"),
    )
    room._seated["p1"].state = LobbyState.PLAYING  # noqa: SLF001
    room._seated["p2"].state = LobbyState.PLAYING  # noqa: SLF001

    handler._execute_narration_turn = AsyncMock(return_value=[])  # type: ignore[method-assign]

    # p1 submits; p2 (Alex) is still composing — no submit, no crash.
    result = await handler._handle_player_action(
        PlayerActionMessage(
            payload=PlayerActionPayload(
                action=NonBlankString.model_validate("I scan the room"),
            ),
            player_id="p1",
        )
    )

    assert result == [], "p1's submission buffers; the barrier must wait on Alex"
    handler._execute_narration_turn.assert_not_called()
    assert room.snapshot.turn_manager.get_phase().name == "InputCollection", (
        "With no crash signal, the barrier must remain in InputCollection — a "
        "slow typist who is present but quiet is never dropped."
    )


# ---------------------------------------------------------------------------
# Edges — idempotency / already-submitted / solo
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crash_signal_for_already_submitted_player_is_noop(
    session_handler_factory,
) -> None:
    """A crash signal for a player who ALREADY submitted must not fire the
    barrier or double-dispatch — the still-awaited peer (p2) keeps the barrier
    open.

    Scenario: p1 submits, then p1's client crashes. p2 has not submitted. The
    crash of an already-submitted player changes nothing about the awaited set,
    so the barrier must stay in InputCollection.

    RED today: the crash path does not exist.
    """
    from sidequest.server.session_room import LobbyState  # type: ignore[attr-defined]

    handler, _sd, room = session_handler_factory(
        slug="crash-already-submitted",
        mode=GameMode.MULTIPLAYER,
        seat_players=[("p1", "Gladstone"), ("p2", "Zanzibar")],
        active_player=("p1", "Gladstone"),
    )
    room._seated["p1"].state = LobbyState.PLAYING  # noqa: SLF001
    room._seated["p2"].state = LobbyState.PLAYING  # noqa: SLF001

    handler._execute_narration_turn = AsyncMock(return_value=[])  # type: ignore[method-assign]

    await handler._handle_player_action(
        PlayerActionMessage(
            payload=PlayerActionPayload(action=NonBlankString.model_validate("I submit first")),
            player_id="p1",
        )
    )
    # p1 (already submitted) crashes — must NOT release the barrier on p2's behalf.
    await handler.handle_message(_build_crash_message("p1"))

    handler._execute_narration_turn.assert_not_called()
    assert room.snapshot.turn_manager.get_phase().name == "InputCollection", (
        "Crash of an already-submitted player must not fire the barrier while "
        "another PLAYING peer still owes a submission."
    )


@pytest.mark.asyncio
async def test_crash_signal_solo_no_pending_turn_is_safe_noop(
    session_handler_factory,
) -> None:
    """A crash signal in a solo session with no pending turn must be a safe
    no-op: no exception, no spurious narration dispatch.

    RED today: no handler exists, so ``handle_message`` returns an
    "Unsupported message type" error rather than a clean no-op.
    """
    from sidequest.server.session_room import LobbyState  # type: ignore[attr-defined]

    handler, _sd, room = session_handler_factory(
        slug="crash-solo-noop",
        mode=GameMode.MULTIPLAYER,
        seat_players=[("p1", "Gladstone")],
        active_player=("p1", "Gladstone"),
    )
    room._seated["p1"].state = LobbyState.PLAYING  # noqa: SLF001

    handler._execute_narration_turn = AsyncMock(return_value=[])  # type: ignore[method-assign]

    # No pending action; the sole player's client crashes.
    out = await handler.handle_message(_build_crash_message("p1"))

    handler._execute_narration_turn.assert_not_called()
    # Must not surface the generic unsupported-type error to the client.
    assert not any(
        getattr(getattr(m, "payload", None), "message", "") == ""
        and getattr(m, "type", "") == "ERROR"
        for m in out
    ), "A CLIENT_ERROR in a solo idle session must be handled cleanly, not error back."


# ---------------------------------------------------------------------------
# Perception firewall (ADR-104/105) — crashed player's draft must not leak
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crash_release_dispatch_excludes_crashed_players_content(
    session_handler_factory,
) -> None:
    """When the crash releases the barrier and dispatches, the crashed player
    (who never submitted) contributes NO action text to the dispatched turn.
    The turn carries only the submitters' actions — no phantom content from a
    player who crashed mid-compose (ADR-104/105 firewall, applied to the
    release path).

    RED today: release path does not exist.
    """
    from sidequest.server.session_room import LobbyState  # type: ignore[attr-defined]

    handler, _sd, room = session_handler_factory(
        slug="crash-firewall",
        mode=GameMode.MULTIPLAYER,
        seat_players=[("p1", "Gladstone"), ("p2", "Zanzibar")],
        active_player=("p1", "Gladstone"),
    )
    room._seated["p1"].state = LobbyState.PLAYING  # noqa: SLF001
    room._seated["p2"].state = LobbyState.PLAYING  # noqa: SLF001

    captured: list[str] = []

    async def fake_execute(sd, action, turn_context):
        captured.append(action)
        return []

    handler._execute_narration_turn = fake_execute  # type: ignore[method-assign]

    await handler._handle_player_action(
        PlayerActionMessage(
            payload=PlayerActionPayload(
                action=NonBlankString.model_validate("I light the lantern"),
            ),
            player_id="p1",
        )
    )
    await handler.handle_message(_build_crash_message("p2"))

    assert len(captured) == 1, "Crash must release the barrier and dispatch once"
    assert "Zanzibar" not in captured[0], (
        "The crashed player (Zanzibar) never submitted; their name/content must "
        "not appear in the dispatched turn — no phantom action from a crash."
    )
    assert "I light the lantern" in captured[0]
