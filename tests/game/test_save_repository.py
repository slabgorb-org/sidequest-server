"""SaveRepository interface + SqliteSaveRepository adapter tests."""

from __future__ import annotations

from sidequest.game.persistence import SqliteStore
from sidequest.game.projection_filter import FilterDecision
from sidequest.game.repository import SaveRepository, SaveTransaction
from sidequest.game.sqlite_repository import SqliteSaveRepository


def test_protocols_are_runtime_checkable():
    # Protocols must be importable and runtime_checkable so isinstance()
    # works in wiring tests and the adapter can be asserted against them.
    assert hasattr(SaveRepository, "_is_runtime_protocol")
    assert hasattr(SaveTransaction, "_is_runtime_protocol")


def _repo() -> SqliteSaveRepository:
    return SqliteSaveRepository(SqliteStore.open_in_memory())


def test_adapter_satisfies_protocol():
    assert isinstance(_repo(), SaveRepository)


def test_append_event_assigns_monotonic_seq():
    repo = _repo()
    r1 = repo.append_event(kind="NARRATION", payload_json="{}")
    r2 = repo.append_event(kind="STATE_UPDATE", payload_json="{}")
    assert r1.seq == 1
    assert r2.seq == 2
    assert repo.latest_event_seq() == 2


def test_read_events_since_orders_ascending():
    repo = _repo()
    repo.append_event(kind="A", payload_json="{}")
    repo.append_event(kind="B", payload_json="{}")
    rows = repo.read_events_since(since_seq=0)
    assert [r.kind for r in rows] == ["A", "B"]
    assert repo.read_events_since(since_seq=1)[0].kind == "B"


def test_transaction_is_atomic_on_exception():
    repo = _repo()
    repo.append_event(kind="SEED", payload_json="{}")
    try:
        with repo.transaction() as tx:
            tx.append_event(kind="DOOMED", payload_json="{}")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert repo.latest_event_seq() == 1


def test_transaction_event_plus_projection_commit_together():
    repo = _repo()
    with repo.transaction() as tx:
        row = tx.append_event(kind="NARRATION", payload_json="{}")
        tx.write_projection(
            event_seq=row.seq,
            player_id="p1",
            decision=FilterDecision(include=True, payload_json="{}"),
        )
    cached = repo.read_projection_since(player_id="p1", since_seq=0)
    assert len(cached) == 1
    assert cached[0].event_seq == row.seq
    assert cached[0].include is True


def test_write_projection_conflict_is_idempotent():
    repo = _repo()
    row = repo.append_event(kind="NARRATION", payload_json="{}")
    for include in (True, False):
        repo.write_projection(
            event_seq=row.seq,
            player_id="p1",
            decision=FilterDecision(include=include, payload_json="{}"),
        )
    cached = repo.read_projection_since(player_id="p1", since_seq=0)
    assert len(cached) == 1
    assert cached[0].include is False


from sidequest.game.event_log import EventLog  # noqa: E402


def test_event_log_delegates_to_repository():
    repo = SqliteSaveRepository(SqliteStore.open_in_memory())
    log = EventLog(repo)
    row = log.append(kind="NARRATION", payload_json="{}")
    assert row.seq == 1
    assert log.latest_seq() == 1
    assert [r.kind for r in log.read_since(since_seq=0)] == ["NARRATION"]


def test_event_log_no_longer_exposes_in_transaction():
    repo = SqliteSaveRepository(SqliteStore.open_in_memory())
    log = EventLog(repo)
    assert not hasattr(log, "append_in_transaction")


from sidequest.game.projection.cache import ProjectionCache  # noqa: E402


def test_projection_cache_delegates_to_repository():
    repo = SqliteSaveRepository(SqliteStore.open_in_memory())
    row = repo.append_event(kind="NARRATION", payload_json="{}")
    cache = ProjectionCache(repo)
    cache.write(
        event_seq=row.seq,
        player_id="p1",
        decision=FilterDecision(include=True, payload_json="{}"),
    )
    got = cache.read_since(player_id="p1", since_seq=0)
    assert len(got) == 1 and got[0].include is True


def test_projection_cache_no_longer_exposes_in_transaction():
    repo = SqliteSaveRepository(SqliteStore.open_in_memory())
    cache = ProjectionCache(repo)
    assert not hasattr(cache, "write_in_transaction")


def test_connect_constructs_event_log_over_repository():
    repo = SqliteSaveRepository(SqliteStore.open_in_memory())
    log = EventLog(repo)
    cache = ProjectionCache(repo)
    assert isinstance(log.repository, SaveRepository)
    row = log.append(kind="NARRATION", payload_json="{}")
    cache.write(
        event_seq=row.seq,
        player_id="p1",
        decision=FilterDecision(include=True, payload_json="{}"),
    )
    assert cache.read_since(player_id="p1", since_seq=0)[0].event_seq == row.seq
