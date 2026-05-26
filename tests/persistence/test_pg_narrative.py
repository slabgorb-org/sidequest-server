"""Postgres narrative_log adapter (ADR-115 A5)."""

from __future__ import annotations

import uuid

import pytest

from sidequest.game import db_pool
from sidequest.game.pg import sessions
from sidequest.game.pg.events import PgEventStore
from sidequest.game.pg.narrative import PgNarrativeStore
from sidequest.game.projection_filter import FilterDecision
from sidequest.game.session import NarrativeEntry


@pytest.fixture
def store(monkeypatch, migrated_db: str):
    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    pool = db_pool.get_pool()
    slug = f"narr_{uuid.uuid4().hex[:8]}"
    sid = sessions.ensure_session(pool, slug=slug, mode="solo", genre_slug="g", world_slug="w")
    yield PgNarrativeStore(pool, session_id=sid)
    db_pool.close_pool()


@pytest.fixture
def store_with_events(monkeypatch, migrated_db: str):
    """Paired narrative store + event store sharing the same session."""
    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    pool = db_pool.get_pool()
    slug = f"narr_ev_{uuid.uuid4().hex[:8]}"
    sid = sessions.ensure_session(pool, slug=slug, mode="solo", genre_slug="g", world_slug="w")
    narr = PgNarrativeStore(pool, session_id=sid)
    evts = PgEventStore(pool, session_id=sid)
    yield narr, evts, sid, pool
    db_pool.close_pool()


# ---------------------------------------------------------------------------
# append_narrative + max_narrative_round + recent_narrative
# ---------------------------------------------------------------------------


def _entry(round: int, author: str = "narrator", content: str = "text") -> NarrativeEntry:
    return NarrativeEntry(round=round, author=author, content=content, tags=["a", "b"])


def test_max_narrative_round_zero_when_empty(store) -> None:
    assert store.max_narrative_round() == 0


def test_append_then_max_round(store) -> None:
    store.append_narrative(_entry(round=3))
    store.append_narrative(_entry(round=5))
    assert store.max_narrative_round() == 5


def test_recent_narrative_empty(store) -> None:
    assert store.recent_narrative(10) == []


def test_recent_narrative_order_and_limit(store) -> None:
    """recent_narrative returns newest-N in ASCENDING (oldest-first) order."""
    for i in range(5):
        store.append_narrative(_entry(round=i, content=f"entry {i}"))
    entries = store.recent_narrative(3)
    assert len(entries) == 3
    # oldest-first — last 3 appended are rounds 2,3,4 in order
    assert [e.round for e in entries] == [2, 3, 4]


def test_recent_narrative_reconstructs_entry_fields(store) -> None:
    e = NarrativeEntry(round=7, author="player", content="hello", tags=["x"])
    store.append_narrative(e)
    entries = store.recent_narrative(1)
    assert len(entries) == 1
    got = entries[0]
    assert got.round == 7
    assert got.author == "player"
    assert got.content == "hello"
    assert got.tags == ["x"]


def test_recent_narrative_tags_roundtrip_list(store) -> None:
    """tags list must survive the json.dumps / json.loads round-trip."""
    e = NarrativeEntry(round=1, author="narrator", content="c", tags=["foo", "bar", "baz"])
    store.append_narrative(e)
    entries = store.recent_narrative(1)
    assert entries[0].tags == ["foo", "bar", "baz"]


def test_recent_narrative_tags_empty_list(store) -> None:
    e = NarrativeEntry(round=1, author="narrator", content="c", tags=[])
    store.append_narrative(e)
    entries = store.recent_narrative(1)
    assert entries[0].tags == []


# ---------------------------------------------------------------------------
# generate_recap
# ---------------------------------------------------------------------------


def test_generate_recap_none_when_empty(store) -> None:
    assert store.generate_recap() is None


def test_generate_recap_returns_string_when_entries_exist(store) -> None:
    store.append_narrative(_entry(round=1, content="Something happened"))
    recap = store.generate_recap()
    assert recap is not None
    assert isinstance(recap, str)
    assert len(recap) > 0


# ---------------------------------------------------------------------------
# read_narration_backfill
# ---------------------------------------------------------------------------


def test_read_narration_backfill_empty_when_no_narrations(store_with_events) -> None:
    narr, evts, sid, pool = store_with_events
    rows = narr.read_narration_backfill(player_id="p1", limit=5)
    assert rows == []


def test_read_narration_backfill_returns_rows_in_seq_order(store_with_events) -> None:
    narr, evts, sid, pool = store_with_events
    # Append two NARRATION events with projection cache entries (include=True)
    ev1 = evts.append_event(kind="NARRATION", payload_json='{"text":"first"}')
    ev2 = evts.append_event(kind="NARRATION", payload_json='{"text":"second"}')
    evts.write_projection(
        event_seq=ev1.seq,
        player_id="p1",
        decision=FilterDecision(include=True, payload_json='{"text":"first"}'),
    )
    evts.write_projection(
        event_seq=ev2.seq,
        player_id="p1",
        decision=FilterDecision(include=True, payload_json='{"text":"second"}'),
    )
    rows = narr.read_narration_backfill(player_id="p1", limit=5)
    assert len(rows) == 2
    # seq-ascending order
    assert rows[0].seq < rows[1].seq
    assert rows[0].kind == "NARRATION"
    assert rows[1].kind == "NARRATION"
    assert rows[0].payload_json == '{"text":"first"}'
    assert rows[1].payload_json == '{"text":"second"}'


def test_read_narration_backfill_skips_excluded_projection(store_with_events) -> None:
    narr, evts, sid, pool = store_with_events
    ev1 = evts.append_event(kind="NARRATION", payload_json='{"text":"visible"}')
    ev2 = evts.append_event(kind="NARRATION", payload_json='{"text":"hidden"}')
    evts.write_projection(
        event_seq=ev1.seq,
        player_id="p1",
        decision=FilterDecision(include=True, payload_json='{"text":"visible"}'),
    )
    evts.write_projection(
        event_seq=ev2.seq,
        player_id="p1",
        decision=FilterDecision(include=False, payload_json=None),
    )
    rows = narr.read_narration_backfill(player_id="p1", limit=5)
    assert len(rows) == 1
    assert rows[0].payload_json == '{"text":"visible"}'


def test_read_narration_backfill_skips_missing_cache_row(store_with_events) -> None:
    narr, evts, sid, pool = store_with_events
    # Event exists but no projection_cache row for this player
    _ev = evts.append_event(kind="NARRATION", payload_json='{"text":"x"}')
    rows = narr.read_narration_backfill(player_id="p1", limit=5)
    assert rows == []


def test_read_narration_backfill_includes_chapter_marker(store_with_events) -> None:
    narr, evts, sid, pool = store_with_events
    chapter = evts.append_event(kind="CHAPTER_MARKER", payload_json='{"chapter":1}')
    narr_ev = evts.append_event(kind="NARRATION", payload_json='{"text":"narr"}')
    evts.write_projection(
        event_seq=chapter.seq,
        player_id="p1",
        decision=FilterDecision(include=True, payload_json='{"chapter":1}'),
    )
    evts.write_projection(
        event_seq=narr_ev.seq,
        player_id="p1",
        decision=FilterDecision(include=True, payload_json='{"text":"narr"}'),
    )
    rows = narr.read_narration_backfill(player_id="p1", limit=5)
    kinds = [r.kind for r in rows]
    # Chapter marker precedes the narration
    assert "CHAPTER_MARKER" in kinds
    assert "NARRATION" in kinds
    assert kinds.index("CHAPTER_MARKER") < kinds.index("NARRATION")


def test_read_narration_backfill_limit_caps_narration_count(store_with_events) -> None:
    narr, evts, sid, pool = store_with_events
    # Write 5 narration events, all with projection cache
    evs = []
    for i in range(5):
        ev = evts.append_event(kind="NARRATION", payload_json=f'{{"i":{i}}}')
        evts.write_projection(
            event_seq=ev.seq,
            player_id="p1",
            decision=FilterDecision(include=True, payload_json=f'{{"i":{i}}}'),
        )
        evs.append(ev)
    rows = narr.read_narration_backfill(player_id="p1", limit=2)
    narr_rows = [r for r in rows if r.kind == "NARRATION"]
    assert len(narr_rows) == 2
    # Must be the NEWEST 2 (highest seq)
    returned_seqs = {r.seq for r in narr_rows}
    expected_seqs = {evs[-1].seq, evs[-2].seq}
    assert returned_seqs == expected_seqs


def test_read_narration_backfill_zero_limit_returns_empty(store_with_events) -> None:
    narr, evts, sid, pool = store_with_events
    ev = evts.append_event(kind="NARRATION", payload_json="{}")
    evts.write_projection(
        event_seq=ev.seq,
        player_id="p1",
        decision=FilterDecision(include=True, payload_json="{}"),
    )
    rows = narr.read_narration_backfill(player_id="p1", limit=0)
    assert rows == []
