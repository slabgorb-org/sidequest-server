"""SaveRepository interface + SqliteSaveRepository adapter tests.

Protocol growth note (ADR-115 A7 / D8)
---------------------------------------
The ``SaveRepository`` Protocol was grown in A7 to the full typed surface
(snapshots, narrative, scrapbook, promotions, session lifecycle).
``SqliteSaveRepository`` implements the events + projection_cache surface plus
(D8) snapshot save/load and narrative read/write — the methods production
paths reach through the bound repository when the in-memory shim is the test
double. It still does not implement the full Protocol (scrapbook, promotions,
session lifecycle) and is deleted in F1, so it does not satisfy
``isinstance(x, SaveRepository)`` for the grown Protocol — that isinstance
check lives in ``tests/persistence/test_pg_save_repository.py`` over
``PgSaveRepository``, which implements the full surface.

The *behaviour* tests below remain valid against ``SqliteSaveRepository`` —
they exercise the methods it does implement.
"""

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


def test_sqlite_adapter_implements_slice_1a_surface():
    """SqliteSaveRepository has the Slice-1a methods required by its consumers.

    Full-Protocol isinstance is checked in test_pg_save_repository.py.
    SqliteSaveRepository is deleted in F1; this test exists until then.
    """
    repo = _repo()
    # Structural check: the Slice-1a methods the existing callers use are present.
    for attr in (
        "transaction",
        "append_event",
        "read_events_since",
        "latest_event_seq",
        "write_projection",
        "read_projection_since",
    ):
        assert callable(getattr(repo, attr, None)), f"missing Slice-1a method: {attr}"


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
    # log.repository is the same SqliteSaveRepository passed in.
    # isinstance vs full SaveRepository Protocol is tested in test_pg_save_repository.py.
    assert log.repository is repo
    row = log.append(kind="NARRATION", payload_json="{}")
    cache.write(
        event_seq=row.seq,
        player_id="p1",
        decision=FilterDecision(include=True, payload_json="{}"),
    )
    assert cache.read_since(player_id="p1", since_seq=0)[0].event_seq == row.seq


# ---------------------------------------------------------------------------
# Snapshot + narrative delegation (ADR-115 D8)
# ---------------------------------------------------------------------------
# Production code reaches the bound SaveRepository for snapshot save/load and
# narrative reads/writes (e.g. session_helpers builds TurnContext.recent_
# narrative_log via repository.recent_narrative; turn_manager.round_invariant
# reads repository.max_narrative_round). The SqliteSaveRepository test shim
# wraps a real SqliteStore, so it delegates these to the backing store. Until
# F1 deletes the shim, these delegations keep the in-memory test double able to
# back those production paths.


def test_adapter_delegates_narrative_to_backing_store():
    from sidequest.game.session import NarrativeEntry

    repo = _repo()
    assert repo.max_narrative_round() == 0
    assert repo.recent_narrative(5) == []

    repo.append_narrative(NarrativeEntry(round=1, author="narrator", content="A"))
    repo.append_narrative(NarrativeEntry(round=2, author="player", content="B"))

    assert repo.max_narrative_round() == 2
    assert [e.content for e in repo.recent_narrative(1)] == ["B"]
    assert [e.content for e in repo.recent_narrative(5)] == ["A", "B"]


def test_adapter_delegates_snapshot_save_load_to_backing_store():
    from sidequest.game.session import GameSnapshot

    store = SqliteStore.open_in_memory()
    store.init_session("caverns_and_claudes", "iron_mines")
    repo = SqliteSaveRepository(store)

    assert repo.load() is None  # nothing saved yet
    snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="iron_mines")
    repo.save(snap)

    loaded = repo.load()
    assert loaded is not None
    assert loaded.snapshot.genre_slug == "caverns_and_claudes"
    assert loaded.snapshot.world_slug == "iron_mines"
