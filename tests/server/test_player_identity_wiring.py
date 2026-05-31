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
    assert room.player_identities.get("p1") == "alice@example.com", (
        "identity must survive a transient disconnect while another WS is alive"
    )

    # Last socket close: player fully gone, identity must clear.
    assert room.disconnect(socket_id="sock-A") == "p1"
    assert "p1" not in room.player_identities, (
        "identity must clear when the player's last socket closes"
    )
