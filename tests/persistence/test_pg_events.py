"""Events seq assignment + projection upsert over Postgres (ADR-115)."""

from __future__ import annotations

import uuid

import pytest

from sidequest.game import db_pool
from sidequest.game.pg import sessions
from sidequest.game.pg.events import PgEventStore
from sidequest.game.projection_filter import FilterDecision


@pytest.fixture
def store(monkeypatch, migrated_db: str):
    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    pool = db_pool.get_pool()
    slug = f"g_w_{uuid.uuid4().hex[:8]}"
    sid = sessions.ensure_session(pool, slug=slug, mode="solo", genre_slug="g", world_slug="w")
    yield PgEventStore(pool, session_id=sid)
    db_pool.close_pool()


def test_append_event_assigns_monotonic_per_session_seq(store) -> None:
    a = store.append_event(kind="NARRATION", payload_json="{}")
    b = store.append_event(kind="NARRATION", payload_json="{}")
    assert (a.seq, b.seq) == (1, 2)


def test_read_events_since(store) -> None:
    store.append_event(kind="A", payload_json="{}")
    store.append_event(kind="B", payload_json="{}")
    rows = store.read_events_since(since_seq=1)
    assert [r.kind for r in rows] == ["B"]


def test_latest_event_seq_zero_when_empty(store) -> None:
    assert store.latest_event_seq() == 0


def test_projection_upsert_then_read(store) -> None:
    ev = store.append_event(kind="NARRATION", payload_json="{}")
    store.write_projection(
        event_seq=ev.seq,
        player_id="p1",
        decision=FilterDecision(include=True, payload_json='{"x":1}'),
    )
    store.write_projection(
        event_seq=ev.seq, player_id="p1", decision=FilterDecision(include=False, payload_json="{}")
    )
    rows = store.read_projection_since(player_id="p1", since_seq=0)
    assert len(rows) == 1 and rows[0].include is False
