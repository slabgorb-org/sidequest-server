"""WS-boundary, room-store, and PARTY_STATUS wiring tests (Story 67-6)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sidequest.game.persistence import GameMode
from sidequest.server.session_room import SessionRoom

_GENRE = "caverns_and_claudes"
_WORLD = "grimvault"
_CONTENT_SEARCH_PATH = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


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


def test_bind_player_identity_writes_room_store_and_skips_blank():
    from sidequest.handlers.connect import bind_player_identity

    room = _room()
    # identity present -> stored
    bind_player_identity(room, player_id="p1", identity="alice@example.com", source="cf_access")
    assert room.get_player_identity("p1") == "alice@example.com"
    # identity absent -> no entry, no crash
    bind_player_identity(room, player_id="p2", identity=None, source=None)
    assert room.get_player_identity("p2") is None


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


# ---------------------------------------------------------------------------
# Wiring test — ConnectHandler.handle actually calls bind_player_identity
# ---------------------------------------------------------------------------


@pytest.fixture()
def _pg_isolation_67_6(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Per-test PG isolation mirroring the pattern in test_solo_auto_seat_on_connect."""
    import psycopg

    from sidequest.game import db_pool

    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(plain, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename <> 'alembic_version'"
        ).fetchall()
        if rows:
            names = ", ".join(f'"{r[0]}"' for r in rows)
            conn.execute(f"TRUNCATE {names} RESTART IDENTITY CASCADE")
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    yield
    db_pool.close_pool()


def _seed_solo_session_for_67_6(slug: str) -> None:
    """Register a SOLO session row in PG (no snapshot — new session / chargen path)."""
    from sidequest.game import db_pool
    from sidequest.server.session_state import _build_pg_repos_for_slug

    _build_pg_repos_for_slug(
        db_pool.get_pool(),
        slug=slug,
        mode=str(GameMode.SOLO),
        genre_slug=_GENRE,
        world_slug=_WORLD,
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("_pg_isolation_67_6")
async def test_connect_handler_writes_identity_to_room_store(tmp_path: Path) -> None:
    """Wiring test: ConnectHandler.handle must call bind_player_identity so that
    room.get_player_identity(player_id) returns the identity that was set on the
    WebSocketSessionHandler before the slug-connect message arrived.

    If the bind_player_identity() call is removed from connect.py, this test
    fails — the room store will be empty for the player_id.
    """
    if not (_CONTENT_SEARCH_PATH / _GENRE).is_dir():
        pytest.skip(f"{_GENRE} content not found")

    from sidequest.protocol.messages import SessionEventMessage, SessionEventPayload
    from sidequest.server.session_handler import WebSocketSessionHandler
    from sidequest.server.session_room import RoomRegistry

    slug = "67-6-identity-wiring-test"
    player_id = "alice-pid"
    identity = "alice@example.com"

    _seed_solo_session_for_67_6(slug)

    registry = RoomRegistry()
    handler = WebSocketSessionHandler(
        save_dir=tmp_path,
        genre_pack_search_paths=[_CONTENT_SEARCH_PATH],
    )
    # Set the identity BEFORE connect — mirrors the WS-boundary resolver (Task 3).
    handler.attach_room_context(
        registry=registry,
        socket_id="sock-alice",
        out_queue=asyncio.Queue(),
        player_identity=identity,
        player_identity_source="cf_access",
    )

    connect_msg = SessionEventMessage(
        type="SESSION_EVENT",
        player_id=player_id,
        payload=SessionEventPayload(
            event="connect",
            game_slug=slug,
            player_name="Alice",
        ),
    )
    await handler.handle_message(connect_msg)

    room = handler._room
    assert room is not None, "slug-connect must bind a room"
    assert room.get_player_identity(player_id) == identity, (
        "ConnectHandler.handle must call bind_player_identity — "
        "the room store must reflect the WS-boundary resolved identity. "
        "If this fails, bind_player_identity() was not called from connect.py."
    )


def test_perspective_character_name_uses_seat_then_falls_back():
    from sidequest.server.websocket_session_handler import perspective_character_name

    class _Snap:
        def __init__(self, seats):
            self.player_seats = seats

    class _SD:
        def __init__(self, pid, pname, seats):
            self.player_id = pid
            self.player_name = pname
            self.snapshot = _Snap(seats)

    # seated -> returns the seated character name
    sd = _SD("p1", "DISPLAY", {"p1": "Rux"})
    assert perspective_character_name(sd) == "Rux"
    # unseated -> falls back to sd.player_name (behavior-preserving for pre-seat)
    sd2 = _SD("p2", "Laverne", {})
    assert perspective_character_name(sd2) == "Laverne"


def test_party_member_identity_present_for_connected_absent_for_disconnected():
    """Self carries identity from the room; a disconnected peer carries None
    and is NEVER given the character name as a fabricated identity."""
    from sidequest.protocol.models import PartyMember

    identities = {"p1": "alice@example.com"}  # room knows p1 (connected), not peer:Rux

    self_member = PartyMember(
        player_id="p1", name="Laverne", player_identity=identities.get("p1"),
        character_name="Laverne", current_hp=10, max_hp=10,
        survivability_pool_label=None, statuses=[], **{"class": "Fighter"}, level=1,
    )
    peer_member = PartyMember(
        player_id="peer:Rux", name="Rux", player_identity=identities.get("peer:Rux"),
        character_name="Rux", current_hp=10, max_hp=10,
        survivability_pool_label=None, statuses=[], **{"class": "Mage"}, level=1,
    )
    assert self_member.player_identity == "alice@example.com"
    assert peer_member.player_identity is None
    assert peer_member.player_identity != peer_member.character_name
