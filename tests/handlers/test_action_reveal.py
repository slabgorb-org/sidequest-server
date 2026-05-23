"""Tests for ActionRevealHandler."""

import warnings
from unittest.mock import MagicMock

import pytest

from sidequest.handlers.action_reveal import HANDLER, ActionRevealHandler
from sidequest.protocol.messages import (
    ActionRevealMessage,
    ActionRevealPayload,
    ActionRevealStatus,
)
from sidequest.protocol.types import NonBlankString


def _make_session(player_id: str = "p1", socket_id: str = "s1", round: int = 7):
    session = MagicMock()
    session._room = MagicMock()
    session._room.broadcast = MagicMock(return_value=[])
    session._room.slug = "test-slug"
    session._socket_id = socket_id
    # Player identity lives on _session_data, not _player_id.
    session._session_data.player_id = player_id
    snapshot = MagicMock()
    snapshot.turn_manager.round = round
    session._room.snapshot = snapshot
    return session


def _make_msg(
    *,
    status: ActionRevealStatus,
    action: str = "I sneak around the back",
    seq: int = 0,
    round: int = 7,
    player_id: str = "p1",
    character_name: str = "Alex",
    aside: bool = False,
):
    payload = ActionRevealPayload(
        player_id=player_id,
        character_name=character_name,
        status=status,
        action=action,
        aside=aside,
        seq=seq,
        round=round,
    )
    return ActionRevealMessage(payload=payload, player_id=player_id)


@pytest.mark.asyncio
async def test_composing_is_broadcast_excluding_sender() -> None:
    handler = ActionRevealHandler()
    session = _make_session()
    msg = _make_msg(status=ActionRevealStatus.COMPOSING, action="I creep", seq=1)

    result = await handler.handle(session, msg)

    assert result == []
    session._room.broadcast.assert_called_once()
    sent_msg = session._room.broadcast.call_args.args[0]
    kwargs = session._room.broadcast.call_args.kwargs
    assert kwargs["exclude_socket_id"] == "s1"
    assert sent_msg.payload.status == ActionRevealStatus.COMPOSING
    assert sent_msg.payload.action == "I creep"


@pytest.mark.asyncio
async def test_submitted_is_broadcast() -> None:
    handler = ActionRevealHandler()
    session = _make_session()
    msg = _make_msg(status=ActionRevealStatus.SUBMITTED, action="I draw my sword", seq=5)

    await handler.handle(session, msg)

    session._room.broadcast.assert_called_once()
    sent_msg = session._room.broadcast.call_args.args[0]
    assert sent_msg.payload.status == ActionRevealStatus.SUBMITTED


@pytest.mark.asyncio
async def test_server_stamps_round_authoritative() -> None:
    """Client-supplied round is overwritten by snapshot.turn_manager.round."""
    handler = ActionRevealHandler()
    session = _make_session(round=42)
    # Client claims round=99 — server must overwrite to 42
    msg = _make_msg(status=ActionRevealStatus.COMPOSING, round=99, seq=0)

    await handler.handle(session, msg)

    sent_msg = session._room.broadcast.call_args.args[0]
    assert sent_msg.payload.round == 42


@pytest.mark.asyncio
async def test_server_stamps_player_id_authoritative() -> None:
    """Client-supplied player_id in payload is overwritten by session_data.player_id."""
    handler = ActionRevealHandler()
    session = _make_session(player_id="real-player")
    # Client lies about player_id — server overwrites
    msg = _make_msg(
        status=ActionRevealStatus.COMPOSING,
        player_id="fake-player",
        seq=0,
    )

    await handler.handle(session, msg)

    sent_msg = session._room.broadcast.call_args.args[0]
    # The stamped player_id is the validated NonBlankString newtype (the
    # handler wraps at the model_copy boundary, consistent with the
    # constructor paths). Comparing to NonBlankString asserts both the
    # authoritative-overwrite value AND that the type contract holds —
    # a raw-str regression here would now also fail this assertion.
    assert sent_msg.payload.player_id == NonBlankString("real-player")


# ---------------------------------------------------------------------------
# player_id type-contract on the stamped broadcast
# (sq-playtest 2026-05-17 / [BS-BUG-LOW])
# ---------------------------------------------------------------------------
#
# Beneath Sünden 3-player MP logged, once per player every turn:
#   PydanticSerializationUnexpectedValue(Expected `NonBlankString` ...
#     [field_name='player_id', input_value='Carl', input_type=str])
# Root cause: the handler stamps the authoritative player_id via
# ``payload.model_copy(update={"player_id": sd.player_id})``. Pydantic-v2
# ``model_copy(update=)`` bypasses validation by design, so the raw ``str``
# is never coerced into the field's ``NonBlankString`` newtype; the type
# contract is violated and ``model_dump_json()`` (the exact call at
# websocket.py:160) warns on every broadcast.


@pytest.mark.asyncio
async def test_stamped_player_id_serializes_without_pydantic_warning() -> None:
    """The stamped, broadcast ActionRevealMessage must honor the
    NonBlankString contract on payload.player_id so the real wire
    serialization (model_dump_json) emits no type-mismatch warning.
    """
    handler = ActionRevealHandler()
    session = _make_session(player_id="Carl")  # _SessionData.player_id is a raw str
    msg = _make_msg(status=ActionRevealStatus.SUBMITTED, player_id="seed", seq=1)

    await handler.handle(session, msg)

    sent = session._room.broadcast.call_args.args[0]
    # Direct contract: the stamped value must be the validated newtype,
    # not a raw str that merely happens to be non-blank.
    assert isinstance(sent.payload.player_id, NonBlankString), (
        f"stamped payload.player_id is {type(sent.payload.player_id).__name__}, "
        f"expected NonBlankString — model_copy(update=) bypassed validation"
    )
    # Observable effect: the real wire path (websocket.py:160) must not
    # warn. pytest.warns can't express 'no warning'; capture explicitly.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        sent.model_dump_json()
    offending = [
        str(w.message)
        for w in caught
        if "PydanticSerializationUnexpectedValue" in str(w.message)
        and "player_id" in str(w.message)
    ]
    assert not offending, f"player_id serialized with a Pydantic type-contract warning: {offending}"


@pytest.mark.asyncio
async def test_no_session_data_drops_silently() -> None:
    handler = ActionRevealHandler()
    session = _make_session()
    session._session_data = None  # not authenticated yet
    msg = _make_msg(status=ActionRevealStatus.COMPOSING, action="x", seq=0)

    result = await handler.handle(session, msg)

    assert result == []
    session._room.broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_unbound_snapshot_drops_with_warning(caplog) -> None:
    handler = ActionRevealHandler()
    session = _make_session()
    session._room.snapshot = None  # room not bound yet
    msg = _make_msg(status=ActionRevealStatus.COMPOSING, action="x", seq=0)

    with caplog.at_level("WARNING"):
        result = await handler.handle(session, msg)

    assert result == []
    session._room.broadcast.assert_not_called()
    assert any("not bound" in r.message or "before room bound" in r.message for r in caplog.records)


def test_module_exports_handler_singleton() -> None:
    assert isinstance(HANDLER, ActionRevealHandler)


@pytest.mark.asyncio
async def test_client_cleared_is_silently_dropped() -> None:
    handler = ActionRevealHandler()
    session = _make_session()
    msg = _make_msg(status=ActionRevealStatus.CLEARED, action="", seq=0)

    result = await handler.handle(session, msg)

    assert result == []
    session._room.broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_stale_seq_dropped_in_same_round() -> None:
    handler = ActionRevealHandler()
    session = _make_session(round=7)
    first = _make_msg(status=ActionRevealStatus.COMPOSING, action="abc", seq=5)
    stale = _make_msg(status=ActionRevealStatus.COMPOSING, action="ab", seq=3)

    await handler.handle(session, first)
    await handler.handle(session, stale)

    assert session._room.broadcast.call_count == 1


@pytest.mark.asyncio
async def test_equal_seq_dropped_in_same_round() -> None:
    """seq must be strictly greater than last seen for the same (socket, round)."""
    handler = ActionRevealHandler()
    session = _make_session(round=7)
    first = _make_msg(status=ActionRevealStatus.COMPOSING, action="abc", seq=5)
    same = _make_msg(status=ActionRevealStatus.COMPOSING, action="abcd", seq=5)

    await handler.handle(session, first)
    await handler.handle(session, same)

    assert session._room.broadcast.call_count == 1


@pytest.mark.asyncio
async def test_seq_resets_on_new_round() -> None:
    handler = ActionRevealHandler()
    session = _make_session(round=7)
    first = _make_msg(status=ActionRevealStatus.COMPOSING, action="abc", seq=5)
    await handler.handle(session, first)

    # Advance the snapshot's round.
    snapshot = session._room.snapshot
    snapshot.turn_manager.round = 8
    new_round = _make_msg(status=ActionRevealStatus.COMPOSING, action="x", seq=0, round=8)
    await handler.handle(session, new_round)

    assert session._room.broadcast.call_count == 2


@pytest.mark.asyncio
async def test_rate_limit_drops_too_fast_composing(monkeypatch) -> None:
    handler = ActionRevealHandler()
    session = _make_session()
    fake_now = [1000.0]
    monkeypatch.setattr(
        "sidequest.handlers.action_reveal.time.monotonic",
        lambda: fake_now[0],
    )

    await handler.handle(session, _make_msg(status=ActionRevealStatus.COMPOSING, seq=0))
    fake_now[0] = 1000.05  # 50ms — under 100ms floor
    await handler.handle(session, _make_msg(status=ActionRevealStatus.COMPOSING, seq=1))
    fake_now[0] = 1000.20  # 200ms total — past floor
    await handler.handle(session, _make_msg(status=ActionRevealStatus.COMPOSING, seq=2))

    assert session._room.broadcast.call_count == 2  # first + third


@pytest.mark.asyncio
async def test_submitted_bypasses_rate_limit(monkeypatch) -> None:
    """Submitted is a discrete event — never throttled."""
    handler = ActionRevealHandler()
    session = _make_session()
    fake_now = [1000.0]
    monkeypatch.setattr(
        "sidequest.handlers.action_reveal.time.monotonic",
        lambda: fake_now[0],
    )

    await handler.handle(session, _make_msg(status=ActionRevealStatus.COMPOSING, seq=0))
    fake_now[0] = 1000.01  # 10ms later — would be throttled if it were composing
    await handler.handle(session, _make_msg(status=ActionRevealStatus.SUBMITTED, seq=1))

    assert session._room.broadcast.call_count == 2


@pytest.mark.asyncio
async def test_rate_limit_counter_per_socket_independent() -> None:
    """Two different sockets each have their own 100ms floor — one's rate limit doesn't suppress the other."""
    handler = ActionRevealHandler()
    session_a = _make_session(player_id="pa", socket_id="sa")
    session_b = _make_session(player_id="pb", socket_id="sb")

    await handler.handle(session_a, _make_msg(status=ActionRevealStatus.COMPOSING, seq=0))
    await handler.handle(session_b, _make_msg(status=ActionRevealStatus.COMPOSING, seq=0))

    session_a._room.broadcast.assert_called_once()
    session_b._room.broadcast.assert_called_once()


@pytest.mark.asyncio
async def test_rate_limit_clears_on_round_advance(monkeypatch) -> None:
    """First composing event of a new round is never throttled by the prior round's timestamp."""
    handler = ActionRevealHandler()
    session = _make_session(round=7)
    fake_now = [1000.0]
    monkeypatch.setattr(
        "sidequest.handlers.action_reveal.time.monotonic",
        lambda: fake_now[0],
    )

    await handler.handle(session, _make_msg(status=ActionRevealStatus.COMPOSING, seq=0))
    # Advance round — time has NOT advanced past the floor.
    snapshot = session._room.snapshot
    snapshot.turn_manager.round = 8
    # Same timestamp — would be throttled if rate-limit was not cleared.
    await handler.handle(
        session,
        _make_msg(status=ActionRevealStatus.COMPOSING, seq=0, round=8),
    )

    assert session._room.broadcast.call_count == 2
