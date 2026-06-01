"""End-to-end ProjectionFilter wiring test.

Asserts the single-truth invariant in executable form:
    - 3 players (no GM seat — the narrator is the GM) receive projections
      consistent with the rules
    - projection_cache has exactly N rows per event
    - projection.filter.decide span count equals the projection count
    - Reconnecting a player receives byte-identical frames to the live
      session (via cache read, no re-filter)
    - Canonical truth lives in the events table (read server-side by the
      narrator), untouched by any projection rule
"""

from __future__ import annotations

import json

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.event_log import EventLog
from sidequest.game.projection.cache import ProjectionCache
from sidequest.game.projection.cache_fill import lazy_fill
from sidequest.game.projection.composed import ComposedFilter
from sidequest.game.projection.envelope import MessageEnvelope
from sidequest.game.projection.rules import load_rules_from_yaml_str
from sidequest.game.projection.view import SessionGameStateView


@pytest.fixture
def pg_repo(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """A real PgSaveRepository on a per-worker throwaway PG db (ADR-115 F1)."""
    import psycopg

    from sidequest.game import db_pool
    from sidequest.server.session_state import _build_pg_repos_for_slug

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
    repo, _dungeon, _sink = _build_pg_repos_for_slug(
        db_pool.get_pool(),
        slug="projection-e2e",
        mode="solo",
        genre_slug="test_genre",
        world_slug="test_world",
    )
    try:
        yield repo
    finally:
        db_pool.close_pool()


def _setup_tracing() -> InMemorySpanExporter:
    """Attach an in-memory exporter to whichever TracerProvider is active.

    OTEL forbids replacing a TracerProvider once set, so if an earlier test
    (or the app's own telemetry setup) has already installed one, we attach
    our SpanProcessor to it rather than overriding. Falls back to installing
    a fresh provider only when none is set.
    """
    exporter = InMemorySpanExporter()
    current = trace.get_tracer_provider()
    if hasattr(current, "add_span_processor"):
        current.add_span_processor(SimpleSpanProcessor(exporter))
    else:
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
    return exporter


def test_end_to_end_single_truth_invariant(pg_repo) -> None:
    exporter = _setup_tracing()
    repo = pg_repo
    log = EventLog(repo)
    cache = ProjectionCache(repo)

    rules = load_rules_from_yaml_str(
        """
rules:
  - kind: NARRATION
    redact_fields:
      - field: text
        unless: is_self(text)
        mask: "**"
        """
    )
    filt = ComposedFilter(rules=rules)
    view = SessionGameStateView(
        player_id_to_character={
            "alice": "alice_char",
            "bob": "bob_char",
            "gm": "gm_char",
        },
    )
    players = ["alice", "bob", "gm"]

    # Fan out 3 events.
    for text in ["one", "two", "three"]:
        row = log.append(kind="NARRATION", payload_json=f'{{"text":"{text}"}}')
        env = MessageEnvelope(kind=row.kind, payload_json=row.payload_json, origin_seq=row.seq)
        for pid in players:
            decision = filt.project(envelope=env, view=view, player_id=pid)
            cache.write(event_seq=row.seq, player_id=pid, decision=decision)

    # 1. projection_cache has N_players × N_events rows (read back per player).
    cache_rows = sum(len(cache.read_since(player_id=pid, since_seq=0)) for pid in players)
    assert cache_rows == 3 * 3

    # 2. projection.filter.decide span count equals cache-row count.
    decide_spans = [
        s for s in exporter.get_finished_spans() if s.name == "projection.filter.decide"
    ]
    assert len(decide_spans) == 9

    # 3. No GM seat exists — every player (including the one historically
    #    named "gm") sees the masked "**". The canonical text lives only in
    #    the events table (assertion 5), read server-side by the narrator,
    #    never in a per-player projection.
    alice_rows = cache.read_since(player_id="alice", since_seq=0)
    for pid in players:
        for r in cache.read_since(player_id=pid, since_seq=0):
            assert json.loads(r.payload_json)["text"] == "**"

    # 4. Reconnecting Alice replays byte-identical frames (cache-only path).
    replay = cache.read_since(player_id="alice", since_seq=0)
    assert [r.payload_json for r in replay] == [r.payload_json for r in alice_rows]

    # 5. GM canonical: events table has true text, unaffected by any rule.
    canonical_rows = repo.read_events_since(since_seq=0)
    assert [json.loads(r.payload_json)["text"] for r in canonical_rows] == ["one", "two", "three"]


def test_emitter_reconnect_relies_on_lazy_fill(pg_repo) -> None:
    """Emitters skip their own fan-out — reconnect must lazy-fill their gap.

    Live fan-out in ``_emit_event`` skips the emitter (they see the raw
    canonical payload directly), so the emitter has no projection_cache
    rows for events they authored. On reconnect, ``lazy_fill`` must fill
    those gaps so the emitter sees byte-identical projections to what
    any other player would have seen.

    This test exists specifically to catch a regression where live fan-out
    starts writing cache rows for the emitter (breaking the "emitter sees
    canonical" invariant) or where lazy_fill on reconnect stops covering
    the emitter-skipped events (breaking byte-identical replay after
    reconnect). The original E2E test writes cache rows for all players
    at live-fan-out time, so neither failure mode would show up there.
    """
    _setup_tracing()
    repo = pg_repo
    log = EventLog(repo)
    cache = ProjectionCache(repo)

    rules = load_rules_from_yaml_str(
        """
rules:
  - kind: NARRATION
    redact_fields:
      - field: text
        unless: is_self(text)
        mask: "**"
        """
    )
    filt = ComposedFilter(rules=rules, pack_slug="test_pack")
    view = SessionGameStateView(
        player_id_to_character={
            "alice": "alice_char",
            "bob": "bob_char",
            "gm": "gm_char",
        },
    )

    # Simulate live fan-out that skips the emitter ("alice").
    emitter_id = "alice"
    peers = ["bob", "gm"]
    for text in ["one", "two"]:
        row = log.append(kind="NARRATION", payload_json=f'{{"text":"{text}"}}')
        env = MessageEnvelope(kind=row.kind, payload_json=row.payload_json, origin_seq=row.seq)
        for pid in peers:
            decision = filt.project(envelope=env, view=view, player_id=pid)
            cache.write(event_seq=row.seq, player_id=pid, decision=decision)

    # Alice has no cache rows — she was the emitter.
    assert cache.read_since(player_id=emitter_id, since_seq=0) == []

    # On reconnect, lazy_fill covers the gap.
    filled = lazy_fill(event_log=log, cache=cache, filter_=filt, view=view, player_id=emitter_id)
    assert filled == 2

    # Alice's now-cached projections match what bob (another non-GM) saw —
    # byte-identical byte-for-byte. This is the single-truth invariant
    # applied to a just-reconnected emitter.
    alice_rows = cache.read_since(player_id=emitter_id, since_seq=0)
    bob_rows = cache.read_since(player_id="bob", since_seq=0)
    assert [r.payload_json for r in alice_rows] == [r.payload_json for r in bob_rows]
    # And both non-GM views show the mask ("**"), never canonical text.
    for r in alice_rows:
        assert json.loads(r.payload_json)["text"] == "**"
