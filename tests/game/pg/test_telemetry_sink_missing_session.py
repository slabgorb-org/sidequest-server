"""Out-of-frame telemetry against a non-existent session must DROP with ONE
loud line — not a per-event ForeignKeyViolation traceback (playtest 2026-06-11).

Root cause: ``session_tx`` runs ``SELECT 1 FROM sessions ... FOR UPDATE`` but
discards the result. When the bound ``session_id`` has no ``sessions`` row (a
stale sink that outlived a torn-down room, or a bind-before-commit race), the
SELECT locks nothing, the INSERT proceeds, and the FK constraint throws an
opaque ``ForeignKeyViolation``. ``_persist_turn_telemetry`` catches it but logs
``exc_info=True`` — a full traceback PER EVENT — so every out-of-frame
``validator/state_transition`` for the dead session is dropped AND fills the log
with stacks. The fix makes "session row missing" a typed, catchable condition
that logs exactly one clean line; genuine sink bugs keep the traceback.
"""

from __future__ import annotations

import logging

import psycopg
import pytest

from sidequest.game import db_pool


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
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


_MISSING_SID = 4242  # no sessions row is ever seeded for this id


def test_record_raises_typed_error_not_fk_violation_on_missing_session() -> None:
    """``PgTelemetrySink.record`` against a session_id with no ``sessions`` row
    raises the typed ``OutOfFrameSessionMissing`` (carrying the id) BEFORE the
    INSERT, never an opaque psycopg ``ForeignKeyViolation``."""
    from sidequest.game.pg.telemetry import OutOfFrameSessionMissing, PgTelemetrySink

    sink = PgTelemetrySink(db_pool.get_pool(), _MISSING_SID)

    with pytest.raises(OutOfFrameSessionMissing) as excinfo:
        sink.record(
            round=1,
            ts="2026-06-11T00:00:00+00:00",
            component="validator",
            event_type="state_transition",
            payload_json="{}",
        )
    assert excinfo.value.session_id == _MISSING_SID


def test_persist_turn_telemetry_drops_missing_session_with_one_clean_line(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The out-of-frame path drops a write to a non-existent session with
    exactly ONE warning and NO traceback (``exc_info`` unset) — distinct from
    the generic ``sink_failed`` path that keeps the stack for real bugs."""
    from sidequest.telemetry import watcher_hub
    from sidequest.game.pg.telemetry import PgTelemetrySink

    stale_sink = PgTelemetrySink(db_pool.get_pool(), _MISSING_SID)
    monkeypatch.setattr(watcher_hub, "_resolve_out_of_frame_sink", lambda: stale_sink)

    with caplog.at_level(logging.WARNING, logger=watcher_hub.logger.name):
        # Must not raise — telemetry never crashes a turn.
        watcher_hub._persist_turn_telemetry(
            "state_transition",
            {"round": 1},
            "validator",
            tx=None,
            event_seq=None,
        )

    drops = [
        r
        for r in caplog.records
        if "no_session" in r.getMessage() or "missing" in r.getMessage().lower()
    ]
    assert len(drops) == 1, (
        f"expected exactly one clean drop line; got {[r.getMessage() for r in caplog.records]}"
    )
    rec = drops[0]
    assert rec.exc_info is None, "drop line must NOT carry a traceback (that's the per-event spam)"
    # The generic traceback path must NOT have fired for a known missing-session drop.
    assert not any(
        "sink_failed" in r.getMessage() and r.exc_info is not None for r in caplog.records
    ), "missing-session drop went down the traceback-logging sink_failed path"
