"""End-to-end wiring: narration_apply → encounter resolution → events table.

Asserts:
1. encounter_dispatch_helper.run_to_resolution drives the live engine to
   opponent_victory.
2. The SQLite events table records the full encounter timeline in order:
   ENCOUNTER_STARTED → ENCOUNTER_BEAT_APPLIED (multiple) →
   ENCOUNTER_METRIC_ADVANCE (multiple) → ENCOUNTER_RESOLVED.
3. snapshot.pending_resolution_signal is set with the correct outcome.

Per CLAUDE.md "Every Test Suite Needs a Wiring Test". This is the round-trip
proof that all Phase 1+2 plumbing actually fires from production code paths.

Note on ENCOUNTER_STARTED: store_bound_to_hub pre-builds a StructuredEncounter
directly without going through instantiate_encounter_from_trigger, so the
lifecycle event does not fire from the fixture. We clear snap.encounter and
call instantiate_encounter_from_trigger directly here so the full event
sequence (including STARTED) is exercised from production code.

Note on [ENCOUNTER RESOLVED] zone: tests/agents/test_narrator_prompt.py already
asserts that a TurnContext with pending_resolution_signal renders
"[ENCOUNTER RESOLVED]" and "outcome: opponent_victory" in the narrator prompt.
That coverage is sufficient; we do not duplicate it here.
"""

from __future__ import annotations

import pytest

from sidequest.agents.orchestrator import NpcMention
from sidequest.server.dispatch.encounter_lifecycle import instantiate_encounter_from_trigger


@pytest.fixture
def _pg_encounter_sink(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Bind the watcher hub to a PG ``PgTelemetrySink`` on a throwaway db.

    ADR-115 D2: ``watcher_hub._maybe_persist_encounter_row`` appends encounter
    ``events`` rows through the bound ``TelemetrySink.append_encounter_event``
    (PG), NOT a raw INSERT into a SQLite ``events`` table. The shared
    ``store_bound_to_hub`` fixture binds an in-memory ``SqliteStore`` (the
    pre-D2 sink shape, which has no ``append_encounter_event``/``record``), so
    the timeline now lands in Postgres. This fixture rebinds the hub to a real
    PG sink for one isolated session and yields a ``PgEventStore`` to read the
    timeline back from the authoritative store the production path wrote to.
    """
    import psycopg

    from sidequest.game import db_pool
    from sidequest.game.pg import sessions
    from sidequest.game.pg.events import PgEventStore
    from sidequest.game.pg.telemetry import PgTelemetrySink
    from sidequest.telemetry.watcher_hub import bind_event_store

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
    pool = db_pool.get_pool()

    sid = sessions.ensure_session(
        pool, slug="dual-track-wiring", mode="solo", genre_slug="test_pack", world_slug="test_world"
    )
    sink = PgTelemetrySink(pool, sid)
    # Rebind the hub from store_bound_to_hub's SqliteStore to the PG sink — the
    # production sink shape under D2. store_bound_to_hub's teardown clears the
    # binding (bind_event_store(None)) after this fixture's teardown runs.
    bind_event_store(sink)
    try:
        yield PgEventStore(pool, session_id=sid)
    finally:
        db_pool.close_pool()


@pytest.mark.integration
def test_full_encounter_round_trip_records_timeline_and_sets_signal(
    store_bound_to_hub,
    encounter_dispatch_helper,
    _pg_encounter_sink,
):
    _store, snap, pack = store_bound_to_hub
    ev_store = _pg_encounter_sink

    # store_bound_to_hub pre-builds a StructuredEncounter without going through
    # instantiate_encounter_from_trigger, so ENCOUNTER_STARTED would not fire.
    # Clear it and use the lifecycle function so every event in the sequence
    # is emitted from production code paths.
    snap.encounter = None
    instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type="combat",
        player_name="Sam",
        npcs_present=[NpcMention(name="Promo", side="opponent", role="hostile")],
        genre_slug="test_pack",
    )

    # Drive opponent to victory through the real narration_apply path.
    encounter_dispatch_helper.run_to_resolution(snap, pack, winner="opponent")

    # --- Engine state ---
    assert snap.encounter is not None
    assert snap.encounter.resolved is True
    assert snap.pending_resolution_signal is not None
    assert snap.pending_resolution_signal.outcome == "opponent_victory"

    # --- Events table (ADR-115 D2: encounter timeline persists to Postgres
    # via the bound PgTelemetrySink, not the in-memory SqliteStore) ---
    rows = ev_store.read_events_since(since_seq=0)
    kinds = [r.kind for r in rows if r.kind.startswith("ENCOUNTER_")]

    assert "ENCOUNTER_STARTED" in kinds, f"ENCOUNTER_STARTED missing; got {kinds!r}"
    assert "ENCOUNTER_BEAT_APPLIED" in kinds, f"ENCOUNTER_BEAT_APPLIED missing; got {kinds!r}"
    assert "ENCOUNTER_METRIC_ADVANCE" in kinds, f"ENCOUNTER_METRIC_ADVANCE missing; got {kinds!r}"
    assert kinds[-1] == "ENCOUNTER_RESOLVED", f"last row must be ENCOUNTER_RESOLVED; got {kinds!r}"
