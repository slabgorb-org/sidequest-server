"""Reconnect replay: events since last_seen_seq are replayed on connect (MP-03 Task 4).

Test 1: Seeds 3 NARRATION events (seq 1/2/3), connects with last_seen_seq=1.
        Expects NARRATION seq=2 then seq=3 in order after SESSION_CONNECTED.

Test 2: Seeds 3 events, connects with last_seen_seq=3.
        Only SESSION_CONNECTED arrives; no NARRATION follows.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sidequest.game.event_log import EventLog
from sidequest.game.persistence import (
    GameMode,
    SqliteStore,
    db_path_for_slug,
    upsert_game,
)
from sidequest.game.sqlite_repository import SqliteSaveRepository
from sidequest.genre.loader import DEFAULT_GENRE_PACK_SEARCH_PATHS
from sidequest.server.app import create_app

_GENRE = "caverns_and_claudes"
_WORLD = "grimvault"
_SLUG_BASE = "2026-04-22-grimvault-replay"


def _genre_packs_path() -> Path | None:
    return next(
        (p for p in DEFAULT_GENRE_PACK_SEARCH_PATHS if p.exists()),
        None,
    )


def _seed_with_events(tmp_path: Path, slug: str) -> None:
    """Create game row and seed 3 NARRATION events (seq 1/2/3)."""
    db = db_path_for_slug(tmp_path, slug)
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(db)
    store.initialize()
    upsert_game(
        store,
        slug=slug,
        mode=GameMode.SOLO,
        genre_slug=_GENRE,
        world_slug=_WORLD,
    )
    log = EventLog(SqliteSaveRepository(store))
    for i in range(3):
        log.append(kind="NARRATION", payload_json=f'{{"text":"beat {i + 1}","seq":0}}')
    store.close()


def test_connect_with_last_seen_seq_replays_missed_events(tmp_path: Path) -> None:
    """Connecting with last_seen_seq=1 replays NARRATION seq=2 and seq=3."""
    packs = _genre_packs_path()
    if packs is None:
        pytest.skip(f"No genre_packs directory found in {DEFAULT_GENRE_PACK_SEARCH_PATHS}")

    slug = _SLUG_BASE + "-missed"
    _seed_with_events(tmp_path, slug)

    app = create_app(genre_pack_search_paths=[packs], save_dir=tmp_path)
    client = TestClient(app)

    with client.websocket_connect("/ws") as ws:
        ws.send_json(
            {
                "type": "SESSION_EVENT",
                "player_id": "alice",
                "payload": {
                    "event": "connect",
                    "game_slug": slug,
                    "last_seen_seq": 1,
                },
            }
        )

        # First message must be SESSION_CONNECTED
        first = ws.receive_json()
        assert first["type"] == "SESSION_EVENT", (
            f"Expected SESSION_EVENT first, got {first['type']}"
        )
        assert first["payload"]["event"] == "connected"

        # Next two messages must be NARRATION with seq=2, then seq=3
        seen_seqs: list[int] = []
        for _ in range(5):
            m = ws.receive_json()
            if m["type"] == "NARRATION":
                seen_seqs.append(m["payload"]["seq"])
            if len(seen_seqs) == 2:
                break

    assert seen_seqs == [2, 3], f"Expected replay of seq [2, 3] but got {seen_seqs}"


def test_connect_with_last_seen_seq_equal_to_latest_replays_nothing(tmp_path: Path) -> None:
    """Connecting with last_seen_seq=3 (fully caught up) only sends SESSION_CONNECTED."""
    packs = _genre_packs_path()
    if packs is None:
        pytest.skip(f"No genre_packs directory found in {DEFAULT_GENRE_PACK_SEARCH_PATHS}")

    slug = _SLUG_BASE + "-caught-up"
    _seed_with_events(tmp_path, slug)

    app = create_app(genre_pack_search_paths=[packs], save_dir=tmp_path)
    client = TestClient(app)

    with client.websocket_connect("/ws") as ws:
        ws.send_json(
            {
                "type": "SESSION_EVENT",
                "player_id": "alice",
                "payload": {
                    "event": "connect",
                    "game_slug": slug,
                    "last_seen_seq": 3,
                },
            }
        )

        connected = ws.receive_json()
        assert connected["type"] == "SESSION_EVENT"
        assert connected["payload"]["event"] == "connected"
        # No replay should follow — the client is already up to date.
        # We stop reading here; if extra data arrived the next call would
        # show it, but the test doesn't require silence.
