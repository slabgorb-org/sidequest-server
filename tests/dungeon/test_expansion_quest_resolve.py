import sqlite3
from sidequest.dungeon.persistence import DungeonStore, ComplicationThread
from sidequest.game.session import GameSnapshot, QuestEntry
from sidequest.dungeon.expansion_quest import resolve_expansion_quests


def _store():
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    s = DungeonStore(conn); s.ensure_schema(); return conn, s


def _seed_thread(store, exp_id, sig, ref, region):
    store.open_thread(ComplicationThread(thread_id=f"q.exp{exp_id}.x", origin_region_id=region,
        kind="quest", status="open", started_at_depth_score=10.0,
        payload={"scope": "expansion", "expansion_id": exp_id, "signature_kind": sig,
                 "ref_id": ref, "anchor_region": region, "title": "t", "objective": "o"}))


def test_reach_deep_resolves_on_arrival():
    conn, store = _store(); _seed_thread(store, 1, "reach_deep", "exp001.r3", "exp001.r3"); conn.commit()
    snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="beneath_sunden")
    snap.quest_log["dungeon:exp1"] = QuestEntry(title="t", objective="o", status="active", anchor_id="exp001.r3")
    n = resolve_expansion_quests(snapshot=snap, store=store, reached_region_ids={"exp001.r3"},
                                 resolved_trope_ids=[], defeated_npc_names=set())
    conn.commit()
    assert n == 1
    assert snap.quest_log["dungeon:exp1"].status == "completed"
    assert store.open_threads() == []   # ledger thread resolved


def test_unfired_beat_does_not_resolve():
    conn, store = _store(); _seed_thread(store, 1, "reach_deep", "exp001.r3", "exp001.r3"); conn.commit()
    snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="beneath_sunden")
    snap.quest_log["dungeon:exp1"] = QuestEntry(title="t", objective="o", status="active", anchor_id="exp001.r3")
    n = resolve_expansion_quests(snapshot=snap, store=store, reached_region_ids=set(),
                                 resolved_trope_ids=[], defeated_npc_names=set())
    assert n == 0
    assert snap.quest_log["dungeon:exp1"].status == "active"
    assert len(store.open_threads()) == 1
