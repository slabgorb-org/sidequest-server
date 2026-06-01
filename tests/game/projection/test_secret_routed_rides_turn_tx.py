"""Regression: the visibility-gated (SECRET_NOTE) projection path must NOT
open a second pooled connection while the turn transaction is open.

The bug (ADR-115 Postgres migration): emit_event opens
``with repo.transaction() as tx:`` which takes a per-session ``FOR UPDATE``
row lock on ``sessions`` (connection A). Inside that block the projection
fan-out reaches CoreInvariantStage's visibility-gated branch, which called
``_publish_secret_routed`` with no ``tx``. ``watcher_hub._persist_turn_telemetry``
then took the ``tx is None`` branch -> ``PgTelemetrySink.record`` opened a
SECOND ``session_tx`` (connection B) taking ``FOR UPDATE`` on the SAME row
connection A already locked -> self-deadlock.

This was harmless on SQLite (one in-process connection, no row lock) but
deadlocks under Postgres on ANY real MP turn that routes a SECRET_NOTE.

The fix threads the open turn ``tx`` (and ``event_seq``) down through
``ComposedFilter.project`` -> ``CoreInvariantStage.evaluate`` ->
``_publish_secret_routed`` -> ``watcher_hub.publish_event`` so the secret-
routed telemetry RIDES the turn tx (same connection) instead of opening a
competing ``session_tx``.

Mirrors the in-frame census contract in
``tests/game/test_mechanical_census_contract.py``: with ``tx`` threaded the
row is attributed ``event_seq`` and rolls back atomically with the turn.

``pytest-timeout`` turns the deadlock into a timeout failure rather than a
hang (the suite carries pytest-timeout).
"""

from __future__ import annotations

import json
import uuid

import pytest

from sidequest.game import db_pool
from sidequest.game.pg import sessions
from sidequest.game.pg.save_repository import PgSaveRepository
from sidequest.game.pg.telemetry import PgTelemetrySink
from sidequest.game.projection.composed import ComposedFilter
from sidequest.game.projection.envelope import MessageEnvelope
from sidequest.game.projection.view import SessionGameStateView
from sidequest.telemetry.watcher_hub import bind_event_store


@pytest.fixture
def repo_and_sink(monkeypatch, migrated_db: str):
    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    pool = db_pool.get_pool()
    slug = f"sq_secret_{uuid.uuid4().hex[:8]}"
    sid = sessions.ensure_session(
        pool, slug=slug, mode="multiplayer", genre_slug="g", world_slug="w"
    )
    repo = PgSaveRepository(pool, session_id=sid)
    sink = PgTelemetrySink(pool, sid)
    yield repo, sink, pool, sid
    db_pool.close_pool()


def _secret_envelope(seq: int) -> MessageEnvelope:
    """A SECRET_NOTE envelope visible only to alice — bob (the evaluated
    recipient) is excluded, so CoreInvariantStage's visibility-gated branch
    fires _publish_secret_routed for bob with malformed=False (happy path)."""
    payload = {
        "note": "the vault code is 1234",
        "_visibility": {"visible_to": ["alice"]},
    }
    return MessageEnvelope(
        kind="SECRET_NOTE",
        payload_json=json.dumps(payload),
        origin_seq=seq,
    )


@pytest.mark.timeout(30)
def test_secret_routed_publish_rides_open_turn_tx(repo_and_sink):
    """Inside an OPEN turn transaction (which holds the per-session FOR UPDATE
    row lock), running ComposedFilter.project on a visibility-gated SECRET_NOTE
    with tx/event_seq threaded must:

      (a) COMPLETE without opening a second pooled connection (no self-deadlock
          -> no timeout), and
      (b) write the invariant.secret_routed telemetry row riding the turn tx
          (event_seq attributed; visible to the in-flight tx, NOT yet committed).

    On pre-fix code _publish_secret_routed dropped tx and the sink opened a
    competing session_tx FOR UPDATE on the already-locked row -> deadlock ->
    this test times out.
    """
    repo, sink, pool, sid = repo_and_sink
    filt = ComposedFilter.with_no_genre_rules()
    # bob is a non-GM, non-author recipient — routed through the
    # visibility-gated exclusion (visible_to=["alice"], so include=False).
    view = SessionGameStateView()

    try:
        bind_event_store(sink)
        captured_seq: list[int] = []
        with pytest.raises(RuntimeError, match="force rollback"), repo.transaction() as tx:
            ev = tx.append_event(kind="SECRET_NOTE", payload_json="{}")
            envelope = _secret_envelope(ev.seq)

            # The load-bearing call: under the bug this hangs (second
            # connection FOR UPDATE on the locked row). Threading tx/event_seq
            # makes the telemetry write ride THIS tx instead.
            decision = filt.project(
                envelope=envelope,
                view=view,
                player_id="bob",
                tx=tx,
                event_seq=ev.seq,
            )
            # Visibility decision is UNCHANGED by the tx threading: bob is not
            # in visible_to, so excluded.
            assert decision.include is False
            assert decision.payload_json == ""

            captured_seq.append(ev.seq)
            inflight = [
                tuple(r)
                for r in tx._conn.execute(  # noqa: SLF001 — read the in-flight tx
                    "SELECT event_seq, component, event_type FROM turn_telemetry "
                    "WHERE session_id = %s",
                    (sid,),
                ).fetchall()
            ]
            # The secret_routed row rode the turn tx: event_seq attributed,
            # component='projection', state_transition event.
            assert inflight == [(ev.seq, "projection", "state_transition")], inflight
            raise RuntimeError("force rollback")

        seq = captured_seq[0]
        # Rolled back atomically with the turn — nothing committed.
        with pool.connection() as conn:
            t_count = conn.execute(
                "SELECT COUNT(*) FROM turn_telemetry WHERE session_id = %s AND event_seq = %s",
                (sid, seq),
            ).fetchone()[0]
        assert t_count == 0
    finally:
        bind_event_store(None)
