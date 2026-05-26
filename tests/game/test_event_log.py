from pathlib import Path

import pytest

from sidequest.game.event_log import EventLog
from sidequest.game.persistence import SqliteStore, db_path_for_slug
from sidequest.game.sqlite_repository import SqliteSaveRepository


@pytest.fixture
def store(tmp_path: Path) -> SqliteStore:
    db = db_path_for_slug(tmp_path, "2026-04-22-moldharrow-keep")
    db.parent.mkdir(parents=True, exist_ok=True)
    s = SqliteStore(db)
    s.initialize()
    return s


def test_append_assigns_monotonic_seq(store):
    log = EventLog(SqliteSaveRepository(store))
    r1 = log.append(kind="NARRATION", payload_json='{"text":"hello"}')
    r2 = log.append(kind="STATE_UPDATE", payload_json='{"hp":10}')
    assert r1.seq == 1
    assert r2.seq == 2


def test_read_since_returns_only_newer(store):
    log = EventLog(SqliteSaveRepository(store))
    for i in range(5):
        log.append(kind="NARRATION", payload_json=f'{{"i":{i}}}')
    rows = log.read_since(since_seq=2)
    assert [r.seq for r in rows] == [3, 4, 5]


def test_read_since_zero_returns_all(store):
    log = EventLog(SqliteSaveRepository(store))
    log.append(kind="NARRATION", payload_json='{"i":1}')
    log.append(kind="NARRATION", payload_json='{"i":2}')
    rows = log.read_since(since_seq=0)
    assert len(rows) == 2


def test_latest_seq(store):
    log = EventLog(SqliteSaveRepository(store))
    assert log.latest_seq() == 0
    log.append(kind="NARRATION", payload_json="{}")
    log.append(kind="STATE_UPDATE", payload_json="{}")
    assert log.latest_seq() == 2
