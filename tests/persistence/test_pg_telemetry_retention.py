"""Story 126-22 — turn_telemetry retention policy (RED).

Measured 2026-06-19: ``turn_telemetry`` held **470,647 rows** for 1,399
sessions and is explicitly excluded from the only existing cleanup
(``sessions._PER_SESSION_TABLES`` deliberately omits it as "global-lifecycle
... survive"), so it grows unbounded. Unlike ``projection_cache`` it is NOT
a rebuildable cache — it is forensic data (ADR-124 save-forensics), round-
attributed, and includes out-of-frame rows whose ``round`` is NULL. So the
policy is *retention*, not eviction: keep the last N rounds per session,
and never silently drop the un-attributable (NULL-round) infra rows.

This suite pins:
  * AC-2  ``prune_turn_telemetry(keep_last_rounds=N)`` retains only the most
          recent N distinct rounds for a session.
  * NULL-round (out-of-frame) rows are RETAINED by a round-based prune — a
          round filter must not silently delete rows it cannot attribute
          (No Silent Fallbacks).
  * AC-3  the prune emits a ``turn_telemetry.prune`` OTEL span carrying
          ``rows_pruned`` and ``session_id``.
  * WIRING the production save path applies the retention so the table is
          actually bounded in production, not just bounded on demand.

Real-Postgres harness mirrors ``tests/persistence/test_pg_events.py``.
Span constants imported, not string-matched.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game import db_pool
from sidequest.game.pg import sessions
from sidequest.game.pg._conn import session_tx
from sidequest.game.pg.events import PgEventStore, PgSaveTransaction
from sidequest.game.pg.save_repository import PgSaveRepository
from sidequest.game.session import GameSnapshot
from sidequest.telemetry import init_tracer

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, migrated_db: str) -> Iterator[PgEventStore]:
    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    pool = db_pool.get_pool()
    slug = f"telret_{uuid.uuid4().hex[:8]}"
    sid = sessions.ensure_session(pool, slug=slug, mode="solo", genre_slug="g", world_slug="w")
    yield PgEventStore(pool, session_id=sid)
    db_pool.close_pool()


@pytest.fixture
def repo(monkeypatch: pytest.MonkeyPatch, migrated_db: str) -> Iterator[PgSaveRepository]:
    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    pool = db_pool.get_pool()
    r = PgSaveRepository.for_slug(
        pool,
        slug=f"telret_repo_{uuid.uuid4().hex[:8]}",
        mode="solo",
        genre_slug="test_genre",
        world_slug="test_world",
    )
    yield r
    db_pool.close_pool()


@pytest.fixture
def otel_capture() -> Iterator[InMemorySpanExporter]:
    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    provider._active_span_processor._span_processors = ()  # type: ignore[attr-defined]
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_rounds(store: PgEventStore, rounds: list[int | None]) -> None:
    """Write one turn_telemetry row per entry in ``rounds`` (None => the
    out-of-frame NULL-round case)."""
    with session_tx(store._pool, store._sid) as conn:
        tx = PgSaveTransaction(conn, store._sid)
        for r in rounds:
            tx.write_telemetry(
                event_seq=None,
                round=r,
                ts=datetime.now(tz=UTC).isoformat(),
                component="mechanical",
                event_type="x",
                payload_json="{}",
            )


def _rounds_present(store: PgEventStore) -> list[int | None]:
    with store._pool.connection() as conn:
        rows = conn.execute(
            "SELECT round FROM turn_telemetry WHERE session_id = %s",
            (store._sid,),
        ).fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# AC-2 — keep last N rounds
# ---------------------------------------------------------------------------


def test_prune_turn_telemetry_keeps_last_n_rounds(store: PgEventStore) -> None:
    """Rows in rounds 1..10, ``keep_last_rounds=3`` => only rounds 8, 9, 10
    survive; rounds 1..7 (7 rows) are pruned and reported."""
    _write_rounds(store, list(range(1, 11)))

    deleted = store.prune_turn_telemetry(keep_last_rounds=3)

    assert deleted == 7, f"rounds 1..7 (7 rows) should be pruned; got {deleted}"
    assert sorted(r for r in _rounds_present(store) if r is not None) == [8, 9, 10]


def test_prune_turn_telemetry_retains_null_round_rows(store: PgEventStore) -> None:
    """A round-based retention must NOT delete out-of-frame rows whose
    ``round`` is NULL — they are not round-attributable, and silently
    dropping forensic infra rows violates No Silent Fallbacks.

    Rounds 1..5 plus 2 NULL-round rows; keep_last_rounds=2 retains rounds
    4 and 5 AND both NULL rows; only rounds 1, 2, 3 (3 rows) are pruned.
    """
    _write_rounds(store, [1, 2, 3, 4, 5, None, None])

    deleted = store.prune_turn_telemetry(keep_last_rounds=2)

    assert deleted == 3, f"only rounds 1, 2, 3 should be pruned; got {deleted}"
    present = _rounds_present(store)
    assert present.count(None) == 2, "both NULL-round rows must survive a round-based prune"
    assert sorted(r for r in present if r is not None) == [4, 5]


def test_prune_turn_telemetry_within_bound_is_a_noop(store: PgEventStore) -> None:
    """Fewer than keep_last_rounds distinct rounds => nothing pruned, 0 reported."""
    _write_rounds(store, [1, 2])

    deleted = store.prune_turn_telemetry(keep_last_rounds=5)

    assert deleted == 0
    assert sorted(r for r in _rounds_present(store) if r is not None) == [1, 2]


# ---------------------------------------------------------------------------
# AC-3 — observable prune
# ---------------------------------------------------------------------------


def test_prune_turn_telemetry_emits_otel_span(
    store: PgEventStore, otel_capture: InMemorySpanExporter
) -> None:
    """The retention sweep must emit one ``turn_telemetry.prune`` span with
    ``rows_pruned`` and ``session_id`` so the cleanup is observable."""
    from sidequest.telemetry.spans import SPAN_TURN_TELEMETRY_PRUNE

    _write_rounds(store, list(range(1, 7)))  # rounds 1..6

    store.prune_turn_telemetry(keep_last_rounds=2)  # keep 5, 6 => prune 1..4

    prune_spans = [
        s for s in otel_capture.get_finished_spans() if s.name == SPAN_TURN_TELEMETRY_PRUNE
    ]
    assert len(prune_spans) == 1, (
        f"expected one {SPAN_TURN_TELEMETRY_PRUNE!r} span; got {len(prune_spans)}. "
        f"all spans: {[s.name for s in otel_capture.get_finished_spans()]}"
    )
    attrs = dict(prune_spans[0].attributes or {})
    assert attrs.get("rows_pruned") == 4, f"span must record 4 rows pruned; got {attrs!r}"
    assert attrs.get("session_id") == store._sid, f"span must record session scope; got {attrs!r}"


# ---------------------------------------------------------------------------
# WIRING — production save path bounds the table
# ---------------------------------------------------------------------------


def test_save_applies_turn_telemetry_retention_in_production_path(
    repo: PgSaveRepository, otel_capture: InMemorySpanExporter
) -> None:
    """The routine production save path must apply the retention so
    turn_telemetry is bounded in production — not merely boundable on demand.
    Writes more rounds than the window, calls the real ``save``, and asserts
    only the last ``TURN_TELEMETRY_KEEP_LAST_ROUNDS`` rounds survive and a
    prune span fired."""
    from sidequest.game.pg.events import TURN_TELEMETRY_KEEP_LAST_ROUNDS
    from sidequest.telemetry.spans import SPAN_TURN_TELEMETRY_PRUNE

    keep = TURN_TELEMETRY_KEEP_LAST_ROUNDS
    over = keep + 3
    with repo.transaction() as tx:
        for r in range(1, over + 1):
            tx.write_telemetry(
                event_seq=None,
                round=r,
                ts=datetime.now(tz=UTC).isoformat(),
                component="mechanical",
                event_type="x",
                payload_json="{}",
            )

    repo.save(GameSnapshot(genre_slug="test_genre", world_slug="test_world"))

    with repo._pool.connection() as conn:
        rounds = [
            row[0]
            for row in conn.execute(
                "SELECT round FROM turn_telemetry WHERE session_id = %s AND round IS NOT NULL",
                (repo._sid,),
            ).fetchall()
        ]
    distinct = sorted(set(rounds))
    assert len(distinct) == keep, (
        f"production save must cap turn_telemetry to {keep} rounds; "
        f"found {len(distinct)} (no retention wired into the save path)"
    )
    assert distinct == list(range(over - keep + 1, over + 1))
    prune_spans = [
        s for s in otel_capture.get_finished_spans() if s.name == SPAN_TURN_TELEMETRY_PRUNE
    ]
    assert prune_spans, "production save path must emit a turn_telemetry.prune span"
