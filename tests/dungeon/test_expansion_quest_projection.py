"""TDD: reconcile_dungeon_quests_into_log — projects open expansion-quest threads
into snapshot.quest_log as namespaced QuestEntry rows (dungeon:expN)."""

import sqlite3

from sidequest.dungeon.persistence import ComplicationThread, DungeonStore
from sidequest.dungeon.expansion_quest import reconcile_dungeon_quests_into_log
from sidequest.game.session import GameSnapshot, QuestEntry


def _store():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    s = DungeonStore(conn)
    s.ensure_schema()
    return conn, s


def _thread(exp_id, region):
    return ComplicationThread(
        thread_id=f"q.exp{exp_id}.x",
        origin_region_id=region,
        kind="quest",
        status="open",
        started_at_depth_score=10.0,
        payload={
            "scope": "expansion",
            "expansion_id": exp_id,
            "signature_kind": "reach_deep",
            "ref_id": region,
            "anchor_region": region,
            "title": "Go deep",
            "objective": "Reach the bottom.",
        },
    )


def test_projection_writes_namespaced_entry_when_reached():
    conn, store = _store()
    store.open_thread(_thread(1, "exp001.r2"))
    conn.commit()
    snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="beneath_sunden")
    n = reconcile_dungeon_quests_into_log(snapshot=snap, store=store, reached_expansion_ids={1})
    assert n == 1
    entry = snap.quest_log["dungeon:exp1"]
    assert entry.objective == "Reach the bottom."
    assert entry.status == "active"
    assert entry.anchor_id == "exp001.r2"


def test_projection_skips_unreached_expansion():
    conn, store = _store()
    store.open_thread(_thread(2, "exp002.r0"))
    conn.commit()
    snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="beneath_sunden")
    n = reconcile_dungeon_quests_into_log(
        snapshot=snap, store=store, reached_expansion_ids=set()
    )
    assert n == 0
    assert "dungeon:exp2" not in snap.quest_log


def test_projection_never_touches_non_dungeon_entries():
    conn, store = _store()
    store.open_thread(_thread(1, "exp001.r0"))
    conn.commit()
    snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="beneath_sunden")
    snap.quest_log["seed_drive"] = QuestEntry(title="My Drive", objective="x", status="active")
    reconcile_dungeon_quests_into_log(snapshot=snap, store=store, reached_expansion_ids={1})
    assert snap.quest_log["seed_drive"].title == "My Drive"  # untouched
