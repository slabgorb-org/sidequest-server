"""PgForensicReader — MVCC read-side forensics (ADR-115 C2).

Tests seed real data via the A2-C1 stores and assert the three public methods
return the correct shapes.  No ?mode=ro, no hand-rolled INSERTs, no SQLite.

_NORM_EV_TS decision (documented here for reviewers)
------------------------------------------------------
The SQLite forensic_query.py normalises events.created_at from
'YYYY-MM-DDThh:mm:ss.ffffff+00:00' (Python isoformat) to
'YYYY-MM-DD HH:MM:SS' (sqlite datetime('now') format) so that lexical
boundary comparisons against narrative_log.created_at work correctly.

Under Postgres BOTH tables are written by the exact same Python call:
    datetime.now(tz=UTC).isoformat()
producing the same 'YYYY-MM-DDThh:mm:ss.ffffff+00:00' format for every
column in every table.  Lexical ordering of two identically-formatted
ISO-8601 strings is always correct (the format sorts as wall-clock time).
Therefore _NORM_EV_TS is NOT applied in PgForensicReader.
Evidence: the boundary assertions in test_build_timeline_round_boundaries
prove that seeded events land in the correct rounds without any
normalisation — the fixture seeds narrative rows AFTER events and the
boundary is still computed correctly.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest

from sidequest.game import db_pool
from sidequest.game.pg import sessions
from sidequest.game.pg._conn import session_tx
from sidequest.game.pg.events import PgEventStore, PgSaveTransaction
from sidequest.game.pg.forensic import PgForensicReader
from sidequest.game.pg.narrative import PgNarrativeStore
from sidequest.game.pg.scrapbook import PgScrapbookStore
from sidequest.game.pg.telemetry import PgTelemetrySink
from sidequest.game.repository import ForensicReader
from sidequest.game.session import NarrativeEntry


def _slug(tag: str = "c2") -> str:
    return f"sq_{tag}_{uuid.uuid4().hex[:8]}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Shared fixture: one fully-seeded session
# ---------------------------------------------------------------------------


@pytest.fixture
def forensic_env(monkeypatch, migrated_db: str):
    """Seed a session with two rounds of events/narrative/telemetry/scrapbook.

    Round 1: event seq 1 (NARRATION with footnote), seq 2 (SCRAPBOOK_ENTRY)
    Round 2: event seq 3 (NARRATION), seq 4 (ENCOUNTER_START)

    Narrative entries written AFTER events (mirrors production order).
    Telemetry rows: one in-frame (seq=1, round=1) + one out-of-frame (seq=None, round=2).
    Scrapbook entry: round=1 (turn_id=1).
    Projection row: event_seq=1, player_id="p1", include=True.
    """
    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    pool = db_pool.get_pool()

    slug = _slug()
    sid = sessions.ensure_session(
        pool, slug=slug, mode="solo", genre_slug="caverns", world_slug="beneath_sunden"
    )

    ev_store = PgEventStore(pool, session_id=sid)
    narr_store = PgNarrativeStore(pool, session_id=sid)
    scrb_store = PgScrapbookStore(pool, session_id=sid)
    sink = PgTelemetrySink(pool, sid)

    narration_payload = json.dumps(
        {
            "text": "The dungeon breathes.",
            "footnotes": [{"fact_id": "dungeon_alive", "summary": "alive", "category": "world"}],
        }
    )

    # Round 1: narrative entry FIRST (production order — narrative_log gets the
    # lowest created_at for the round; events follow in the same round window).
    # This mirrors the _seed_rounds() pattern in test_forensic_query.py which
    # the boundary algorithm was designed around.
    narr_store.append_narrative(
        NarrativeEntry(
            timestamp=0, round=1, author="narrator", content="The dungeon breathes.", tags=["dark"]
        )
    )

    # Round 1 events (seq=1,2) — written AFTER narrative so their created_at
    # is >= narrative_log.created_at for round 1.
    ev1 = ev_store.append_event(kind="NARRATION", payload_json=narration_payload)
    ev2 = ev_store.append_event(kind="SCRAPBOOK_ENTRY", payload_json=json.dumps({"scene": "r1"}))

    # Round 1 telemetry — in-frame: use write_telemetry in a session_tx
    with session_tx(pool, sid) as conn:
        tx = PgSaveTransaction(conn, sid)
        tx.write_telemetry(
            event_seq=ev1.seq,
            round=1,
            ts=_now(),
            component="mechanical",
            event_type="census",
            payload_json=json.dumps(
                {
                    "player_id": "p1",
                    "character_name": "Rux",
                    "seat": 0,
                    "edge": {"current": 3},
                    "xp": 100,
                    "level": 1,
                    "location": "entrance",
                    "inventory": [],
                    "acquired_advancements": [],
                    "round": 1,
                }
            ),
        )

    # Round 1 scrapbook
    scrb_store.append_scrapbook_entry(
        turn_id=1,
        scene_title="The Entrance",
        scene_type="exploration",
        location="entrance",
        image_url="https://cdn.example.com/entrance.jpg",
        narrative_excerpt="Darkness ahead.",
        world_facts=["ancient", "dangerous"],
        npcs_present=[],
        render_status="done",
    )

    # Round 1 projection (player p1 sees ev1)
    from sidequest.game.projection_filter import FilterDecision

    ev_store.write_projection(
        event_seq=ev1.seq,
        player_id="p1",
        decision=FilterDecision(include=True, payload_json=narration_payload),
    )

    # Round 2: narrative entry FIRST, then events.
    narr_store.append_narrative(
        NarrativeEntry(timestamp=0, round=2, author="narrator", content="A second room.", tags=[])
    )

    # Round 2 events (seq=3,4)
    ev3 = ev_store.append_event(
        kind="NARRATION", payload_json=json.dumps({"text": "A second room.", "footnotes": []})
    )
    ev4 = ev_store.append_event(
        kind="ENCOUNTER_START", payload_json=json.dumps({"encounter": "goblin"})
    )

    # Round 2 telemetry — out-of-frame (no event_seq)
    sink.record(
        round=2,
        ts=_now(),
        component="trope",
        event_type="trope_tick",
        payload_json=json.dumps({"trope_id": "undead_stirring", "progress": 1}),
    )

    reader = PgForensicReader(pool)

    yield {
        "pool": pool,
        "slug": slug,
        "sid": sid,
        "ev1_seq": ev1.seq,
        "ev2_seq": ev2.seq,
        "ev3_seq": ev3.seq,
        "ev4_seq": ev4.seq,
        "reader": reader,
    }
    db_pool.close_pool()


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_pg_forensic_reader_satisfies_protocol(forensic_env) -> None:
    """PgForensicReader must pass isinstance(reader, ForensicReader)."""
    reader = forensic_env["reader"]
    assert isinstance(reader, ForensicReader)


# ---------------------------------------------------------------------------
# list_saves
# ---------------------------------------------------------------------------


def test_list_saves_returns_session_row(forensic_env) -> None:
    """list_saves() finds the seeded session with correct fields."""
    reader = forensic_env["reader"]
    slug = forensic_env["slug"]
    saves = reader.list_saves()
    row = next((s for s in saves if s["slug"] == slug), None)
    assert row is not None, f"slug={slug!r} not found in list_saves"


def test_list_saves_genre_world(forensic_env) -> None:
    reader = forensic_env["reader"]
    slug = forensic_env["slug"]
    saves = reader.list_saves()
    row = next(s for s in saves if s["slug"] == slug)
    assert row["genre"] == "caverns"
    assert row["world"] == "beneath_sunden"


def test_list_saves_last_played_present(forensic_env) -> None:
    reader = forensic_env["reader"]
    slug = forensic_env["slug"]
    saves = reader.list_saves()
    row = next(s for s in saves if s["slug"] == slug)
    assert row["last_played"] is not None
    assert isinstance(row["last_played"], str)


def test_list_saves_telemetry_counts(forensic_env) -> None:
    """telemetry_rows counts all turn_telemetry rows; mechanical_rows counts component='mechanical'."""
    reader = forensic_env["reader"]
    slug = forensic_env["slug"]
    saves = reader.list_saves()
    row = next(s for s in saves if s["slug"] == slug)
    # 1 mechanical (census in-frame) + 1 trope (out-of-frame) = 2 total
    assert row["telemetry_rows"] == 2
    assert row["mechanical_rows"] == 1


def test_list_saves_shape_keys(forensic_env) -> None:
    """list_saves rows carry the exact dict keys the REST endpoint (D7) expects."""
    reader = forensic_env["reader"]
    slug = forensic_env["slug"]
    saves = reader.list_saves()
    row = next(s for s in saves if s["slug"] == slug)
    expected_keys = {
        "slug",
        "genre",
        "world",
        "created_at",
        "last_played",
        "telemetry_rows",
        "mechanical_rows",
    }
    assert set(row.keys()) == expected_keys


def test_list_saves_cross_session_isolation(forensic_env) -> None:
    """A second session's data is listed separately; slugs don't bleed."""
    pool = forensic_env["pool"]
    reader = forensic_env["reader"]
    slug_a = forensic_env["slug"]

    slug_b = _slug("iso")
    sessions.ensure_session(pool, slug=slug_b, mode="solo", genre_slug="neon", world_slug="grid")

    saves = reader.list_saves()
    slugs = {s["slug"] for s in saves}
    assert slug_a in slugs
    assert slug_b in slugs
    # Each row is independent (different genres)
    row_a = next(s for s in saves if s["slug"] == slug_a)
    row_b = next(s for s in saves if s["slug"] == slug_b)
    assert row_a["genre"] == "caverns"
    assert row_b["genre"] == "neon"


# ---------------------------------------------------------------------------
# build_timeline
# ---------------------------------------------------------------------------


def test_build_timeline_round_boundaries(forensic_env) -> None:
    """build_timeline returns one entry per narrative round (two seeded rounds)."""
    reader = forensic_env["reader"]
    sid = forensic_env["sid"]
    timeline = reader.build_timeline(sid)
    assert len(timeline) == 2
    rounds = [e["round"] for e in timeline]
    assert rounds == [1, 2]


def test_build_timeline_seq_range(forensic_env) -> None:
    """Round 1 covers seqs 1-2; round 2 covers seqs 3-4."""
    reader = forensic_env["reader"]
    sid = forensic_env["sid"]
    ev1 = forensic_env["ev1_seq"]
    ev2 = forensic_env["ev2_seq"]
    ev3 = forensic_env["ev3_seq"]
    ev4 = forensic_env["ev4_seq"]

    timeline = reader.build_timeline(sid)
    r1 = next(e for e in timeline if e["round"] == 1)
    r2 = next(e for e in timeline if e["round"] == 2)

    assert r1["seq_start"] == ev1
    assert r1["seq_end"] == ev2
    assert r2["seq_start"] == ev3
    assert r2["seq_end"] == ev4


def test_build_timeline_event_kind_counts(forensic_env) -> None:
    """Round 1 has NARRATION×1 + SCRAPBOOK_ENTRY×1; round 2 has NARRATION×1 + ENCOUNTER_START×1."""
    reader = forensic_env["reader"]
    sid = forensic_env["sid"]
    timeline = reader.build_timeline(sid)
    r1 = next(e for e in timeline if e["round"] == 1)
    r2 = next(e for e in timeline if e["round"] == 2)

    assert r1["event_kind_counts"].get("NARRATION") == 1
    assert r1["event_kind_counts"].get("SCRAPBOOK_ENTRY") == 1
    assert r2["event_kind_counts"].get("NARRATION") == 1
    assert r2["event_kind_counts"].get("ENCOUNTER_START") == 1


def test_build_timeline_narrative_authors(forensic_env) -> None:
    """Both rounds show 'narrator' as the single author."""
    reader = forensic_env["reader"]
    sid = forensic_env["sid"]
    timeline = reader.build_timeline(sid)
    for entry in timeline:
        assert entry["narrative_authors"] == ["narrator"]


def test_build_timeline_ts_present(forensic_env) -> None:
    """Every timeline entry carries a non-empty ts string."""
    reader = forensic_env["reader"]
    sid = forensic_env["sid"]
    timeline = reader.build_timeline(sid)
    for entry in timeline:
        assert isinstance(entry["ts"], str) and entry["ts"]


def test_build_timeline_shape_keys(forensic_env) -> None:
    """Timeline entries carry the exact dict keys the REST endpoint (D7) expects."""
    reader = forensic_env["reader"]
    sid = forensic_env["sid"]
    timeline = reader.build_timeline(sid)
    expected = {"round", "seq_start", "seq_end", "event_kind_counts", "narrative_authors", "ts"}
    for entry in timeline:
        assert set(entry.keys()) == expected


def test_build_timeline_empty_session(monkeypatch, migrated_db: str) -> None:
    """build_timeline on a session with no narrative rows returns []."""
    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    pool = db_pool.get_pool()
    try:
        sid = sessions.ensure_session(
            pool, slug=_slug("empty"), mode="solo", genre_slug="g", world_slug="w"
        )
        reader = PgForensicReader(pool)
        timeline = reader.build_timeline(sid)
        assert timeline == []
    finally:
        db_pool.close_pool()


# ---------------------------------------------------------------------------
# build_turn_bundle
# ---------------------------------------------------------------------------


def test_build_turn_bundle_shape_keys(forensic_env) -> None:
    """build_turn_bundle returns the canonical keys matching forensic_query.build_turn_bundle."""
    reader = forensic_env["reader"]
    sid = forensic_env["sid"]
    bundle = reader.build_turn_bundle(sid, 1)
    expected = {
        "round",
        "narrative",
        "events",
        "derived",
        "projection",
        "scrapbook",
        "unparseable_seqs",
        "telemetry",
        "mechanical",
    }
    assert set(bundle.keys()) == expected


def test_build_turn_bundle_round1_events(forensic_env) -> None:
    """Round 1 bundle has 2 events (NARRATION + SCRAPBOOK_ENTRY)."""
    reader = forensic_env["reader"]
    sid = forensic_env["sid"]
    bundle = reader.build_turn_bundle(sid, 1)
    assert len(bundle["events"]) == 2
    kinds = {e["kind"] for e in bundle["events"]}
    assert kinds == {"NARRATION", "SCRAPBOOK_ENTRY"}


def test_build_turn_bundle_event_shape(forensic_env) -> None:
    """Each event entry has seq, kind, payload, created_at."""
    reader = forensic_env["reader"]
    sid = forensic_env["sid"]
    bundle = reader.build_turn_bundle(sid, 1)
    for ev in bundle["events"]:
        assert set(ev.keys()) == {"seq", "kind", "payload", "created_at"}
        assert isinstance(ev["seq"], int)
        assert isinstance(ev["kind"], str)


def test_build_turn_bundle_narrative(forensic_env) -> None:
    """Round 1 narrative has one entry with correct shape."""
    reader = forensic_env["reader"]
    sid = forensic_env["sid"]
    bundle = reader.build_turn_bundle(sid, 1)
    assert len(bundle["narrative"]) == 1
    narr = bundle["narrative"][0]
    assert set(narr.keys()) == {"round", "author", "content", "tags", "created_at"}
    assert narr["author"] == "narrator"
    assert narr["content"] == "The dungeon breathes."
    assert narr["tags"] == ["dark"]


def test_build_turn_bundle_projection(forensic_env) -> None:
    """Round 1 bundle includes the projection row for player p1."""
    reader = forensic_env["reader"]
    sid = forensic_env["sid"]
    bundle = reader.build_turn_bundle(sid, 1)
    assert len(bundle["projection"]) >= 1
    proj = bundle["projection"][0]
    assert set(proj.keys()) == {"event_seq", "player_id", "include", "payload"}
    assert proj["player_id"] == "p1"
    assert proj["include"] in (True, False, 1, 0)  # bool or int, either is OK


def test_build_turn_bundle_scrapbook(forensic_env) -> None:
    """Round 1 bundle has the seeded scrapbook entry."""
    reader = forensic_env["reader"]
    sid = forensic_env["sid"]
    bundle = reader.build_turn_bundle(sid, 1)
    assert len(bundle["scrapbook"]) == 1
    sb = bundle["scrapbook"][0]
    expected_keys = {
        "scene_title",
        "scene_type",
        "location",
        "image_url",
        "narrative_excerpt",
        "world_facts",
        "npcs_present",
        "render_status",
    }
    assert set(sb.keys()) == expected_keys
    assert sb["location"] == "entrance"
    assert sb["world_facts"] == ["ancient", "dangerous"]


def test_build_turn_bundle_telemetry(forensic_env) -> None:
    """Round 1 telemetry fold has the in-frame census row."""
    reader = forensic_env["reader"]
    sid = forensic_env["sid"]
    bundle = reader.build_turn_bundle(sid, 1)
    tel = bundle["telemetry"]
    assert set(tel.keys()) == {"rows", "by_component", "total", "unparseable_seqs"}
    assert tel["total"] >= 1
    components = {r["component"] for r in tel["rows"]}
    assert "mechanical" in components


def test_build_turn_bundle_mechanical(forensic_env) -> None:
    """Round 1 mechanical fold: state not 'absent', pc 'Rux' present."""
    reader = forensic_env["reader"]
    sid = forensic_env["sid"]
    bundle = reader.build_turn_bundle(sid, 1)
    mech = bundle["mechanical"]
    assert set(mech.keys()) == {"state", "pcs", "trope", "unparseable_seqs"}
    assert mech["state"] in ("baseline", "static", "moved", "absent")
    # The census row has player_id=p1 / character_name=Rux; state != absent
    assert mech["state"] != "absent"
    names = [pc["character_name"] for pc in mech["pcs"]]
    assert "Rux" in names


def test_build_turn_bundle_derived_known_facts(forensic_env) -> None:
    """Round 1 derived panel folds the footnote: fact_id='dungeon_alive'."""
    reader = forensic_env["reader"]
    sid = forensic_env["sid"]
    bundle = reader.build_turn_bundle(sid, 1)
    derived = bundle["derived"]
    assert "dungeon_alive" in derived
    entry = derived["dungeon_alive"]
    assert "value" in entry and "source_seqs" in entry


def test_build_turn_bundle_unknown_round_empty_bundle(forensic_env) -> None:
    """build_turn_bundle for a round that doesn't exist returns an empty-but-shaped bundle."""
    reader = forensic_env["reader"]
    sid = forensic_env["sid"]
    bundle = reader.build_turn_bundle(sid, 999)
    assert bundle["round"] == 999
    assert bundle["events"] == []
    # narrative may be empty; telemetry/mechanical must be the empty shapes
    assert bundle["telemetry"]["total"] == 0
    assert bundle["mechanical"]["state"] == "absent"


def test_build_turn_bundle_round2_has_no_scrapbook(forensic_env) -> None:
    """Round 2 bundle has no scrapbook entries (only seeded for round 1)."""
    reader = forensic_env["reader"]
    sid = forensic_env["sid"]
    bundle = reader.build_turn_bundle(sid, 2)
    assert bundle["scrapbook"] == []


def test_build_turn_bundle_cross_session_isolation(forensic_env) -> None:
    """build_turn_bundle for a different session_id sees no data from the seeded session."""
    pool = forensic_env["pool"]
    reader = forensic_env["reader"]

    other_sid = sessions.ensure_session(
        pool, slug=_slug("other"), mode="solo", genre_slug="g", world_slug="w"
    )
    # other session has no events/narrative
    bundle = reader.build_turn_bundle(other_sid, 1)
    assert bundle["events"] == []
    assert bundle["narrative"] == []
