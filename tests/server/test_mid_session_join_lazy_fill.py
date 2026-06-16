"""Mid-session join: lazy-fill cache for the new player."""

from __future__ import annotations

from pathlib import Path

from sidequest.game.event_log import EventLog
from sidequest.game.persistence import SqliteStore
from sidequest.game.projection.cache import ProjectionCache
from sidequest.game.projection.cache_fill import lazy_fill
from sidequest.game.projection.composed import ComposedFilter
from sidequest.game.projection.view import SessionGameStateView
from sidequest.game.projection_filter import FilterDecision
from sidequest.game.sqlite_repository import SqliteSaveRepository


def test_lazy_fill_populates_cache_for_new_player(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "s.db")
    repo = SqliteSaveRepository(store)
    log = EventLog(repo)
    cache = ProjectionCache(repo)
    filt = ComposedFilter.with_no_genre_rules()
    view = SessionGameStateView(
        gm_player_id="gm",
        player_id_to_character={"alice": "alice_char"},
    )

    log.append(kind="NARRATION", payload_json='{"text":"one"}')
    log.append(kind="NARRATION", payload_json='{"text":"two"}')

    filled = lazy_fill(
        event_log=log,
        cache=cache,
        filter_=filt,
        view=view,
        player_id="alice",
    )
    assert filled == 2

    rows = cache.read_since(player_id="alice", since_seq=0)
    assert [r.event_seq for r in rows] == [1, 2]


def test_lazy_fill_skips_already_cached_events(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "s.db")
    repo = SqliteSaveRepository(store)
    log = EventLog(repo)
    cache = ProjectionCache(repo)
    filt = ComposedFilter.with_no_genre_rules()
    view = SessionGameStateView(
        gm_player_id="gm",
        player_id_to_character={"alice": "alice_char"},
    )

    log.append(kind="NARRATION", payload_json='{"text":"one"}')
    log.append(kind="NARRATION", payload_json='{"text":"two"}')

    cache.write(
        event_seq=1,
        player_id="alice",
        decision=FilterDecision(include=True, payload_json='{"text":"one"}'),
    )

    filled = lazy_fill(event_log=log, cache=cache, filter_=filt, view=view, player_id="alice")
    assert filled == 1
