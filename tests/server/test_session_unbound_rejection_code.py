"""Regression tests for playtest 2026-04-30 — uvicorn ``--reload``
zombies session binding.

When uvicorn auto-reloads on a Python file change (or `git pull`),
the existing handler is destroyed and a fresh handler instance is
created for the auto-reconnecting WebSocket. The fresh handler is in
``_State.AwaitingConnect`` until SESSION_EVENT{connect} re-binds it.

Pre-fix: any non-SESSION_EVENT message in this state was rejected
with a generic ERROR ("not connected" / "not in Playing state"). The
client showed a transient error toast but had no protocol-level
signal that the right recovery was to re-fire SESSION_EVENT{connect}.
Player saw three different error messages depending on the action:
- DICE_THROW: "not in Playing state"
- PLAYER_ACTION: "not connected"
- YIELD: "not in Playing state"
…all symptoms of the same root cause.

Fix: tag the rejection with ``code="session_unbound"`` and log INFO
``session.message_rejected_unbound``. The client detects the code and
auto-re-fires SESSION_EVENT{connect} from the saved slug — protocol-
level recovery that works regardless of why the rebind didn't fire on
its own (effect-timing race, message drop during close-then-reopen,
React StrictMode double-mount).

Tests cover all three handlers exercising the rejection path.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
from sidequest.protocol.enums import MessageType
from sidequest.protocol.messages import (
    DiceThrowMessage,
    PlayerActionMessage,
    PlayerActionPayload,
    YieldMessage,
)
from sidequest.protocol.types import NonBlankString
from sidequest.server.session_handler import _State


def _unbound_session():
    """Fresh handler — never received SESSION_EVENT{connect}."""
    session = MagicMock()
    session._state = _State.AwaitingConnect
    session._session_data = None
    return session


@pytest.mark.asyncio
async def test_player_action_in_awaiting_connect_tags_session_unbound(caplog):
    from sidequest.handlers.player_action import HANDLER

    session = _unbound_session()
    msg = PlayerActionMessage(
        type=MessageType.PLAYER_ACTION,
        payload=PlayerActionPayload(action=NonBlankString("look around")),
        player_id="p1",
    )

    with caplog.at_level("INFO"):
        outbound = await HANDLER.handle(session, msg)

    assert len(outbound) == 1
    assert outbound[0].type == "ERROR"
    assert outbound[0].payload.code == "session_unbound", (
        "PLAYER_ACTION rejected in AwaitingConnect must carry "
        "code=session_unbound so the client can auto-recover by "
        "re-firing SESSION_EVENT{connect}"
    )
    assert any(
        "session.message_rejected_unbound" in r.message and "PLAYER_ACTION" in r.message
        for r in caplog.records
    ), "missing INFO log for grep-able recovery trail"


@pytest.mark.asyncio
async def test_dice_throw_in_awaiting_connect_tags_session_unbound(caplog):
    from sidequest.handlers.dice_throw import HANDLER

    session = _unbound_session()
    msg = DiceThrowMessage(
        type=MessageType.DICE_THROW,
        payload=DiceThrowPayload(
            request_id="req-1",
            throw_params=ThrowParams(
                velocity=(0.0, 0.0, 0.0),
                angular=(0.0, 0.0, 0.0),
                position=(0.0, 0.0),
            ),
            face=[6, 6, 6],
            beat_id="attack",
        ),
        player_id="p1",
    )

    with caplog.at_level("INFO"):
        outbound = await HANDLER.handle(session, msg)

    assert len(outbound) == 1
    assert outbound[0].type == "ERROR"
    assert outbound[0].payload.code == "session_unbound"
    assert any(
        "session.message_rejected_unbound" in r.message and "DICE_THROW" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_yield_in_awaiting_connect_tags_session_unbound(caplog):
    from sidequest.handlers.yield_action import HANDLER

    session = _unbound_session()
    msg = YieldMessage(
        type=MessageType.YIELD,
        payload={},
        player_id="p1",
    )

    with caplog.at_level("INFO"):
        outbound = await HANDLER.handle(session, msg)

    assert len(outbound) == 1
    assert outbound[0].type == "ERROR"
    assert outbound[0].payload.code == "session_unbound"
    assert any(
        "session.message_rejected_unbound" in r.message and "YIELD" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_player_action_in_creating_state_does_not_tag_session_unbound():
    """Defensive: ``_State.Creating`` is a valid state for PLAYER_ACTION
    (chargen-time decisions). Don't trigger the auto-recovery flow for
    this — the client is correctly mid-chargen.
    """
    from sidequest.handlers.player_action import HANDLER

    session = MagicMock()
    session._state = _State.Creating
    session._session_data = None  # data missing — different rejection path

    msg = PlayerActionMessage(
        type=MessageType.PLAYER_ACTION,
        payload=PlayerActionPayload(action=NonBlankString("test")),
        player_id="p1",
    )
    outbound = await HANDLER.handle(session, msg)

    assert len(outbound) == 1
    assert outbound[0].type == "ERROR"
    # The "Internal error: session data missing" rejection has no code —
    # it's a different problem class (server-internal, not unbound).
    # session_unbound is reserved for the AwaitingConnect specifically.
    assert outbound[0].payload.code != "session_unbound", (
        "session_unbound must NOT tag rejections that aren't the "
        "AwaitingConnect-state recovery case — it's a specific signal"
    )
