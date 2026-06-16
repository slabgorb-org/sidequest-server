"""Reconnect reads pre-computed projection_cache — bit-identical to live frames."""

from __future__ import annotations

from pathlib import Path

from sidequest.game.event_log import EventLog
from sidequest.game.persistence import SqliteStore
from sidequest.game.projection.cache import ProjectionCache
from sidequest.game.projection.composed import ComposedFilter
from sidequest.game.projection.envelope import MessageEnvelope
from sidequest.game.projection.view import SessionGameStateView
from sidequest.game.projection_filter import FilterDecision
from sidequest.game.sqlite_repository import SqliteSaveRepository


def test_reconnect_replays_cached_payloads(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "s.db")
    repo = SqliteSaveRepository(store)
    log = EventLog(repo)
    cache = ProjectionCache(repo)
    filt = ComposedFilter.with_no_genre_rules()
    view = SessionGameStateView(
        gm_player_id="gm",
        player_id_to_character={"alice": "alice_char"},
    )

    live_frames: dict[int, FilterDecision] = {}
    for text in ["one", "two", "three"]:
        row = log.append(kind="NARRATION", payload_json=f'{{"text":"{text}"}}')
        env = MessageEnvelope(kind=row.kind, payload_json=row.payload_json, origin_seq=row.seq)
        decision = filt.project(envelope=env, view=view, player_id="alice")
        cache.write(event_seq=row.seq, player_id="alice", decision=decision)
        live_frames[row.seq] = decision

    replayed = cache.read_since(player_id="alice", since_seq=0)
    assert len(replayed) == 3
    for cached in replayed:
        live = live_frames[cached.event_seq]
        assert cached.include == live.include
        assert cached.payload_json == (live.payload_json if live.include else None)
