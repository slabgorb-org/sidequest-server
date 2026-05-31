"""WS-boundary, room-store, and PARTY_STATUS wiring tests (Story 67-6)."""

import pytest

from sidequest.game.persistence import GameMode
from sidequest.server.session_room import SessionRoom


class _FakeWS:
    """Minimal WebSocket double: headers + accept/close recording."""

    def __init__(self, headers: dict[str, str]):
        self.headers = headers
        self.accepted = False
        self.closed_code: int | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000) -> None:
        self.closed_code = code


@pytest.mark.asyncio
async def test_ws_boundary_closes_when_identity_unresolvable():
    from sidequest.server import websocket as ws_mod

    ws = _FakeWS(headers={})  # no Cf-Access, no Host -> unresolvable
    result = await ws_mod.resolve_identity_or_close(ws)

    assert result is None
    assert ws.accepted is False
    assert ws.closed_code == 1008  # policy violation


@pytest.mark.asyncio
async def test_ws_boundary_returns_identity_and_source_when_resolvable():
    from sidequest.server import websocket as ws_mod

    ws = _FakeWS(headers={"cf-access-authenticated-user-email": "alice@example.com"})
    result = await ws_mod.resolve_identity_or_close(ws)

    assert result == ("alice@example.com", "cf_access")
    assert ws.closed_code is None


def _room() -> SessionRoom:
    return SessionRoom(slug="s", mode=GameMode.SOLO)


def test_room_stores_and_reads_player_identity():
    room = _room()
    assert room.get_player_identity("p1") is None
    room.set_player_identity("p1", "alice@example.com")
    assert room.get_player_identity("p1") == "alice@example.com"
    # Reconnect overwrites via the same writer (the only non-disconnect path
    # that mutates identity).
    room.set_player_identity("p1", "bob@example.com")
    assert room.get_player_identity("p1") == "bob@example.com"


def test_identity_survives_transient_disconnect_cleared_on_last_socket():
    """Identity is ephemeral but ref-counted by presence: it MUST survive a
    transient (multi-socket) disconnect and only drop when the player's LAST
    socket closes. Drives the real disconnect() path edited in Task 2 — no
    poking internal dicts for the disconnect step.
    """
    room = SessionRoom(slug="s", mode=GameMode.MULTIPLAYER)
    room.set_player_identity("p1", "alice@example.com")
    room.connect("p1", socket_id="sock-A")
    room.connect("p1", socket_id="sock-B")  # HMR / reload -> two live sockets

    # Transient close (latest socket): player still present on sock-A, so
    # identity must survive.
    assert room.disconnect(socket_id="sock-B") is None
    assert room.get_player_identity("p1") == "alice@example.com", (
        "identity must survive a transient disconnect while another WS is alive"
    )

    # Last socket close: player fully gone, identity must clear.
    assert room.disconnect(socket_id="sock-A") == "p1"
    assert room.get_player_identity("p1") is None, (
        "identity must clear when the player's last socket closes"
    )
