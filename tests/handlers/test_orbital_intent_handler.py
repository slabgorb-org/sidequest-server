"""Wiring tests for OrbitalIntentHandler.

Drives ``WebSocketSessionHandler.handle_message`` with an ORBITAL_INTENT
message and verifies the registry routes through the new handler.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sidequest.game.persistence import GameMode, SqliteStore
from sidequest.game.session import GameSnapshot
from sidequest.protocol.enums import MessageType
from sidequest.protocol.messages import (
    ErrorMessage,
    OrbitalChartMessage,
    OrbitalIntentMessage,
)
from sidequest.protocol.orbital_intent import OrbitalIntent
from sidequest.server.session_handler import WebSocketSessionHandler
from sidequest.server.session_room import RoomRegistry, SessionRoom

ORBITAL_FIXTURES = Path(__file__).resolve().parents[1] / "orbital" / "fixtures"


def _attach(handler: WebSocketSessionHandler) -> None:
    handler.attach_room_context(
        registry=RoomRegistry(),
        socket_id="sock-1",
        out_queue=asyncio.Queue(),
    )


def _bind_room_with_orbital(handler: WebSocketSessionHandler, tmp_path: Path) -> None:
    snapshot = GameSnapshot(party_body_id="turning_hub")
    store = SqliteStore(tmp_path / "t.db")
    room = SessionRoom(slug="orbital-test", mode=GameMode.SOLO)
    room.bind_world(
        snapshot=snapshot,
        store=store,
        world_dir=ORBITAL_FIXTURES / "world_minimal",
    )
    handler._room = room


@pytest.mark.asyncio
async def test_view_map_routes_through_handler(tmp_path: Path) -> None:
    handler = WebSocketSessionHandler(save_dir=tmp_path / "saves", genre_pack_search_paths=[])
    _attach(handler)
    _bind_room_with_orbital(handler, tmp_path)

    msg = OrbitalIntentMessage(
        payload=OrbitalIntent.model_validate({"kind": "view_map", "scope": "system_root"}),
        player_id="P1",
    )
    outbound = await handler.handle_message(msg)

    assert len(outbound) == 1
    chart = outbound[0]
    assert isinstance(chart, OrbitalChartMessage)
    assert chart.type == MessageType.ORBITAL_CHART
    assert chart.payload.scope_center == "coyote"
    assert "<svg" in chart.payload.svg
    assert chart.payload.party_at == "turning_hub"


@pytest.mark.asyncio
async def test_drill_in_then_drill_out_persists_scope(tmp_path: Path) -> None:
    handler = WebSocketSessionHandler(save_dir=tmp_path / "saves", genre_pack_search_paths=[])
    _attach(handler)
    _bind_room_with_orbital(handler, tmp_path)

    drill_in = OrbitalIntentMessage(
        payload=OrbitalIntent.model_validate({"kind": "drill_in", "body_id": "red_prospect"}),
    )
    drill_out = OrbitalIntentMessage(
        payload=OrbitalIntent.model_validate({"kind": "drill_out"}),
    )

    out_in = await handler.handle_message(drill_in)
    assert isinstance(out_in[0], OrbitalChartMessage)
    assert out_in[0].payload.scope_center == "red_prospect"

    out_back = await handler.handle_message(drill_out)
    assert isinstance(out_back[0], OrbitalChartMessage)
    assert out_back[0].payload.scope_center == "coyote"


@pytest.mark.asyncio
async def test_unbound_room_returns_session_unbound_error(tmp_path: Path) -> None:
    handler = WebSocketSessionHandler(save_dir=tmp_path / "saves", genre_pack_search_paths=[])
    _attach(handler)
    # No room bound — _room is None.

    msg = OrbitalIntentMessage(
        payload=OrbitalIntent.model_validate({"kind": "view_map", "scope": "system_root"}),
    )
    outbound = await handler.handle_message(msg)

    assert len(outbound) == 1
    err = outbound[0]
    assert isinstance(err, ErrorMessage)
    assert err.payload.code == "session_unbound"


@pytest.mark.asyncio
async def test_world_without_orbital_tier_returns_unavailable_error(tmp_path: Path) -> None:
    handler = WebSocketSessionHandler(save_dir=tmp_path / "saves", genre_pack_search_paths=[])
    _attach(handler)

    # Bind a room without orbital content (no world_dir provided).
    snapshot = GameSnapshot()
    store = SqliteStore(tmp_path / "t.db")
    room = SessionRoom(slug="no-orbital", mode=GameMode.SOLO)
    room.bind_world(snapshot=snapshot, store=store)
    handler._room = room

    msg = OrbitalIntentMessage(
        payload=OrbitalIntent.model_validate({"kind": "view_map", "scope": "system_root"}),
    )
    outbound = await handler.handle_message(msg)

    err = outbound[0]
    assert isinstance(err, ErrorMessage)
    assert err.payload.code == "orbital_unavailable"


@pytest.mark.asyncio
async def test_handler_is_registered(tmp_path: Path) -> None:
    """Wiring assertion: the registry returns a handler for ORBITAL_INTENT."""
    handler_class = WebSocketSessionHandler
    registered = handler_class._message_handler_for("ORBITAL_INTENT")
    assert registered is not None
