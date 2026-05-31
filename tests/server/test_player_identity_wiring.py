"""WS-boundary, room-store, and PARTY_STATUS wiring tests (Story 67-6)."""
from sidequest.game.persistence import GameMode
from sidequest.server.session_room import SessionRoom


def _room() -> SessionRoom:
    return SessionRoom(slug="s", mode=GameMode.SOLO)


def test_room_stores_and_clears_player_identity():
    room = _room()
    room.set_player_identity("p1", "alice@example.com")
    assert room.player_identities.get("p1") == "alice@example.com"
    room.clear_player_identity("p1")
    assert "p1" not in room.player_identities
