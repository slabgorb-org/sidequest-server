"""Concurrent-writer regression test for ``SAVE_WRITE_LOCK``.

Source spec: docs/superpowers/specs/2026-05-24-sqlite-write-race-fix-design.md
Source plan: docs/superpowers/plans/2026-05-24-sqlite-write-race-fix.md

Fires concurrent writes from N=8 threads against a file-backed
``SqliteStore`` simulating one MP playtest's worth of activity:
    - 2 threads: ``store.save(snapshot)``      — snapshot-save path (site #4)
    - 2 threads: ``store.append_narrative(e)`` — narrative log     (site #6)
    - 2 threads: C2 event-append + mechanical_census stand-in
                                                — reentry case   (site #10 → #14)
    - 2 threads: bare ``publish_event(...)``   — telemetry only   (site #14)

Pre-fix (sites unwrapped): the watcher path swallows
``sqlite3.OperationalError: database is locked`` in its try/except, so
this test detects races via ``caplog`` for the
``turn_telemetry.sink_failed`` warning AND via OperationalError surfacing
from any non-telemetry writer.

Post-fix: zero failures, zero swallowed warnings, every non-NULL
``event_seq`` in ``turn_telemetry`` matches a real ``events.seq``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from sidequest.game import persistence as persistence_module
from sidequest.game.persistence import SAVE_WRITE_LOCK, SqliteStore
from sidequest.telemetry.watcher_hub import bind_event_store, publish_event

# -------- helpers --------


def _open_store(tmp_path) -> SqliteStore:
    return SqliteStore.open(str(tmp_path / "save.db"))


def _append_synthetic_event(store: SqliteStore, kind: str = "NARRATION") -> int:
    """Insert one row into ``events`` and return its seq.

    Used by the C2 stand-in thread group to fire the in-transaction
    reentry path through ``publish_event`` → ``_persist_turn_telemetry``.
    """
    conn = store._conn
    cur = conn.execute(
        "INSERT INTO events (kind, payload_json, created_at) VALUES (?, ?, ?)",
        (kind, "{}", "t"),
    )
    seq = cur.lastrowid
    assert seq is not None
    return int(seq)


# -------- fixtures --------


@pytest.fixture
def store(tmp_path):
    s = _open_store(tmp_path)
    bind_event_store(s)
    try:
        yield s
    finally:
        bind_event_store(None)
        s.close()


# -------- the main concurrent regression --------

ITERATIONS_PER_THREAD = 60  # 60 * 8 = 480 total ops, ≈ one playtest's worth


def test_concurrent_writers_no_race(store, caplog):
    """N=8 concurrent writers across all 4 thread groups; expect zero
    OperationalErrors and zero ``turn_telemetry.sink_failed`` warnings."""
    caplog.set_level(logging.WARNING)

    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def record(exc: BaseException) -> None:
        with errors_lock:
            errors.append(exc)

    # --- Group A: snapshot save ---
    def saver():
        from sidequest.game.session import GameSnapshot

        for _ in range(ITERATIONS_PER_THREAD):
            try:
                snap = GameSnapshot()
                store.save(snap)
            except sqlite3.OperationalError as exc:
                record(exc)
            except Exception as exc:  # noqa: BLE001
                record(exc)

    # --- Group B: narrative append ---
    def narrator():
        from sidequest.game.session import NarrativeEntry

        for i in range(ITERATIONS_PER_THREAD):
            try:
                store.append_narrative(
                    NarrativeEntry(
                        timestamp=0,
                        round=i,
                        author="player",
                        content="x",
                        tags=[],
                    )
                )
            except sqlite3.OperationalError as exc:
                record(exc)
            except Exception as exc:  # noqa: BLE001
                record(exc)

    # --- Group C: C2 event-append + reentry through publish_event ---
    def c2_stand_in():
        """Simulates the production C2 path: hold SAVE_WRITE_LOCK and a
        ``with conn:`` open, append an event, then call publish_event
        which re-enters _persist_turn_telemetry which re-acquires the
        lock. With RLock this must not deadlock."""
        conn = store._conn
        for i in range(ITERATIONS_PER_THREAD):
            try:
                with SAVE_WRITE_LOCK, conn:
                    _append_synthetic_event(store)
                    publish_event(
                        "state_transition",
                        {"field": "mechanical", "round": i},
                        component="mechanical_census",
                    )
            except sqlite3.OperationalError as exc:
                record(exc)
            except Exception as exc:  # noqa: BLE001
                record(exc)

    # --- Group D: telemetry-only (no open turn txn) ---
    def telemetry_only():
        for i in range(ITERATIONS_PER_THREAD):
            try:
                publish_event(
                    "state_transition",
                    {"field": "intent", "label": "explore", "round": i},
                    component="intent",
                )
            except sqlite3.OperationalError as exc:
                record(exc)
            except Exception as exc:  # noqa: BLE001
                record(exc)

    workers = [
        saver,
        saver,
        narrator,
        narrator,
        c2_stand_in,
        c2_stand_in,
        telemetry_only,
        telemetry_only,
    ]

    with ThreadPoolExecutor(max_workers=len(workers)) as pool:
        futures = [pool.submit(fn) for fn in workers]
        for f in as_completed(futures):
            f.result()

    # Assertion 1: no OperationalError observed in any thread.
    op_errors = [e for e in errors if isinstance(e, sqlite3.OperationalError)]
    assert op_errors == [], f"OperationalErrors leaked: {op_errors!r}"

    # Assertion 2: no other surprise exceptions.
    other_errors = [e for e in errors if not isinstance(e, sqlite3.OperationalError)]
    assert other_errors == [], f"unexpected thread errors: {other_errors!r}"

    # Assertion 3: telemetry sink never logged ``sink_failed``.
    sink_failed = [
        rec for rec in caplog.records if "turn_telemetry.sink_failed" in rec.getMessage()
    ]
    assert sink_failed == [], (
        f"turn_telemetry.sink_failed warnings: {[r.getMessage() for r in sink_failed]}"
    )

    # Assertion 4 (atomicity, replaces spec assertion #4 — no FK in schema):
    # every non-NULL ``turn_telemetry.event_seq`` matches an existing
    # ``events.seq``. Holds iff the in-transaction branch's "ride the open
    # event-frame transaction" path persisted atomically with the event row.
    orphans = store._conn.execute(
        "SELECT t.event_seq FROM turn_telemetry t "
        "LEFT JOIN events e ON e.seq = t.event_seq "
        "WHERE t.event_seq IS NOT NULL AND e.seq IS NULL"
    ).fetchall()
    assert orphans == [], f"telemetry rows with orphan event_seq: {orphans!r}"

    # Assertion 5: at least *some* telemetry rows landed with a non-NULL
    # event_seq — proves the C2 reentry path actually fired.
    inflight = store._conn.execute(
        "SELECT COUNT(*) FROM turn_telemetry WHERE event_seq IS NOT NULL"
    ).fetchone()[0]
    assert inflight > 0, "no in-transaction telemetry rows — group C never fired"


# -------- load-path checkpoint lock regression --------


def test_load_canonicalize_checkpoint_holds_write_lock(tmp_path, monkeypatch):
    """The WAL checkpoint on the ``load()`` canonicalize-backup path must run
    under ``SAVE_WRITE_LOCK``.

    Regression for the slipped writer the #413 sweep missed: it wrapped the
    9 obvious writer *methods*, but ``load()`` (a read method) also issues a
    ``PRAGMA wal_checkpoint(TRUNCATE)`` write when it canonicalizes a legacy
    save. Under MP reconnect storms an unlocked checkpoint here races a live
    ``save()`` on another connection → "database is locked".

    We force the canonicalize path by augmenting a freshly-saved snapshot
    with a legacy ``world_confrontations`` field (which the migration pops,
    making ``migrated != raw``), then assert the lock is held at the moment
    the backup copy runs — that copy sits inside the same
    ``with SAVE_WRITE_LOCK`` block as the checkpoint.
    """
    from sidequest.game.session import GameSnapshot

    store = SqliteStore.open(str(tmp_path / "save.db"))
    store.save(GameSnapshot())

    # Augment the stored snapshot with a legacy field so the next load()
    # migrates (migrated != raw) and takes the checkpoint+backup branch.
    with SAVE_WRITE_LOCK, store._conn:
        row = store._conn.execute("SELECT snapshot_json FROM game_state WHERE id = 1").fetchone()
        data = json.loads(row["snapshot_json"])
        data["world_confrontations"] = []
        store._conn.execute(
            "UPDATE game_state SET snapshot_json = ? WHERE id = 1",
            (json.dumps(data),),
        )

    lock_held_at_copy: dict[str, bool] = {}
    real_copy2 = persistence_module.shutil.copy2

    def _spy_copy2(src, dst, *args, **kwargs):
        # copy2 runs immediately after the checkpoint, inside the same
        # ``with SAVE_WRITE_LOCK`` block — so ownership here proves the
        # checkpoint write was serialized too.
        lock_held_at_copy["held"] = SAVE_WRITE_LOCK._is_owned()
        return real_copy2(src, dst, *args, **kwargs)

    monkeypatch.setattr(persistence_module.shutil, "copy2", _spy_copy2)

    result = store.load()

    assert result is not None
    assert lock_held_at_copy.get("held") is True, (
        "load()'s canonicalize checkpoint+backup must run under SAVE_WRITE_LOCK"
    )
    assert (tmp_path / "save.db.canonicalize.bak").exists(), (
        "the canonicalize path must have fired (otherwise the test is vacuous)"
    )
    store.close()


# -------- reentrancy unit test --------


def test_save_write_lock_is_reentrant():
    """The lock must be a ``threading.RLock``-shaped object that allows
    the same thread to re-acquire without blocking."""
    acquired_twice = SAVE_WRITE_LOCK.acquire(blocking=False)
    try:
        assert acquired_twice
        re = SAVE_WRITE_LOCK.acquire(blocking=False)
        try:
            assert re, "SAVE_WRITE_LOCK must be reentrant (RLock)"
        finally:
            if re:
                SAVE_WRITE_LOCK.release()
    finally:
        if acquired_twice:
            SAVE_WRITE_LOCK.release()


# -------- watcher_hub rename smoke --------


def test_watcher_hub_uses_save_write_lock():
    """After the rename, ``watcher_hub`` no longer owns a private
    ``_persist_lock``. Future writers grepping for "persist lock" should
    land on the doc block in ``persistence.py`` and nothing else."""
    from sidequest.telemetry import watcher_hub

    assert not hasattr(watcher_hub, "_persist_lock"), (
        "watcher_hub._persist_lock should have been removed and replaced "
        "by SAVE_WRITE_LOCK imported from sidequest.game.persistence"
    )
    assert watcher_hub.SAVE_WRITE_LOCK is SAVE_WRITE_LOCK
