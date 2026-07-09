"""165-3 REWORK (ADR-096 v2, Track C2) — the DICE_THROW handler must resolve the
dungeon store the way production actually populates it.

Reviewer Critical #1: the reach gate shipped DEAD because
``DiceThrowHandler.handle`` passed
``dungeon_store=getattr(sd, "dungeon_store", None)`` — but ``_SessionData`` has NO
``dungeon_store`` field and prod never assigns one. The real store lives on
``sd.lookahead_handle.persistence`` (the same resolution
``websocket_session_handler.py:986`` uses for the movement subsystem). So the mask
always resolved ``None`` → the gate always took the ``no_grid`` skip → C1's
enforcement never engaged on a real strike. 1834 tests were green because every
tactical test either called ``dispatch_dice_throw`` directly (skipping this
handler) or set ``sd.dungeon_store`` by hand (the attribute prod never writes).

This is the wiring test done right: it drives the REAL handler path with a REAL
store attached the production way (``lookahead_handle.persistence``) and asserts
the store actually reaches ``dispatch_dice_throw``. It must FAIL on the dead
``getattr`` (captures ``None``) and PASS once the handler resolves via
``lookahead_handle``.

Why not a MagicMock ``sd``: a bare ``MagicMock`` auto-creates a *truthy*
``sd.dungeon_store``, which would HIDE the very bug under test. A real
``_SessionData`` (which has no such field) is load-bearing here — ``getattr``
returns ``None``, reproducing prod.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.server.tactical_emit_fixtures import build_sd_with_tactical_region


class _StopAtDispatch(Exception):
    """Sentinel raised by the dispatch spy to stop handle() at the call site —
    everything downstream of dispatch is irrelevant to store resolution."""


def _dice_msg():
    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.protocol.enums import MessageType
    from sidequest.protocol.messages import DiceThrowMessage

    return DiceThrowMessage(
        type=MessageType.DICE_THROW,
        payload=DiceThrowPayload(
            request_id="req-165-3-store-resolution",
            throw_params=ThrowParams(
                velocity=(0.0, 5.0, -2.0),
                angular=(1.0, 1.0, 1.0),
                position=(0.5, 0.5),
            ),
            face=[15],
            beat_id="attack",
        ),
        player_id="",  # empty -> no spoof branch; handler uses sd.player_id
    )


def test_session_data_has_no_dungeon_store_field():
    """Direction-lock (CLAUDE.md blessed reflection tripwire): ``_SessionData`` must
    NOT carry a ``dungeon_store`` field. This is WHY ``getattr(sd, "dungeon_store",
    None)`` is structurally dead in prod, and it fences off the WRONG fix — adding
    a field to ``_SessionData`` instead of resolving from ``lookahead_handle``.
    The store belongs to the lookahead handle; the session data must not grow a
    shadow copy."""
    from sidequest.server.session_handler import _SessionData

    field_names = {f.name for f in dataclasses.fields(_SessionData)}
    assert "dungeon_store" not in field_names, (
        "_SessionData grew a 'dungeon_store' field — the reach-gate fix must "
        "resolve the store from sd.lookahead_handle.persistence (as "
        "websocket_session_handler.py:986 does), NOT by shadowing it onto the "
        "session dataclass."
    )


@pytest.mark.asyncio
async def test_handler_forwards_lookahead_persistence_store_to_dispatch(monkeypatch):
    """RED-driver: with a real store on ``sd.lookahead_handle.persistence`` and NO
    ``sd.dungeon_store``, the handler must forward THAT store to
    ``dispatch_dice_throw``. On the dead ``getattr`` it forwards ``None`` and this
    fails; once resolution moves to ``lookahead_handle`` it forwards the store."""
    from sidequest.server.session_handler import _State

    sd, snap, _room_id = build_sd_with_tactical_region(creature_revealed=True)
    # Seed WN-legal stats (initiative reads DEX at other seams; harmless here and
    # keeps the fixture consistent with the seating tests).
    snap.characters[0].stats.update(
        {"STR": 12, "DEX": 12, "CON": 12, "INT": 12, "WIS": 12, "CHA": 12}
    )

    # Remove the hand-set theater attribute the fixture adds, then attach the store
    # the PRODUCTION way: on the lookahead handle. On the dead getattr path the
    # handler now sees no dungeon_store and resolves None (RED). The real fix reads
    # lookahead_handle.persistence and forwards SENTINEL_STORE (GREEN).
    if hasattr(sd, "dungeon_store"):
        delattr(sd, "dungeon_store")
    sentinel_store = object()
    sd.lookahead_handle = SimpleNamespace(persistence=sentinel_store)

    captured: dict = {}

    def _spy(**kwargs):
        captured.update(kwargs)
        raise _StopAtDispatch

    # The handler does a fresh ``from ...dice import dispatch_dice_throw`` per call,
    # so patch the SOURCE symbol, not a handler-local rebind.
    monkeypatch.setattr(
        "sidequest.server.dispatch.dice.dispatch_dice_throw", _spy, raising=True
    )

    session = MagicMock()
    session._state = _State.Playing
    session._room = None  # solo path: no room_broadcast / emit_confrontation
    session._session_data = sd

    from sidequest.handlers.dice_throw import HANDLER

    with pytest.raises(_StopAtDispatch):
        await HANDLER.handle(session, _dice_msg())

    assert "dungeon_store" in captured, "handler never reached dispatch_dice_throw"
    assert captured["dungeon_store"] is sentinel_store, (
        "DiceThrowHandler forwarded dungeon_store="
        f"{captured['dungeon_store']!r} — it must resolve the store from "
        "sd.lookahead_handle.persistence (the prod store), not the dead "
        "getattr(sd, 'dungeon_store', None) which is always None in production. "
        "With the store unresolved, _resolve_room_mask returns None and the reach "
        "gate silently skips (reason=no_grid) on every real strike."
    )
