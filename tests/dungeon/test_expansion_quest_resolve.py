import sqlite3

from sidequest.dungeon.expansion_quest import resolve_expansion_quests
from sidequest.dungeon.persistence import ComplicationThread, DungeonStore
from sidequest.game.session import GameSnapshot, QuestEntry


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


def test_resolves_thread_even_when_quest_log_entry_absent():
    conn, store = _store(); _seed_thread(store, 1, "reach_deep", "exp001.r3", "exp001.r3"); conn.commit()
    snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="beneath_sunden")
    # quest_log deliberately NOT pre-populated
    n = resolve_expansion_quests(snapshot=snap, store=store, reached_region_ids={"exp001.r3"},
                                 resolved_trope_ids=[], defeated_npc_names=set())
    conn.commit()
    assert n == 1
    assert store.open_threads() == []


def test_unfired_beat_does_not_resolve():
    conn, store = _store(); _seed_thread(store, 1, "reach_deep", "exp001.r3", "exp001.r3"); conn.commit()
    snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="beneath_sunden")
    snap.quest_log["dungeon:exp1"] = QuestEntry(title="t", objective="o", status="active", anchor_id="exp001.r3")
    n = resolve_expansion_quests(snapshot=snap, store=store, reached_region_ids=set(),
                                 resolved_trope_ids=[], defeated_npc_names=set())
    assert n == 0
    assert snap.quest_log["dungeon:exp1"].status == "active"
    assert len(store.open_threads()) == 1


def test_set_piece_resolves_on_trope_resolution():
    """A set_piece-signature quest completes when its ref_id trope resolves.

    RED: resolve_expansion_quests is not yet called from the trope handshake
    site, so this drives the function directly to confirm the signature path works.
    GREEN: wiring in websocket_session_handler.py invokes it alongside
    resolve_complications_for_resolved_tropes.
    """
    conn, store = _store()
    _seed_thread(store, 2, "set_piece", "the_keeper_wakes", "exp002.r0")
    conn.commit()
    snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="beneath_sunden")
    snap.quest_log["dungeon:exp2"] = QuestEntry(
        title="Wake the Keeper", objective="Trigger the keeper set-piece.", status="active",
        anchor_id="exp002.r0",
    )
    n = resolve_expansion_quests(
        snapshot=snap,
        store=store,
        reached_region_ids=set(),
        resolved_trope_ids=["the_keeper_wakes"],
        defeated_npc_names=set(),
    )
    conn.commit()
    assert n == 1, f"expected 1 quest resolved, got {n}"
    assert snap.quest_log["dungeon:exp2"].status == "completed", (
        "quest log entry not flipped to 'completed' after set_piece trope resolved"
    )
    assert store.open_threads() == [], "ledger thread still open after set_piece resolution"
