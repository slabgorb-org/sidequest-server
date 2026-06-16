"""Structural deadlock-impossibility for the telemetry write path (ADR-115 D5).

History: the SQLite watcher path (``_persist_turn_telemetry`` /
``_maybe_persist_encounter_row``) reached the bound store's ``_conn`` directly
and serialized every writer under a process-wide ``SAVE_WRITE_LOCK`` (RLock),
sniffing ``conn.in_transaction`` to decide whether to join an open turn or open
its own ``with conn:`` frame. That whole heuristic is DELETED in D5.

The deadlock is now STRUCTURAL, not lock-mediated:

  * The in-frame census rides the open turn ``SaveTransaction`` (same Pg
    connection) via ``tx.write_telemetry`` — threaded EXPLICITLY through
    ``publish_event(tx=, event_seq=)``. No second pooled connection is borrowed
    inside the turn tx, so the per-session ``FOR UPDATE`` row lock the turn
    already holds can never be contended by the census write.
  * Out-of-frame publishes go through ``TelemetrySink.record`` (its own short
    session_tx) — genuinely outside any open turn.

This file asserts that structural property over a REAL ``PgSaveRepository`` +
``PgTelemetrySink`` (no SQLite, no ``SAVE_WRITE_LOCK``), and that ``watcher_hub``
neither imports nor exposes ``SAVE_WRITE_LOCK`` any more.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from sidequest.game import db_pool
from sidequest.game.pg import sessions
from sidequest.game.pg.save_repository import PgSaveRepository
from sidequest.game.pg.telemetry import PgTelemetrySink
from sidequest.telemetry import watcher_hub
from sidequest.telemetry.watcher_hub import bind_event_store, publish_event


def _slug() -> str:
    return f"sq_swl_{uuid.uuid4().hex[:8]}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


@pytest.fixture
def repo_and_sink(monkeypatch, migrated_db: str):
    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    pool = db_pool.get_pool()
    slug = _slug()
    sid = sessions.ensure_session(pool, slug=slug, mode="solo", genre_slug="g", world_slug="w")
    repo = PgSaveRepository(pool, session_id=sid)
    sink = PgTelemetrySink(pool, sid)
    try:
        yield repo, sink, pool, sid
    finally:
        bind_event_store(None)
        db_pool.close_pool()


def test_in_frame_telemetry_rides_one_connection(repo_and_sink) -> None:
    """A turn that emits a census persists BOTH the event and the telemetry
    row on ONE transaction/connection — no second pooled connection is
    borrowed inside the open turn (the structural deadlock-impossibility).

    We bind a sink so that, if the implementation ever regressed to routing
    the in-frame write through the sink (a second connection), the
    ``event_seq`` would be NULL instead of the turn's seq — the assertion
    below would then fail.
    """
    repo, sink, pool, sid = repo_and_sink
    bind_event_store(sink)

    with repo.transaction() as tx:
        row = tx.append_event(kind="NARRATION", payload_json="{}")
        publish_event(
            "census",
            {"round": 1, "marker": "structural"},
            component="mechanical",
            tx=tx,
            event_seq=row.seq,
        )

    with pool.connection() as conn:
        trow = conn.execute(
            "SELECT event_seq FROM turn_telemetry WHERE session_id = %s AND event_type = %s",
            (sid, "census"),
        ).fetchone()
        erow = conn.execute(
            "SELECT seq FROM events WHERE session_id = %s AND seq = %s",
            (sid, row.seq),
        ).fetchone()
    assert erow is not None
    assert trow is not None and trow[0] == row.seq, (
        "in-frame telemetry must ride the SAME turn tx/connection (event_seq attributed), "
        "proving no second connection was borrowed"
    )


def test_in_frame_rollback_is_atomic(repo_and_sink) -> None:
    """One connection ⟹ the telemetry row rolls back atomically with the event."""
    repo, sink, pool, sid = repo_and_sink
    bind_event_store(sink)

    captured: list[int] = []
    with pytest.raises(RuntimeError, match="swl_rollback"), repo.transaction() as tx:
        row = tx.append_event(kind="DOOMED", payload_json="{}")
        publish_event("census", {"round": 1}, component="mechanical", tx=tx, event_seq=row.seq)
        captured.append(row.seq)
        raise RuntimeError("swl_rollback")

    seq = captured[0]
    with pool.connection() as conn:
        trow = conn.execute(
            "SELECT event_seq FROM turn_telemetry WHERE session_id = %s AND event_seq = %s",
            (sid, seq),
        ).fetchone()
    assert trow is None, "rolled-back in-frame telemetry must not persist"


def test_watcher_hub_no_longer_uses_save_write_lock() -> None:
    """The watcher_hub telemetry path no longer imports or exposes
    ``SAVE_WRITE_LOCK`` (the heuristic + lock are deleted). This is an
    attribute check on the live module, not a source-text grep."""
    assert not hasattr(watcher_hub, "SAVE_WRITE_LOCK"), (
        "watcher_hub must not expose SAVE_WRITE_LOCK — the per-write lock path is deleted"
    )
    # The deleted heuristic helpers are gone too; the rename to _telemetry_sink
    # is the only persistence-binding global that remains.
    assert not hasattr(watcher_hub, "_event_store"), (
        "the SQLite _event_store binding was renamed to _telemetry_sink"
    )
    assert hasattr(watcher_hub, "_telemetry_sink")
