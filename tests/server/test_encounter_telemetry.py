"""Tests: ENCOUNTER_* state_transition events are persisted to the events table.

ADR-115 D5: the watcher hub's ``_maybe_persist_encounter_row`` no longer reaches
a SqliteStore's ``_conn`` — it routes encounter ``state_transition`` publishes
through the bound ``TelemetrySink.append_encounter_event`` (its own session_tx,
out of frame). These tests exercise that routing END-TO-END with a REAL
``PgTelemetrySink``: bind it via ``bind_event_store(sink)``, fire a watcher
``publish_event`` with ``field="encounter"``, and read the resulting row back
from the ``events`` table.

The GM-panel lie-detector contract: an encounter beat/resolution that fires a
watcher event MUST leave a typed ``ENCOUNTER_*`` row in ``events``.
"""

from __future__ import annotations

import json
import uuid

import pytest

from sidequest.game import db_pool
from sidequest.game.pg import sessions
from sidequest.game.pg.telemetry import PgTelemetrySink
from sidequest.telemetry.watcher_hub import bind_event_store, publish_event


def _slug() -> str:
    return f"sq_enc_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def sink(monkeypatch, migrated_db: str):
    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    pool = db_pool.get_pool()
    sid = sessions.ensure_session(pool, slug=_slug(), mode="solo", genre_slug="g", world_slug="w")
    s = PgTelemetrySink(pool, sid)
    bind_event_store(s)
    try:
        yield s, pool, sid
    finally:
        bind_event_store(None)
        db_pool.close_pool()


def _events(pool, sid):
    with pool.connection() as conn:
        return conn.execute(
            "SELECT kind, payload_json FROM events WHERE session_id = %s ORDER BY seq",
            (sid,),
        ).fetchall()


def test_beat_applied_writes_event_row(sink) -> None:
    """A ``op="beat_applied"`` encounter publish lands an ENCOUNTER_BEAT_APPLIED
    row with its payload preserved (the kind mapping + payload-passthrough
    behavior the old SqliteStore test asserted)."""
    _s, pool, sid = sink
    publish_event(
        "state_transition",
        {
            "field": "encounter",
            "op": "beat_applied",
            "actor_side": "player",
            "beat_kind": "strike",
            "outcome_tier": "Success",
        },
        component="encounter",
    )

    rows = _events(pool, sid)
    kinds = [r[0] for r in rows]
    assert "ENCOUNTER_BEAT_APPLIED" in kinds
    payload = next(json.loads(r[1]) for r in rows if r[0] == "ENCOUNTER_BEAT_APPLIED")
    assert payload["actor_side"] == "player"
    assert payload["beat_kind"] == "strike"
    assert payload["outcome_tier"] == "Success"


def test_narrator_dial_advance_writes_event_row(sink) -> None:
    """A ``op="narrator_dial_advance"`` encounter publish lands an
    ENCOUNTER_NARRATOR_DIAL_ADVANCE row with its before/after/delta preserved.

    Playtest 2026-06-10 (coyote_star dogfight): the narrator advances the
    dial_threshold dogfight's energy dials via the ``advance_confrontation``
    tool. That tool emits this watcher event with full before/after/delta, but
    ``narrator_dial_advance`` was absent from ``_KIND_BY_OP`` so the row was
    silently dropped from the events table. Result: the GM-panel EncounterTab
    timeline (and the DRIVER's ``/encounter_events`` forensic pull) showed
    ENCOUNTER_BEAT_APPLIED with Δ0 and no dial-advance trail — the opponent's
    energy climbed 10→20 with zero attribution. The dial move MUST leave a typed
    ENCOUNTER_* row so "why is the enemy winning" is answerable from the trail.
    """
    _s, pool, sid = sink
    publish_event(
        "state_transition",
        {
            "field": "encounter",
            "op": "narrator_dial_advance",
            "encounter_type": "dogfight",
            "axis": "opponent",
            "delta": 5,
            "reason": "contact climbs across the six",
            "value_before": 10,
            "value_after": 15,
            "threshold": 30,
            "crossed_threshold": False,
            "source": "advance_confrontation",
        },
        component="encounter",
    )

    rows = _events(pool, sid)
    kinds = [r[0] for r in rows]
    assert "ENCOUNTER_NARRATOR_DIAL_ADVANCE" in kinds, (
        "narrator dial advance must persist a typed ENCOUNTER_* row so the "
        "dial move is attributable in the GM-panel timeline"
    )
    payload = next(json.loads(r[1]) for r in rows if r[0] == "ENCOUNTER_NARRATOR_DIAL_ADVANCE")
    assert payload["axis"] == "opponent"
    assert payload["delta"] == 5
    assert payload["value_before"] == 10
    assert payload["value_after"] == 15


def test_resolution_writes_event_row_with_structured_outcome(sink) -> None:
    """A ``op="resolved"`` encounter publish lands ENCOUNTER_RESOLVED with the
    structured outcome preserved."""
    _s, pool, sid = sink
    publish_event(
        "state_transition",
        {"field": "encounter", "op": "resolved", "outcome": "opponent_victory"},
        component="encounter",
    )

    rows = _events(pool, sid)
    assert rows, "no encounter events were persisted"
    assert rows[-1][0] == "ENCOUNTER_RESOLVED"
    payload = json.loads(rows[-1][1])
    assert payload["outcome"] == "opponent_victory"


def test_encounter_events_persisted_in_order(sink) -> None:
    """Multiple encounter ops persist in publish order with ENCOUNTER_RESOLVED
    last (the timeline-ordering contract the old test covered)."""
    _s, pool, sid = sink
    publish_event(
        "state_transition",
        {"field": "encounter", "op": "started"},
        component="encounter",
    )
    publish_event(
        "state_transition",
        {"field": "encounter", "op": "beat_applied", "outcome_tier": "Success"},
        component="encounter",
    )
    publish_event(
        "state_transition",
        {"field": "encounter", "op": "resolved", "outcome": "opponent_victory"},
        component="encounter",
    )

    rows = _events(pool, sid)
    kinds = [r[0] for r in rows]
    assert kinds == ["ENCOUNTER_STARTED", "ENCOUNTER_BEAT_APPLIED", "ENCOUNTER_RESOLVED"]


def test_non_encounter_state_transition_persists_no_event_row(sink) -> None:
    """A state_transition that is NOT field="encounter" must write NO encounter
    row (the gating negative case)."""
    _s, pool, sid = sink
    publish_event(
        "state_transition",
        {"field": "intent", "label": "explore"},
        component="intent",
    )

    rows = _events(pool, sid)
    assert rows == [], "non-encounter state_transition must not write an events row"


def test_unmapped_encounter_op_persists_no_event_row(sink) -> None:
    """An encounter op with no _KIND_BY_OP mapping writes nothing (honest skip,
    not a fabricated row)."""
    _s, pool, sid = sink
    publish_event(
        "state_transition",
        {"field": "encounter", "op": "not_a_real_op"},
        component="encounter",
    )

    rows = _events(pool, sid)
    assert rows == [], "unmapped encounter op must not write an events row"
