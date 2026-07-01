"""Story 160-4 WIRING (RED): the SOLO-companion exemption must be reachable from
the production connect handler, not just from a direct ``SessionRoom.connect``
call.

The fix touches TWO sites: ``SessionRoom.connect`` (the guard exemption + OTEL)
AND ``handlers/connect.py`` (which must pass the connect handshake's
``companion_of`` into ``room.connect``). A Dev who edits only ``connect`` leaves
the bug live in production. This drives the REAL connect handler — a human takes
the SOLO slot, then a bonded companion connects — and asserts the companion is
admitted (no ERROR frame; both in ``_connected``). It fails today because the
handler's ``room.connect`` call raises ``SoloSlotConflict`` and the handler
returns an ERROR frame.

DB-gated (ADR-115): SKIPS loudly without ``SIDEQUEST_TEST_DATABASE_URL`` — same
convention as the other connect-handler integration tests in this suite. The
autouse PG fixture lives in THIS file (not the fast unit file) so it never gates
the DB-free ``test_companion_solo_seat.py`` unit tests.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.persistence import GameMode
from sidequest.game.session import GameSnapshot
from sidequest.protocol.messages import SessionEventMessage, SessionEventPayload
from sidequest.server.session_handler import WebSocketSessionHandler
from sidequest.server.session_room import RoomRegistry

_GENRE = "space_opera"
_WORLD = "coyote_star"
_CONTENT_SEARCH_PATH = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Per-worker throwaway PG db, cleaned per test (ADR-115 D2). Mirrors
    ``test_seal_reconcile_reconnect_order.py``."""
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


def _seed_solo_human(slug: str, player_id: str, char_name: str) -> None:
    """Persist a SOLO snapshot with the human durably seated — a reload/connect
    restores it from Postgres and the human reclaims the sole solo slot."""
    from sidequest.game import db_pool
    from sidequest.server.session_state import _build_pg_repos_for_slug

    snap = GameSnapshot(genre_slug=_GENRE, world_slug=_WORLD, location="Far Landing")
    core = CreatureCore(
        name=char_name,
        description=f"Playtest character for {player_id}",
        personality="reach-tested",
        inventory=Inventory(),
    )
    snap.characters = [
        Character(core=core, char_class="Smuggler", race="Coreworlder", backstory="Gate")
    ]
    snap.player_seats[player_id] = char_name

    repo, _dungeon, _sink = _build_pg_repos_for_slug(
        db_pool.get_pool(),
        slug=slug,
        mode=str(GameMode.SOLO),
        genre_slug=_GENRE,
        world_slug=_WORLD,
    )
    repo.save(snap)


def _handler(save_dir: Path, registry: RoomRegistry, socket_id: str) -> WebSocketSessionHandler:
    handler = WebSocketSessionHandler(
        save_dir=save_dir,
        genre_pack_search_paths=[_CONTENT_SEARCH_PATH],
    )
    handler.attach_room_context(registry=registry, socket_id=socket_id, out_queue=asyncio.Queue())
    return handler


async def _connect(
    handler: WebSocketSessionHandler,
    *,
    player_id: str,
    name: str,
    slug: str,
    companion_of: str | None = None,
    relationship: str | None = None,
) -> list[object]:
    return await handler.handle_message(
        SessionEventMessage(
            type="SESSION_EVENT",
            player_id=player_id,
            payload=SessionEventPayload(
                event="connect",
                game_slug=slug,
                player_name=name,
                companion_of=companion_of,
                relationship=relationship,
            ),
        )
    )


def _error_frames(messages: list[object]) -> list[object]:
    return [m for m in messages if str(getattr(m, "type", "")).endswith("ERROR")]


@pytest.mark.asyncio
async def test_bonded_companion_admitted_to_solo_room_through_handler(tmp_path: Path) -> None:
    """End-to-end: the human takes the SOLO slot, then a bonded companion
    (``companion_of`` set, role: pet) connects. The companion MUST be admitted —
    no ERROR frame, and both appear in the room roster. Today the handler's
    ``room.connect`` raises ``SoloSlotConflict`` and returns an ERROR frame (the
    exact 160-4 repro)."""
    slug = "2026-06-27-beneath_sunden-solo-wiring"
    _seed_solo_human(slug, "curly-pid", "Curly")

    registry = RoomRegistry()

    # The human reclaims the sole solo slot.
    human = _handler(tmp_path, registry, "sock-human")
    human_out = await _connect(human, player_id="curly-pid", name="Curly", slug=slug)
    assert not _error_frames(human_out), (
        f"the human's own connect to their SOLO room must succeed; got {human_out}"
    )

    # The owner's server-resolved identity (ADR-119) — in real play the connect
    # boundary resolves this from the Host header / Cf-Access. The companion's
    # companion_of below must match it, or the auth-checked exemption fails closed
    # (review 160-4). Record it the way the room store would.
    registry.get_or_create(slug, mode=GameMode.SOLO).set_player_identity(
        "curly-pid", "player1.local"
    )

    # A bonded companion connects. Before path (b): SoloSlotConflict -> ERROR.
    companion = _handler(tmp_path, registry, "sock-owl")
    companion_out = await _connect(
        companion,
        player_id="owl-pid",
        name="Owl",
        slug=slug,
        companion_of="player1.local",
        relationship="pet",
    )

    assert not _error_frames(companion_out), (
        "a bonded companion (companion_of set) must NOT be rejected from a SOLO "
        f"room — the SoloSlotConflict guard must exempt it (path b); got {companion_out}"
    )
    room = registry.get_or_create(slug, mode=GameMode.SOLO)
    assert set(room.connected_player_ids()) == {"curly-pid", "owl-pid"}, (
        "the companion must be admitted into the room roster alongside its human; "
        f"got {room.connected_player_ids()}"
    )
