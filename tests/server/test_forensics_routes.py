import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sidequest.server.app import create_app


def _client(tmp_path: Path) -> TestClient:
    packs = tmp_path / "genre_packs"
    packs.mkdir(parents=True, exist_ok=True)
    saves = tmp_path / "saves"
    saves.mkdir(parents=True, exist_ok=True)
    app = create_app(genre_pack_search_paths=[packs], save_dir=saves)
    return TestClient(app)


@pytest.fixture
def pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Bind the process pool to a per-worker throwaway PG db, clean per test.

    ADR-115 D2: the forensics REST endpoints (/api/debug/saves, .../timeline,
    .../turn/N, .../snapshot) now read the authoritative forensic tables from
    Postgres via db_pool.get_pool() + PgForensicReader — NOT the SQLite
    save_dir db. Seed and read must share one isolated database, so this
    fixture truncates and binds the per-worker migrated db to the pool.
    """
    import psycopg

    from sidequest.game import db_pool

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


def _seed_pg(slug: str) -> int:
    """Seed one round of forensic data into Postgres via the A2-C1 stores —
    the authoritative read path under ADR-115 D2.

    Mirrors the legacy SQLite _seed: a session row + one narrative entry +
    one NARRATION event carrying the ``fn-cave`` footnote the turn-bundle
    derived panel folds. Returns the session_id.

    Narrative is appended FIRST (production order: persistence phase writes
    narrative_log before the broadcast/emit phase), so narrative_log holds
    the lowest created_at for the round — the boundary build_timeline relies
    on (see tests/persistence/test_pg_forensic.py module docstring).
    """
    from sidequest.game import db_pool
    from sidequest.game.pg import sessions
    from sidequest.game.pg.events import PgEventStore
    from sidequest.game.pg.narrative import PgNarrativeStore
    from sidequest.game.session import NarrativeEntry

    pool = db_pool.get_pool()
    sid = sessions.ensure_session(
        pool, slug=slug, mode="solo", genre_slug="caverns_and_claudes", world_slug="test"
    )
    narr_store = PgNarrativeStore(pool, session_id=sid)
    ev_store = PgEventStore(pool, session_id=sid)

    narr_store.append_narrative(
        NarrativeEntry(timestamp=0, round=1, author="narrator", content="You enter.", tags=[])
    )
    ev_store.append_event(
        kind="NARRATION",
        payload_json=json.dumps(
            {
                "text": "You enter.",
                "footnotes": [
                    {
                        "fact_id": "fn-cave",
                        "summary": "The cave mouth opens into darkness.",
                        "category": "Place",
                        "is_new": True,
                    }
                ],
                "_visibility": {"visible_to": "all"},
            }
        ),
    )
    return sid


def test_list_saves_endpoint(tmp_path, pg_isolation):
    _seed_pg("caverns_and_claudes_test")
    client = _client(tmp_path)
    resp = client.get("/api/debug/saves")
    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["slug"] == "caverns_and_claudes_test"
    assert body[0]["genre"] == "caverns_and_claudes"


def test_timeline_endpoint(tmp_path, pg_isolation):
    _seed_pg("caverns_and_claudes_test")
    client = _client(tmp_path)
    resp = client.get("/api/debug/save/caverns_and_claudes_test/timeline")
    assert resp.status_code == 200
    assert resp.json()[0]["round"] == 1


def test_turn_bundle_endpoint(tmp_path, pg_isolation):
    _seed_pg("caverns_and_claudes_test")
    client = _client(tmp_path)
    resp = client.get("/api/debug/save/caverns_and_claudes_test/turn/1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["round"] == 1
    assert body["derived"]["fn-cave"]["value"]["summary"] == "The cave mouth opens into darkness."
    assert body["derived"]["fn-cave"]["value"]["category"] == "Place"


def test_turn_bundle_unknown_slug_is_empty_not_500(tmp_path):
    client = _client(tmp_path)
    resp = client.get("/api/debug/save/nope/turn/1")
    assert resp.status_code == 200
    assert resp.json() == {
        "round": 1,
        "narrative": [],
        "events": [],
        "derived": {},
        "projection": [],
        "scrapbook": [],
        "unparseable_seqs": [],
        "telemetry": {"rows": [], "by_component": {}, "total": 0, "unparseable_seqs": []},
        "mechanical": {"state": "absent", "pcs": [], "trope": None, "unparseable_seqs": []},
    }


def test_turn_bundle_corrupt_save_is_empty_not_500(tmp_path):
    """D7.4: a present-but-corrupt save degrades to empty, never 500."""
    saves = tmp_path / "saves"
    db = saves / "games" / "corruptslug" / "save.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_text("this is not a sqlite database")
    client = _client(tmp_path)
    resp = client.get("/api/debug/save/corruptslug/turn/1")
    assert resp.status_code == 200
    assert resp.json() == {
        "round": 1,
        "narrative": [],
        "events": [],
        "derived": {},
        "projection": [],
        "scrapbook": [],
        "unparseable_seqs": [],
        "telemetry": {"rows": [], "by_component": {}, "total": 0, "unparseable_seqs": []},
        "mechanical": {"state": "absent", "pcs": [], "trope": None, "unparseable_seqs": []},
    }


def test_timeline_unknown_slug_is_empty_not_500(tmp_path):
    resp = _client(tmp_path).get("/api/debug/save/nope/timeline")
    assert resp.status_code == 200
    assert resp.json() == []


def test_timeline_corrupt_save_is_empty_not_500(tmp_path):
    saves = tmp_path / "saves"
    db = saves / "games" / "corruptslug" / "save.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_text("this is not a sqlite database")
    resp = _client(tmp_path).get("/api/debug/save/corruptslug/timeline")
    assert resp.status_code == 200
    assert resp.json() == []


def test_snapshot_endpoint_returns_persisted_state(tmp_path, pg_isolation):
    """ADR-115 D2: /snapshot now reads game_state.snapshot_json from Postgres
    via PgForensicReader.snapshot_json (a raw verbatim decode), NOT the SQLite
    save_dir db. The old SQLite-file read-only/unmodified assertions are gone
    because there is no longer a per-save SQLite file in the read path. We seed
    game_state directly (the endpoint returns the stored dict verbatim, never
    round-tripping through the domain model) and assert the raw passthrough.
    """
    from sidequest.game import db_pool
    from sidequest.game.pg import sessions

    pool = db_pool.get_pool()
    sid = sessions.ensure_session(pool, slug="snap_ok", mode="solo", genre_slug="g", world_slug="w")
    with pool.connection() as conn, conn.transaction():
        conn.execute(
            "INSERT INTO game_state (session_id, snapshot_json, saved_at) VALUES (%s, %s, %s)",
            (sid, json.dumps({"location": "Cave"}), "2026-05-18T00:00:00+00:00"),
        )

    client = _client(tmp_path)
    resp = client.get("/api/debug/save/snap_ok/snapshot")
    assert resp.status_code == 200
    assert resp.json() == {"location": "Cave"}  # persisted snapshot returned verbatim


def test_snapshot_endpoint_unknown_slug_is_empty_not_500(tmp_path):
    resp = _client(tmp_path).get("/api/debug/save/nope/snapshot")
    assert resp.status_code == 200
    assert resp.json() == {}


def test_snapshot_endpoint_corrupt_save_is_empty_not_500(tmp_path):
    saves = tmp_path / "saves"
    db = saves / "games" / "snap_bad" / "save.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_text("this is not a sqlite database")
    resp = _client(tmp_path).get("/api/debug/save/snap_bad/snapshot")
    assert resp.status_code == 200
    assert resp.json() == {}
