"""Story 126-22 — projection_cache bounded-growth retention (RED).

Measured 2026-06-19 during a save-DB rotation: ``projection_cache`` held
**4,313,951 rows** for only 1,399 sessions (~3,000 rows/session), dwarfing
every other save table combined — the "too much garbage in the save DB"
bloat. Root cause: ``PgEventStore.write_projection`` upserts one row per
``(session_id, event_seq, player_id)`` on every event for every connected
player, and **nothing ever deletes them** during a session's life
(``init_session`` clears them only on a slot *reinit*, which organic
single-session-per-slug play never triggers).

``projection_cache`` is a pure cache: ``projection/cache_fill.lazy_fill``
rebuilds any missing rows from the event log on reconnect (re-projecting
against the current view — the documented mid-session-join softening). So
pruning is *recoverable* — a far-behind player's rows are re-filled on
their next connect, and a caught-up player only ever reads
``read_projection_since(since_seq=last_seen_seq)``, i.e. the head of the
window. That makes a bounded per-player window safe.

This suite pins the contract for the fix:
  * AC-1  ``prune_projection_cache(keep_last_per_player=K)`` bounds the
          table to the newest K event_seqs per (session, player).
  * AC-3  the prune emits a ``projection.cache.prune`` OTEL span carrying
          ``rows_pruned`` and ``session_id`` (scope) — so the GM panel can
          see the cleanup fire instead of it being silent.
  * WIRING the routine production save path (``PgSaveRepository.save``)
          enforces the bound — the bug was "nothing calls prune", so a
          prune method with no production caller would NOT fix it.

Per CLAUDE.md "No Source-Text Wiring Tests": every assertion drives the
real PgEventStore / PgSaveRepository against a migrated Postgres and checks
row counts or emitted spans — never source-code patterns. Span constants
are imported (not string-matched) to survive refactor.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game import db_pool
from sidequest.game.pg import sessions
from sidequest.game.pg.events import PgEventStore
from sidequest.game.pg.save_repository import PgSaveRepository
from sidequest.game.projection_filter import FilterDecision
from sidequest.game.session import GameSnapshot
from sidequest.telemetry import init_tracer

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, migrated_db: str) -> Iterator[PgEventStore]:
    """A ``PgEventStore`` bound to a freshly-migrated throwaway Postgres db.

    Mirrors ``tests/persistence/test_pg_events.py::store`` exactly so the
    retention tests share the project's canonical real-PG harness.
    """
    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    pool = db_pool.get_pool()
    slug = f"projret_{uuid.uuid4().hex[:8]}"
    sid = sessions.ensure_session(pool, slug=slug, mode="solo", genre_slug="g", world_slug="w")
    yield PgEventStore(pool, session_id=sid)
    db_pool.close_pool()


@pytest.fixture
def repo(monkeypatch: pytest.MonkeyPatch, migrated_db: str) -> Iterator[PgSaveRepository]:
    """A real ``PgSaveRepository`` for the production-path wiring test."""
    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    pool = db_pool.get_pool()
    r = PgSaveRepository.for_slug(
        pool,
        slug=f"projret_repo_{uuid.uuid4().hex[:8]}",
        mode="solo",
        genre_slug="test_genre",
        world_slug="test_world",
    )
    yield r
    db_pool.close_pool()


@pytest.fixture
def otel_capture() -> Iterator[InMemorySpanExporter]:
    """Capture finished OTEL spans in-memory (mirrors the seed-emission suite)."""
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


def _seed_projection_rows(store: PgEventStore, *, n_events: int, players: list[str]) -> None:
    """Append ``n_events`` events and write a projection row per player each.

    Produces ``n_events * len(players)`` projection_cache rows at event_seqs
    1..n_events for every player — the per-event-per-player fan-out that is
    the measured bloat.
    """
    for _ in range(n_events):
        ev = store.append_event(kind="NARRATION", payload_json="{}")
        for pid in players:
            store.write_projection(
                event_seq=ev.seq,
                player_id=pid,
                decision=FilterDecision(include=True, payload_json='{"x":1}'),
            )


def _projection_count(store: PgEventStore) -> int:
    with store._pool.connection() as conn:
        return int(
            conn.execute(
                "SELECT count(*) FROM projection_cache WHERE session_id = %s",
                (store._sid,),
            ).fetchone()[0]
        )


def _seqs_for_player(store: PgEventStore, player_id: str) -> set[int]:
    rows = store.read_projection_since(player_id=player_id, since_seq=0)
    return {r.event_seq for r in rows}


# ---------------------------------------------------------------------------
# AC-1 — bounded per-(session, player) growth
# ---------------------------------------------------------------------------


def test_prune_projection_cache_keeps_only_last_n_per_player(store: PgEventStore) -> None:
    """After 12 events for 2 players (24 rows), ``prune_projection_cache``
    with ``keep_last_per_player=5`` must leave exactly the newest 5 event_seqs
    per player (10 rows) and report 14 rows deleted.

    This is the core bloat fix: per-(session, player) growth is bounded, not
    unbounded ~3k/session accumulation.
    """
    _seed_projection_rows(store, n_events=12, players=["p1", "p2"])
    assert _projection_count(store) == 24

    deleted = store.prune_projection_cache(keep_last_per_player=5)

    assert deleted == 14, f"expected 24 - (2 * 5) = 14 rows pruned, got {deleted}"
    assert _projection_count(store) == 10
    # Newest 5 seqs survive for each player; the rest are gone.
    assert _seqs_for_player(store, "p1") == {8, 9, 10, 11, 12}
    assert _seqs_for_player(store, "p2") == {8, 9, 10, 11, 12}


def test_prune_keeps_the_head_each_live_player_resumes_from(store: PgEventStore) -> None:
    """The single newest projection row per player must NEVER be pruned.

    A live/caught-up player resumes by reading
    ``read_projection_since(since_seq=last_seen_seq)`` — pruning the head of
    the window would blank their narrative pane on reconnect. Guards against
    an over-aggressive prune that eats the rows live resume depends on.
    """
    _seed_projection_rows(store, n_events=8, players=["solo"])

    store.prune_projection_cache(keep_last_per_player=3)

    # The most-recent event's projection survives.
    tail = store.read_projection_since(player_id="solo", since_seq=7)
    assert [r.event_seq for r in tail] == [8]


def test_prune_within_bound_is_a_noop(store: PgEventStore) -> None:
    """When a player already has <= keep_last_per_player rows, prune deletes
    nothing and reports 0 — idempotent, no surprise data loss."""
    _seed_projection_rows(store, n_events=3, players=["p1"])

    deleted = store.prune_projection_cache(keep_last_per_player=5)

    assert deleted == 0
    assert _seqs_for_player(store, "p1") == {1, 2, 3}


# ---------------------------------------------------------------------------
# AC-3 — the prune is observable (OTEL span), not silent
# ---------------------------------------------------------------------------


def test_prune_emits_otel_span_with_count_and_scope(
    store: PgEventStore, otel_capture: InMemorySpanExporter
) -> None:
    """The prune must emit exactly one ``projection.cache.prune`` span
    carrying ``rows_pruned`` (how many) and ``session_id`` (the scope), so
    the GM panel / save-forensics can see the cleanup fire instead of it
    being an invisible side effect (CLAUDE.md OTEL Observability Principle).
    """
    from sidequest.telemetry.spans import SPAN_PROJECTION_CACHE_PRUNE

    _seed_projection_rows(store, n_events=10, players=["p1"])

    store.prune_projection_cache(keep_last_per_player=4)

    prune_spans = [
        s for s in otel_capture.get_finished_spans() if s.name == SPAN_PROJECTION_CACHE_PRUNE
    ]
    assert len(prune_spans) == 1, (
        f"expected exactly one {SPAN_PROJECTION_CACHE_PRUNE!r} span; got {len(prune_spans)}. "
        f"all spans: {[s.name for s in otel_capture.get_finished_spans()]}"
    )
    attrs = dict(prune_spans[0].attributes or {})
    assert attrs.get("rows_pruned") == 6, f"span must record 10 - 4 = 6 pruned; got {attrs!r}"
    assert attrs.get("session_id") == store._sid, (
        f"span must record the session scope; got {attrs!r}"
    )


# ---------------------------------------------------------------------------
# WIRING — the production save path enforces the bound
# ---------------------------------------------------------------------------


def test_save_enforces_projection_cache_bound_in_production_path(
    repo: PgSaveRepository, otel_capture: InMemorySpanExporter
) -> None:
    """The measured bug was "nothing prunes" — so the bound must be enforced
    by a real production code path, not merely available as a method.

    ``PgSaveRepository.save`` is the routine persistence path hit on every
    turn and on disconnect. After writing more than the keep-last window for
    a player and then calling the real ``save``, the player's projection rows
    must be capped to ``PROJECTION_CACHE_KEEP_LAST_PER_PLAYER`` and a
    ``projection.cache.prune`` span must have fired. (If a future design
    wires the trigger elsewhere — disconnect, inline-on-write — re-point this
    one test; SOME production path must bound the table.)
    """
    from sidequest.game.pg.events import PROJECTION_CACHE_KEEP_LAST_PER_PLAYER
    from sidequest.telemetry.spans import SPAN_PROJECTION_CACHE_PRUNE

    keep = PROJECTION_CACHE_KEEP_LAST_PER_PLAYER
    over = keep + 5
    # Bulk-write in one transaction so the wiring test stays fast regardless
    # of the production window size.
    with repo.transaction() as tx:
        for _ in range(over):
            ev = tx.append_event(kind="NARRATION", payload_json="{}")
            tx.write_projection(
                event_seq=ev.seq,
                player_id="p1",
                decision=FilterDecision(include=True, payload_json="{}"),
            )

    assert len(repo.read_projection_since(player_id="p1", since_seq=0)) == over

    repo.save(GameSnapshot(genre_slug="test_genre", world_slug="test_world"))

    remaining = repo.read_projection_since(player_id="p1", since_seq=0)
    assert len(remaining) == keep, (
        f"production save must cap projection_cache to {keep} rows/player; "
        f"found {len(remaining)} (no pruning wired into the save path)"
    )
    # The newest `keep` event_seqs are the ones retained.
    assert {r.event_seq for r in remaining} == set(range(over - keep + 1, over + 1))
    prune_spans = [
        s for s in otel_capture.get_finished_spans() if s.name == SPAN_PROJECTION_CACHE_PRUNE
    ]
    assert prune_spans, "production save path must emit a projection.cache.prune span"
