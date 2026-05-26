"""D7 — REST forensic/games endpoints read from Postgres (PgForensicReader).

ADR-115 Task D7: the debug/forensic/games REST endpoints were lifted off
``SqliteStore(db)`` + ``open_save_readonly`` and now read from the
``PgForensicReader`` / pg ``sessions`` repository against the process-global
pool.

These are wiring + behavior tests: a real session is seeded via the Pg* repos
into a ``migrated_db`` pool, then the endpoints are hit through the real
FastAPI app (TestClient). Assertions prove the response comes from the Pg read
path (correct timeline boundaries / turn bundle / saves list / snapshot /
encounter events) and that an unknown slug yields lossy-empty (never 500).

No source-text assertions (CLAUDE.md "No Source-Text Wiring Tests") — the
wiring is proven by fixture-seeded behavior flowing through the real route.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from sidequest.game import db_pool
from sidequest.game.pg import sessions
from sidequest.game.pg._conn import session_tx
from sidequest.game.pg.events import PgEventStore, PgSaveTransaction
from sidequest.game.pg.narrative import PgNarrativeStore
from sidequest.game.pg.scrapbook import PgScrapbookStore
from sidequest.game.pg.telemetry import PgTelemetrySink
from sidequest.game.session import NarrativeEntry
from sidequest.server.app import create_app


def _slug(tag: str = "d7") -> str:
    return f"sq_{tag}_{uuid.uuid4().hex[:8]}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


@pytest.fixture
def pg_rest_env(monkeypatch, migrated_db: str, tmp_path) -> Iterator[dict]:
    """Seed a two-round session into a migrated PG pool and yield a TestClient.

    Mirrors tests/persistence/test_pg_forensic.py::forensic_env seeding so the
    endpoint reads exercise the same fixtured data through the HTTP route.

    Round 1: NARRATION (seq1, with footnote) + ENCOUNTER_STARTED (seq2)
    Round 2: NARRATION (seq3) + ENCOUNTER_RESOLVED (seq4)
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

    # Round 1 — narrative first (production order), then events.
    narr_store.append_narrative(
        NarrativeEntry(
            timestamp=0, round=1, author="narrator", content="The dungeon breathes.", tags=["dark"]
        )
    )
    ev1 = ev_store.append_event(kind="NARRATION", payload_json=narration_payload)
    ev2 = ev_store.append_event(
        kind="ENCOUNTER_STARTED",
        payload_json=json.dumps({"encounter_type": "combat", "turn": 1}),
    )

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

    from sidequest.game.projection_filter import FilterDecision

    ev_store.write_projection(
        event_seq=ev1.seq,
        player_id="p1",
        decision=FilterDecision(include=True, payload_json=narration_payload),
    )

    # Round 2
    narr_store.append_narrative(
        NarrativeEntry(timestamp=0, round=2, author="narrator", content="A second room.", tags=[])
    )
    ev3 = ev_store.append_event(
        kind="NARRATION", payload_json=json.dumps({"text": "A second room.", "footnotes": []})
    )
    ev4 = ev_store.append_event(
        kind="ENCOUNTER_RESOLVED",
        payload_json=json.dumps({"outcome": "player_victory", "turn": 3}),
    )

    sink.record(
        round=2,
        ts=_now(),
        component="trope",
        event_type="trope_tick",
        payload_json=json.dumps({"trope_id": "undead_stirring", "progress": 1}),
    )

    # Persist a final snapshot so the /snapshot endpoint has something to read.
    from sidequest.game.persistence import GameMode
    from sidequest.game.pg.save_repository import PgSaveRepository
    from sidequest.game.session import GameSnapshot, TurnManager

    repo = PgSaveRepository.for_slug(
        pool,
        slug=slug,
        mode=GameMode.SOLO,
        genre_slug="caverns",
        world_slug="beneath_sunden",
    )
    repo.save(
        GameSnapshot(
            genre_slug="caverns",
            world_slug="beneath_sunden",
            turn_manager=TurnManager(interaction=2),
        )
    )

    packs_dir = tmp_path / "genre_packs"
    packs_dir.mkdir()
    saves_dir = tmp_path / "saves"
    saves_dir.mkdir()
    app = create_app(genre_pack_search_paths=[packs_dir], save_dir=saves_dir)
    client = TestClient(app)

    yield {
        "client": client,
        "slug": slug,
        "sid": sid,
        "ev1": ev1.seq,
        "ev2": ev2.seq,
        "ev3": ev3.seq,
        "ev4": ev4.seq,
    }
    db_pool.close_pool()


# ---------------------------------------------------------------------------
# /api/debug/saves
# ---------------------------------------------------------------------------


def test_debug_saves_lists_pg_session(pg_rest_env) -> None:
    """The seeded PG session appears in /api/debug/saves with the Pg shape."""
    client = pg_rest_env["client"]
    slug = pg_rest_env["slug"]
    resp = client.get("/api/debug/saves")
    assert resp.status_code == 200
    rows = resp.json()
    row = next((r for r in rows if r["slug"] == slug), None)
    assert row is not None, f"{slug} not found in {rows!r}"
    # PgForensicReader-specific fields prove the read came from Postgres, not
    # the file-mtime SQLite walk (which has no telemetry counts).
    assert row["genre"] == "caverns"
    assert row["world"] == "beneath_sunden"
    assert row["telemetry_rows"] == 2
    assert row["mechanical_rows"] == 1


# ---------------------------------------------------------------------------
# /api/debug/save/{slug}/timeline
# ---------------------------------------------------------------------------


def test_debug_timeline_round_boundaries(pg_rest_env) -> None:
    """Timeline boundaries match the PG-seeded rounds (proves PgForensicReader)."""
    client = pg_rest_env["client"]
    slug = pg_rest_env["slug"]
    resp = client.get(f"/api/debug/save/{slug}/timeline")
    assert resp.status_code == 200
    timeline = resp.json()
    assert [e["round"] for e in timeline] == [1, 2]
    r1 = next(e for e in timeline if e["round"] == 1)
    r2 = next(e for e in timeline if e["round"] == 2)
    assert r1["seq_start"] == pg_rest_env["ev1"]
    assert r1["seq_end"] == pg_rest_env["ev2"]
    assert r2["seq_start"] == pg_rest_env["ev3"]
    assert r2["seq_end"] == pg_rest_env["ev4"]


def test_debug_timeline_unknown_slug_empty(pg_rest_env) -> None:
    """Unknown slug → [] (lossy-empty), never 500."""
    client = pg_rest_env["client"]
    resp = client.get("/api/debug/save/does-not-exist/timeline")
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# /api/debug/save/{slug}/turn/{round}
# ---------------------------------------------------------------------------


def test_debug_turn_bundle_round1(pg_rest_env) -> None:
    """Round-1 bundle comes from PgForensicReader with correct events/derived."""
    client = pg_rest_env["client"]
    slug = pg_rest_env["slug"]
    resp = client.get(f"/api/debug/save/{slug}/turn/1")
    assert resp.status_code == 200
    bundle = resp.json()
    assert bundle["round"] == 1
    kinds = {e["kind"] for e in bundle["events"]}
    assert kinds == {"NARRATION", "ENCOUNTER_STARTED"}
    # KnownFacts fold from the footnote proves the PG event-fold path ran.
    assert "dungeon_alive" in bundle["derived"]
    # mechanical census proves telemetry fold path ran.
    assert bundle["mechanical"]["state"] != "absent"
    names = [pc["character_name"] for pc in bundle["mechanical"]["pcs"]]
    assert "Rux" in names


def test_debug_turn_bundle_unknown_round_empty(pg_rest_env) -> None:
    """Unknown round → empty-but-shaped bundle, never 500."""
    client = pg_rest_env["client"]
    slug = pg_rest_env["slug"]
    resp = client.get(f"/api/debug/save/{slug}/turn/999")
    assert resp.status_code == 200
    bundle = resp.json()
    assert bundle["round"] == 999
    assert bundle["events"] == []
    assert bundle["telemetry"]["total"] == 0
    assert bundle["mechanical"]["state"] == "absent"


def test_debug_turn_bundle_unknown_slug_empty(pg_rest_env) -> None:
    """Unknown slug → empty-but-shaped bundle, never 500."""
    client = pg_rest_env["client"]
    resp = client.get("/api/debug/save/does-not-exist/turn/1")
    assert resp.status_code == 200
    bundle = resp.json()
    assert bundle["round"] == 1
    assert bundle["events"] == []
    assert bundle["mechanical"]["state"] == "absent"


# ---------------------------------------------------------------------------
# /api/debug/save/{slug}/snapshot
# ---------------------------------------------------------------------------


def test_debug_snapshot_returns_persisted_snapshot(pg_rest_env) -> None:
    """The persisted snapshot is read from PG game_state (not SQLite)."""
    client = pg_rest_env["client"]
    slug = pg_rest_env["slug"]
    resp = client.get(f"/api/debug/save/{slug}/snapshot")
    assert resp.status_code == 200
    snap = resp.json()
    assert isinstance(snap, dict)
    assert snap.get("genre_slug") == "caverns"
    assert snap.get("world_slug") == "beneath_sunden"


def test_debug_snapshot_unknown_slug_empty(pg_rest_env) -> None:
    """Unknown slug → {} (lossy-empty), never 500."""
    client = pg_rest_env["client"]
    resp = client.get("/api/debug/save/does-not-exist/snapshot")
    assert resp.status_code == 200
    assert resp.json() == {}


# ---------------------------------------------------------------------------
# /api/sessions/{slug}/encounter_events
# ---------------------------------------------------------------------------


def test_encounter_events_from_pg(pg_rest_env) -> None:
    """ENCOUNTER_* rows are read from the PG events table in seq order."""
    client = pg_rest_env["client"]
    slug = pg_rest_env["slug"]
    resp = client.get(f"/api/sessions/{slug}/encounter_events")
    assert resp.status_code == 200
    rows = resp.json()
    kinds = [r["kind"] for r in rows]
    assert kinds == ["ENCOUNTER_STARTED", "ENCOUNTER_RESOLVED"]
    for r in rows:
        assert set(r.keys()) == {"seq", "kind", "payload", "created_at"}
        assert isinstance(r["payload"], dict)


def test_encounter_events_unknown_slug_404(pg_rest_env) -> None:
    """Unknown slug → 404 (this endpoint's documented missing-session contract)."""
    client = pg_rest_env["client"]
    resp = client.get("/api/sessions/does-not-exist/encounter_events")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /api/debug/state
# ---------------------------------------------------------------------------


def test_debug_state_projects_pg_session(pg_rest_env) -> None:
    """/api/debug/state projects the persisted PG snapshot for the seeded slug."""
    client = pg_rest_env["client"]
    slug = pg_rest_env["slug"]
    resp = client.get(f"/api/debug/state?session_key={slug}")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    view = body[0]
    assert view["session_key"] == slug
    assert view["genre_slug"] == "caverns"
    assert view["world_slug"] == "beneath_sunden"


def test_debug_state_unknown_session_key_empty(pg_rest_env) -> None:
    """Unknown session_key → [] (lossy-empty), never 404/500."""
    client = pg_rest_env["client"]
    resp = client.get("/api/debug/state?session_key=does-not-exist")
    assert resp.status_code == 200
    assert resp.json() == []


def test_debug_state_survives_one_bad_save(pg_rest_env) -> None:
    """One unloadable session must not 500 the whole State tab.

    Wiring test for the "never-500 forensics" invariant (D7 review): the
    pg_rest_env fixture already seeded one good session; here we add a second
    session whose persisted ``game_state.snapshot_json`` is corrupt (non-JSON
    text), so ``PgSaveRepository.load()`` raises ``SaveSchemaIncompatibleError``
    inside the debug_state per-slug body. The endpoint must log+skip that slug
    and still return the GOOD session's view — not propagate the exception as a
    500 that blanks the entire dashboard.
    """
    client = pg_rest_env["client"]
    good_slug = pg_rest_env["slug"]

    # Seed a valid session row, then poison its game_state row with non-JSON
    # text. snapshot_json is a TEXT column, so the corruption only surfaces at
    # load()-time deserialization — exactly the runtime failure the per-slug
    # try/except must contain.
    pool = db_pool.get_pool()
    bad_slug = _slug("bad")
    bad_sid = sessions.ensure_session(
        pool, slug=bad_slug, mode="solo", genre_slug="caverns", world_slug="beneath_sunden"
    )
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO game_state (session_id, snapshot_json, saved_at) VALUES (%s, %s, %s)",
            (bad_sid, "this is not valid json {{{", _now()),
        )

    resp = client.get("/api/debug/state")
    assert resp.status_code == 200, f"one bad save 500'd the State tab — body: {resp.text}"
    body = resp.json()
    keys = [v["session_key"] for v in body]
    # The good session survives; the bad one is logged-and-skipped (no view).
    assert good_slug in keys, f"good session missing after bad-save skip: {keys}"
    assert bad_slug not in keys
