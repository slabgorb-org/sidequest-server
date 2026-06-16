import json
import sqlite3
from pathlib import Path

from sidequest.game.forensic_query import list_saves
from sidequest.game.persistence import SqliteStore


def _make_save(saves_dir: Path, slug: str, *, genre: str, world: str) -> SqliteStore:
    db = saves_dir / "games" / slug / "save.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore.open(str(db))
    conn = store.connection()
    conn.execute(
        "INSERT OR REPLACE INTO session_meta "
        "(id, genre_slug, world_slug, created_at, last_played, schema_version) "
        "VALUES (1, ?, ?, ?, ?, 1)",
        (genre, world, "2026-05-18T00:00:00+00:00", "2026-05-18T00:05:00+00:00"),
    )
    conn.commit()
    return store


def test_list_saves_returns_meta_for_each_save(tmp_path):
    saves = tmp_path / "saves"
    s = _make_save(saves, "caverns_and_claudes_test", genre="caverns_and_claudes", world="test")
    s.close()
    result = list_saves(saves)
    assert len(result) == 1
    row = result[0]
    assert row["slug"] == "caverns_and_claudes_test"
    assert row["genre"] == "caverns_and_claudes"
    assert row["world"] == "test"
    assert "last_activity_ts" in row


def test_list_saves_skips_broken_db_loudly(tmp_path, caplog):
    saves = tmp_path / "saves"
    s = _make_save(saves, "good_save", genre="g", world="w")
    s.close()
    broken = saves / "games" / "broken_save" / "save.db"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("this is not sqlite")
    with caplog.at_level("WARNING"):
        result = list_saves(saves)
    slugs = {r["slug"] for r in result}
    assert slugs == {"good_save"}
    assert "broken_save" in caplog.text


def test_list_saves_missing_root_returns_empty(tmp_path):
    assert list_saves(tmp_path / "nope") == []


def test_list_saves_does_not_mutate_the_save(tmp_path):
    """list_saves must read without writing. SqliteStore.open re-runs the
    full schema (executescript materializes every missing table) and flips
    journal_mode=WAL — a write to the main save.db. The read-only path must
    leave the file byte-identical. Seed via plain sqlite3 in DELETE mode
    (NOT SqliteStore) so the buggy path's schema-creation write is real and
    observable, and there is no pre-existing WAL-header confound."""
    saves = tmp_path / "saves"
    db = saves / "games" / "ro_test" / "save.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db))
    con.executescript(
        "PRAGMA journal_mode=DELETE;"
        "CREATE TABLE session_meta ("
        " id INTEGER PRIMARY KEY CHECK (id = 1),"
        " genre_slug TEXT NOT NULL, world_slug TEXT NOT NULL,"
        " created_at TEXT NOT NULL, last_played TEXT NOT NULL,"
        " schema_version INTEGER NOT NULL DEFAULT 1);"
        "INSERT INTO session_meta VALUES"
        " (1,'g','w','2026-05-18T00:00:00+00:00','2026-05-18T00:05:00+00:00',1);"
    )
    con.commit()
    con.close()
    bytes_before = db.read_bytes()
    mtime_before = db.stat().st_mtime_ns

    result = list_saves(saves)

    assert {r["slug"] for r in result} == {"ro_test"}  # save is readable
    assert db.read_bytes() == bytes_before  # main db not rewritten
    assert db.stat().st_mtime_ns == mtime_before  # main db not touched


def test_list_saves_skips_save_with_no_meta_loudly(tmp_path, caplog):
    saves = tmp_path / "saves"
    db = saves / "games" / "no_meta_save" / "save.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE session_meta ("
        " id INTEGER PRIMARY KEY CHECK (id = 1),"
        " genre_slug TEXT NOT NULL, world_slug TEXT NOT NULL,"
        " created_at TEXT NOT NULL, last_played TEXT NOT NULL,"
        " schema_version INTEGER NOT NULL DEFAULT 1)"
    )  # table exists, but NO id=1 row inserted
    con.commit()
    con.close()
    with caplog.at_level("WARNING"):
        result = list_saves(saves)
    assert {r["slug"] for r in result} == set()  # skipped, not returned
    assert "forensic_query.no_meta" in caplog.text
    assert "no_meta_save" in caplog.text


def _seed_rounds(store):
    """Round 1: 2 events + 1 narrative. Round 2: 1 event + 1 narrative.

    Uses production timestamp SHAPES (Spike F5.2): narrative_log.created_at
    is sqlite datetime('now') form 'YYYY-MM-DD HH:MM:SS'; events.created_at
    is Python .isoformat() 'YYYY-MM-DDTHH:MM:SS.ffffff+00:00'.
    """
    conn = store.connection()
    conn.execute(
        "INSERT INTO narrative_log (round_number, author, content, tags, created_at) "
        "VALUES (1, 'narrator', 'You enter the cave.', '[]', '2026-05-18 00:01:00')"
    )
    conn.commit()
    conn.execute(
        "INSERT INTO events (kind, payload_json, created_at) VALUES "
        "('NARRATION', ?, '2026-05-18T00:01:01.000000+00:00')",
        (
            json.dumps(
                {
                    "text": "You enter the cave.",
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
        ),
    )
    conn.execute(
        "INSERT INTO events (kind, payload_json, created_at) VALUES "
        "('SCRAPBOOK_ENTRY', ?, '2026-05-18T00:01:02.000000+00:00')",
        (json.dumps({"turn_id": 1, "location": "Cave"}),),
    )
    conn.execute(
        "INSERT INTO narrative_log (round_number, author, content, tags, created_at) "
        "VALUES (2, 'narrator', 'A goblin lunges.', '[]', '2026-05-18 00:02:00')"
    )
    conn.execute(
        "INSERT INTO events (kind, payload_json, created_at) VALUES "
        "('NARRATION', ?, '2026-05-18T00:02:01.000000+00:00')",
        (
            json.dumps(
                {
                    "text": "A goblin lunges.",
                    "footnotes": [
                        {
                            "fact_id": "fn-goblin",
                            "summary": "A goblin guards the hall.",
                            "category": "Person",
                            "is_new": True,
                        }
                    ],
                    "_visibility": {"visible_to": "all"},
                }
            ),
        ),
    )
    conn.commit()


def test_build_timeline_buckets_events_by_round(tmp_path):
    from sidequest.game.forensic_query import _ro_connect, build_timeline

    saves = tmp_path / "saves"
    store = _make_save(saves, "tl_test", genre="g", world="w")
    _seed_rounds(store)
    store.close()
    db = saves / "games" / "tl_test" / "save.db"
    conn = _ro_connect(db)
    try:
        timeline = build_timeline(conn)
    finally:
        conn.close()

    assert [t["round"] for t in timeline] == [1, 2]
    r1, r2 = timeline
    assert r1["seq_start"] == 1 and r1["seq_end"] == 2
    assert r1["event_kind_counts"] == {"NARRATION": 1, "SCRAPBOOK_ENTRY": 1}
    assert r1["narrative_authors"] == ["narrator"]
    assert r2["seq_start"] == 3 and r2["seq_end"] == 3
    assert r2["event_kind_counts"] == {"NARRATION": 1}


def test_build_turn_bundle_assembles_all_panels(tmp_path):
    from sidequest.game.forensic_query import _ro_connect, build_turn_bundle

    saves = tmp_path / "saves"
    store = _make_save(saves, "bundle_test", genre="g", world="w")
    _seed_rounds(store)
    c = store.connection()
    c.execute(
        "INSERT INTO projection_cache (event_seq, player_id, include, payload_json) "
        "VALUES (1, 'player1', 1, ?)",
        (json.dumps({"type": "NARRATION", "text": "You enter the cave."}),),
    )
    c.execute(
        "INSERT INTO scrapbook_entries "
        "(turn_id, scene_title, scene_type, location, image_url, narrative_excerpt, "
        " world_facts, npcs_present, render_status) "
        "VALUES (1, 'The Cave Mouth', 'exploration', 'Cave', NULL, 'You enter.', "
        " '[]', '[]', 'rendered')"
    )
    c.commit()
    store.close()
    db = saves / "games" / "bundle_test" / "save.db"
    conn = _ro_connect(db)
    try:
        bundle = build_turn_bundle(conn, 1)
    finally:
        conn.close()

    assert bundle["round"] == 1
    assert [n["content"] for n in bundle["narrative"]] == ["You enter the cave."]
    assert [e["seq"] for e in bundle["events"]] == [1, 2]
    assert bundle["events"][0]["kind"] == "NARRATION"
    assert bundle["derived"]["fn-cave"]["value"] == {
        "summary": "The cave mouth opens into darkness.",
        "category": "Place",
    }
    assert bundle["derived"]["fn-cave"]["source_seqs"] == [1]
    assert bundle["unparseable_seqs"] == []
    assert bundle["projection"][0]["player_id"] == "player1"
    assert bundle["projection"][0]["include"] == 1
    assert bundle["scrapbook"][0]["scene_title"] == "The Cave Mouth"


def test_build_turn_bundle_unknown_round_is_empty_not_error(tmp_path):
    from sidequest.game.forensic_query import _ro_connect, build_turn_bundle

    saves = tmp_path / "saves"
    store = _make_save(saves, "empty_round", genre="g", world="w")
    _seed_rounds(store)
    store.close()
    db = saves / "games" / "empty_round" / "save.db"
    conn = _ro_connect(db)
    try:
        bundle = build_turn_bundle(conn, 999)
    finally:
        conn.close()
    assert bundle["round"] == 999
    assert bundle["narrative"] == []
    assert bundle["events"] == []
    assert bundle["derived"] == {}
    assert bundle["projection"] == []
    assert bundle["scrapbook"] == []
    assert bundle["unparseable_seqs"] == []


def test_build_turn_bundle_never_raises_on_corrupt_stored_json(tmp_path, caplog):
    from sidequest.game.forensic_query import _ro_connect, build_turn_bundle

    saves = tmp_path / "saves"
    store = _make_save(saves, "corrupt", genre="g", world="w")
    c = store.connection()
    c.execute(
        "INSERT INTO narrative_log (round_number, author, content, tags, created_at) "
        "VALUES (1, 'narrator', 'hi', '{not json', '2026-05-18 00:01:00')"
    )
    c.execute(
        "INSERT INTO events (kind, payload_json, created_at) VALUES "
        "('NARRATION', '{bad json', '2026-05-18T00:01:01.000000+00:00')"
    )
    c.execute(
        "INSERT INTO scrapbook_entries "
        "(turn_id, scene_title, scene_type, location, image_url, narrative_excerpt, "
        " world_facts, npcs_present, render_status) "
        "VALUES (1, 'S', 't', 'L', NULL, 'x', '{bad', '[]', 'rendered')"
    )
    c.commit()
    store.close()
    db = saves / "games" / "corrupt" / "save.db"
    conn = _ro_connect(db)
    try:
        with caplog.at_level("WARNING"):
            bundle = build_turn_bundle(conn, 1)  # must NOT raise
    finally:
        conn.close()

    assert bundle["events"][0]["payload"] == {"__unparseable__": "{bad json"}
    assert 1 in bundle["unparseable_seqs"]
    assert bundle["narrative"][0]["tags"] == []
    assert bundle["scrapbook"][0]["world_facts"] == []
    assert "forensic_query.unparseable_json_list" in caplog.text


def test_bundle_telemetry_buckets_by_event_seq_range_and_round(tmp_path):
    """Telemetry rows for a round = event_seq within the round's seq
    range, PLUS rows whose `round` column matches (covers NULL-event_seq)."""
    from sidequest.game.forensic_query import _ro_connect, build_turn_bundle

    saves = tmp_path / "saves"
    store = _make_save(saves, "tel_bucket", genre="g", world="w")
    _seed_rounds(store)
    db = saves / "games" / "tel_bucket" / "save.db"
    con = store.connection()
    con.executescript(
        "CREATE TABLE IF NOT EXISTS turn_telemetry ("
        " seq INTEGER PRIMARY KEY AUTOINCREMENT, event_seq INTEGER,"
        " round INTEGER, ts TEXT NOT NULL, component TEXT NOT NULL,"
        " event_type TEXT NOT NULL, payload_json TEXT NOT NULL);"
    )
    lo = con.execute("SELECT MIN(seq) AS lo_seq FROM events").fetchone()["lo_seq"]
    con.executemany(
        "INSERT INTO turn_telemetry "
        "(event_seq, round, ts, component, event_type, payload_json) "
        "VALUES (?,?,?,?,?,?)",
        [
            (lo, None, "t", "intent", "state_transition", '{"label":"a"}'),
            (None, 1, "t", "beat", "selected", '{"beat":"b"}'),
            (None, 2, "t", "intent", "state_transition", '{"label":"c"}'),
        ],
    )
    con.commit()
    store.close()
    conn = _ro_connect(db)
    try:
        bundle = build_turn_bundle(conn, 1)
        tel = bundle["telemetry"]
        assert tel["total"] == 2
        assert tel["by_component"] == {
            "intent": {"state_transition": 1},
            "beat": {"selected": 1},
        }
    finally:
        conn.close()


def test_bundle_missing_turn_telemetry_table_is_zero_rows_not_error(tmp_path):
    """Old saves predate the table. forensics is read-only and must NOT
    create it; a missing table behaves exactly like zero rows.

    Seeds via plain sqlite3 (NOT SqliteStore) so the turn_telemetry table
    is genuinely absent — matching the pre-Task-2 save shape.
    """
    from sidequest.game.forensic_query import _ro_connect, build_turn_bundle

    saves = tmp_path / "saves"
    db = saves / "games" / "no_tel_table" / "save.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db))
    con.executescript(
        "PRAGMA journal_mode=DELETE;"
        "CREATE TABLE session_meta ("
        " id INTEGER PRIMARY KEY CHECK (id = 1),"
        " genre_slug TEXT NOT NULL, world_slug TEXT NOT NULL,"
        " created_at TEXT NOT NULL, last_played TEXT NOT NULL,"
        " schema_version INTEGER NOT NULL DEFAULT 1);"
        "INSERT INTO session_meta VALUES (1,'g','w','t','t',1);"
        "CREATE TABLE narrative_log ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " round_number INTEGER NOT NULL, author TEXT NOT NULL,"
        " content TEXT NOT NULL, tags TEXT, created_at TEXT NOT NULL);"
        "CREATE TABLE events ("
        " seq INTEGER PRIMARY KEY AUTOINCREMENT,"
        " kind TEXT NOT NULL, payload_json TEXT NOT NULL,"
        " created_at TEXT NOT NULL);"
        "CREATE TABLE projection_cache ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " event_seq INTEGER NOT NULL, player_id TEXT NOT NULL,"
        " include INTEGER NOT NULL DEFAULT 1, payload_json TEXT NOT NULL);"
        "CREATE TABLE scrapbook_entries ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, turn_id INTEGER NOT NULL,"
        " scene_title TEXT, scene_type TEXT, location TEXT,"
        " image_url TEXT, narrative_excerpt TEXT,"
        " world_facts TEXT, npcs_present TEXT, render_status TEXT);"
        "INSERT INTO narrative_log (round_number,author,content,tags,created_at)"
        " VALUES (1,'narrator','hi','[]','2026-05-18 00:01:00');"
        "INSERT INTO events (kind,payload_json,created_at)"
        ' VALUES (\'NARRATION\',\'{"text":"hi","footnotes":[],"_visibility":{"visible_to":"all"}}\',\'2026-05-18T00:01:01.000000+00:00\');'
    )
    con.commit()
    con.close()
    conn = _ro_connect(db)
    try:
        bundle = build_turn_bundle(conn, 1)
        assert bundle["telemetry"] == {
            "rows": [],
            "by_component": {},
            "total": 0,
            "unparseable_seqs": [],
        }
    finally:
        conn.close()


def test_bundle_unknown_round_includes_empty_telemetry_key(tmp_path):
    from sidequest.game.forensic_query import _ro_connect, build_turn_bundle

    saves = tmp_path / "saves"
    store = _make_save(saves, "unknown_rnd_tel", genre="g", world="w")
    _seed_rounds(store)
    store.close()
    db = saves / "games" / "unknown_rnd_tel" / "save.db"
    conn = _ro_connect(db)
    try:
        bundle = build_turn_bundle(conn, 999)
        assert bundle["telemetry"] == {
            "rows": [],
            "by_component": {},
            "total": 0,
            "unparseable_seqs": [],
        }
    finally:
        conn.close()


def test_telemetry_read_does_not_mutate_the_save(tmp_path):
    """Read-only byte-identity: a forensics read over a save WITH
    turn_telemetry leaves save.db byte-identical.

    SqliteStore already creates turn_telemetry and sets WAL mode, so we
    seed the row directly via the store connection (no journal_mode flip,
    no duplicate CREATE TABLE).  We use PRAGMA wal_checkpoint + PRAGMA
    journal_mode to flush the WAL into the main file before snapshotting
    bytes_before so the comparison is apples-to-apples.
    """
    from sidequest.game.forensic_query import _ro_connect, build_turn_bundle

    saves = tmp_path / "saves"
    store = _make_save(saves, "tel_ro", genre="g", world="w")
    _seed_rounds(store)
    db = saves / "games" / "tel_ro" / "save.db"
    con = store.connection()
    con.execute(
        "INSERT INTO turn_telemetry (event_seq,round,ts,component,event_type,payload_json)"
        " VALUES (1,1,'t','c','e','{}')"
    )
    con.commit()
    # Checkpoint the WAL so the main db file is the canonical source and
    # there is no pending -wal that a read-only open could see as a write.
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.commit()
    store.close()
    bytes_before = db.read_bytes()
    mtime_before = db.stat().st_mtime_ns
    conn = _ro_connect(db)
    try:
        build_turn_bundle(conn, 1)
    finally:
        conn.close()
    assert db.read_bytes() == bytes_before
    assert db.stat().st_mtime_ns == mtime_before


def test_list_saves_includes_telemetry_row_count(tmp_path):
    saves = tmp_path / "saves"
    db = saves / "games" / "tel" / "save.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db))
    con.executescript(
        "PRAGMA journal_mode=DELETE;"
        "CREATE TABLE session_meta (id INTEGER PRIMARY KEY CHECK (id=1),"
        " genre_slug TEXT NOT NULL, world_slug TEXT NOT NULL,"
        " created_at TEXT NOT NULL, last_played TEXT NOT NULL,"
        " schema_version INTEGER NOT NULL DEFAULT 1);"
        "INSERT INTO session_meta VALUES (1,'g','w','2026-05-18T00:00:00+00:00','2026-05-18T00:05:00+00:00',1);"
        "CREATE TABLE turn_telemetry (seq INTEGER PRIMARY KEY AUTOINCREMENT,"
        " event_seq INTEGER, round INTEGER, ts TEXT NOT NULL,"
        " component TEXT NOT NULL, event_type TEXT NOT NULL, payload_json TEXT NOT NULL);"
        "INSERT INTO turn_telemetry (event_seq,round,ts,component,event_type,payload_json)"
        " VALUES (1,1,'t','c','e','{}'),(2,1,'t','c','e','{}');"
    )
    con.commit()
    con.close()
    [save] = list_saves(saves)
    assert save["telemetry_rows"] == 2


def test_list_saves_telemetry_count_zero_when_table_missing(tmp_path):
    saves = tmp_path / "saves"
    db = saves / "games" / "old" / "save.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db))
    con.executescript(
        "PRAGMA journal_mode=DELETE;"
        "CREATE TABLE session_meta (id INTEGER PRIMARY KEY CHECK (id=1),"
        " genre_slug TEXT NOT NULL, world_slug TEXT NOT NULL,"
        " created_at TEXT NOT NULL, last_played TEXT NOT NULL,"
        " schema_version INTEGER NOT NULL DEFAULT 1);"
        "INSERT INTO session_meta VALUES (1,'g','w','2026-05-18T00:00:00+00:00','2026-05-18T00:05:00+00:00',1);"
    )
    con.commit()
    con.close()
    [save] = list_saves(saves)
    assert save["telemetry_rows"] == 0  # missing table -> 0, not error


def test_list_saves_includes_mechanical_row_count(tmp_path):
    import sqlite3

    from sidequest.game.forensic_query import list_saves

    saves = tmp_path / "saves"
    db = saves / "games" / "mech" / "save.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db))
    con.executescript(
        "PRAGMA journal_mode=DELETE;"
        "CREATE TABLE session_meta (id INTEGER PRIMARY KEY CHECK (id=1),"
        " genre_slug TEXT NOT NULL, world_slug TEXT NOT NULL,"
        " created_at TEXT NOT NULL, last_played TEXT NOT NULL,"
        " schema_version INTEGER NOT NULL DEFAULT 1);"
        "INSERT INTO session_meta VALUES "
        "(1,'g','w','2026-05-18T00:00:00+00:00','2026-05-18T00:05:00+00:00',1);"
        "CREATE TABLE turn_telemetry (seq INTEGER PRIMARY KEY AUTOINCREMENT,"
        " event_seq INTEGER, round INTEGER, ts TEXT NOT NULL,"
        " component TEXT NOT NULL, event_type TEXT NOT NULL,"
        " payload_json TEXT NOT NULL);"
        "INSERT INTO turn_telemetry "
        "(event_seq,round,ts,component,event_type,payload_json) VALUES "
        "(1,1,'t','mechanical','census','{}'),"
        "(1,1,'t','intent','state_transition','{}'),"
        "(2,2,'t','mechanical','census','{}');"
    )
    con.commit()
    con.close()
    [save] = list_saves(saves)
    assert save["mechanical_rows"] == 2  # only component='mechanical'
    assert save["telemetry_rows"] == 3  # Phase-1 count unchanged (all rows)


def test_list_saves_mechanical_count_zero_when_table_missing(tmp_path):
    import sqlite3

    from sidequest.game.forensic_query import list_saves

    saves = tmp_path / "saves"
    db = saves / "games" / "old" / "save.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db))
    con.executescript(
        "PRAGMA journal_mode=DELETE;"
        "CREATE TABLE session_meta (id INTEGER PRIMARY KEY CHECK (id=1),"
        " genre_slug TEXT NOT NULL, world_slug TEXT NOT NULL,"
        " created_at TEXT NOT NULL, last_played TEXT NOT NULL,"
        " schema_version INTEGER NOT NULL DEFAULT 1);"
        "INSERT INTO session_meta VALUES "
        "(1,'g','w','2026-05-18T00:00:00+00:00','2026-05-18T00:05:00+00:00',1);"
    )
    con.commit()
    con.close()
    [save] = list_saves(saves)
    assert save["mechanical_rows"] == 0  # missing table -> 0, not error


def test_list_saves_telemetry_count_zero_when_table_present_but_empty(tmp_path):
    # Distinct from the missing-table guard: the table EXISTS, so the real
    # SELECT COUNT(*) path executes and must return 0 (not the else-branch).
    saves = tmp_path / "saves"
    db = saves / "games" / "empty" / "save.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db))
    con.executescript(
        "PRAGMA journal_mode=DELETE;"
        "CREATE TABLE session_meta (id INTEGER PRIMARY KEY CHECK (id=1),"
        " genre_slug TEXT NOT NULL, world_slug TEXT NOT NULL,"
        " created_at TEXT NOT NULL, last_played TEXT NOT NULL,"
        " schema_version INTEGER NOT NULL DEFAULT 1);"
        "INSERT INTO session_meta VALUES (1,'g','w','2026-05-18T00:00:00+00:00','2026-05-18T00:05:00+00:00',1);"
        "CREATE TABLE turn_telemetry (seq INTEGER PRIMARY KEY AUTOINCREMENT,"
        " event_seq INTEGER, round INTEGER, ts TEXT NOT NULL,"
        " component TEXT NOT NULL, event_type TEXT NOT NULL, payload_json TEXT NOT NULL);"
        # NB: no INSERT INTO turn_telemetry — table present, zero rows
    )
    con.commit()
    con.close()
    [save] = list_saves(saves)
    assert (
        save["telemetry_rows"] == 0
    )  # real COUNT(*) on an empty table, not the missing-table guard


def _add_mechanical(db, rows):
    """rows = list of (event_seq, round, event_type, payload_dict)."""
    con = sqlite3.connect(str(db))
    con.executescript(
        "CREATE TABLE IF NOT EXISTS turn_telemetry ("
        " seq INTEGER PRIMARY KEY AUTOINCREMENT, event_seq INTEGER,"
        " round INTEGER, ts TEXT NOT NULL, component TEXT NOT NULL,"
        " event_type TEXT NOT NULL, payload_json TEXT NOT NULL);"
    )
    con.executemany(
        "INSERT INTO turn_telemetry "
        "(event_seq, round, ts, component, event_type, payload_json) "
        "VALUES (?,?,?,?,?,?)",
        [(es, rn, "t", "mechanical", et, json.dumps(p)) for es, rn, et, p in rows],
    )
    con.commit()
    con.close()


def test_bundle_mechanical_diffs_round_against_prev_census_round(tmp_path):
    db = tmp_path / "saves" / "games" / "mech" / "save.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    store = _make_save(tmp_path / "saves", "mech", genre="g", world="w")
    _seed_rounds(store)  # rounds 1 & 2 of events/narrative
    store.close()
    base = {
        "player_id": "p1",
        "character_name": "Rux",
        "seat": 0,
        "edge": {"current": 10, "max": 10},
        "location": "Cave",
        "inventory": [],
        "xp": 0,
        "level": 1,
        "acquired_advancements": [],
    }
    _add_mechanical(
        db,
        [
            (None, 1, "census", {**base, "round": 1}),
            (None, 2, "census", {**base, "round": 2, "xp": 25}),
        ],
    )
    from sidequest.game.forensic_query import _ro_connect, build_turn_bundle

    conn = _ro_connect(db)
    try:
        b = build_turn_bundle(conn, 2)
        m = b["mechanical"]
        assert m["state"] == "moved"
        [pc] = m["pcs"]
        assert pc["player_id"] == "p1"
        assert pc["kind"] == "moved"
        assert dict(pc["deltas"])["xp"] == "+25"
    finally:
        conn.close()


def test_bundle_missing_turn_telemetry_table_is_absent_not_error(tmp_path):
    store = _make_save(tmp_path / "saves", "old", genre="g", world="w")
    _seed_rounds(store)  # NO turn_telemetry table
    store.close()
    from sidequest.game.forensic_query import _ro_connect, build_turn_bundle

    db = tmp_path / "saves" / "games" / "old" / "save.db"
    conn = _ro_connect(db)
    try:
        b = build_turn_bundle(conn, 1)
        assert b["mechanical"] == {
            "state": "absent",
            "pcs": [],
            "trope": None,
            "unparseable_seqs": [],
        }
    finally:
        conn.close()


def test_bundle_unknown_round_includes_empty_mechanical_key(tmp_path):
    store = _make_save(tmp_path / "saves", "uk", genre="g", world="w")
    _seed_rounds(store)
    store.close()
    from sidequest.game.forensic_query import _ro_connect, build_turn_bundle

    db = tmp_path / "saves" / "games" / "uk" / "save.db"
    conn = _ro_connect(db)
    try:
        b = build_turn_bundle(conn, 999)
        assert b["mechanical"] == {
            "state": "absent",
            "pcs": [],
            "trope": None,
            "unparseable_seqs": [],
        }
    finally:
        conn.close()


def test_mechanical_read_does_not_mutate_the_save(tmp_path):
    store = _make_save(tmp_path / "saves", "bi", genre="g", world="w")
    _seed_rounds(store)
    store.close()
    db = tmp_path / "saves" / "games" / "bi" / "save.db"
    con = sqlite3.connect(str(db))
    con.executescript("PRAGMA journal_mode=DELETE;")
    con.commit()
    con.close()
    _add_mechanical(
        db,
        [
            (
                None,
                1,
                "census",
                {
                    "player_id": "p1",
                    "character_name": "Rux",
                    "seat": 0,
                    "round": 1,
                    "edge": {"current": 1, "max": 1},
                    "location": "Cave",
                    "inventory": [],
                    "xp": 0,
                    "level": 1,
                    "acquired_advancements": [],
                },
            )
        ],
    )
    bytes_before = db.read_bytes()
    mtime_before = db.stat().st_mtime_ns
    from sidequest.game.forensic_query import _ro_connect, build_turn_bundle

    conn = _ro_connect(db)
    try:
        build_turn_bundle(conn, 1)
    finally:
        conn.close()
    assert db.read_bytes() == bytes_before
    assert db.stat().st_mtime_ns == mtime_before
