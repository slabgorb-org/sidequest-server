"""Task 4: seed_expansion_quest — opens an expansion-scoped ledger thread."""
import sqlite3
from sidequest.dungeon.persistence import DungeonStore
from sidequest.dungeon.region_graph.model import Expansion, RegionNode
from sidequest.dungeon.themes import ExpansionQuestTemplate
from sidequest.dungeon.expansion_quest import seed_expansion_quest


def _store():
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    s = DungeonStore(conn); s.ensure_schema(); return conn, s


def _exp():
    return Expansion(expansion_id=2,
        new_nodes=[RegionNode(id="exp002.r0", expansion_id=2, theme="bone_crypt", depth_score=40.0)],
        new_edges=[])


def test_seed_opens_one_expansion_quest_thread():
    conn, store = _store()
    tpl = ExpansionQuestTemplate(signature="reach_deep", title="The {theme} deepens",
                                 objective="Descend to the heart of the {theme}.")
    tid = seed_expansion_quest(campaign_seed=77, expansion=_exp(), manifests_by_region={},
        template=tpl, store=store, started_at_depth_score=40.0)
    conn.commit()
    threads = store.open_threads()
    assert len(threads) == 1
    t = threads[0]
    assert t.thread_id == tid
    assert t.kind == "quest"
    assert t.payload["scope"] == "expansion"
    assert t.payload["expansion_id"] == 2
    assert t.payload["objective"] == "Descend to the heart of the bone_crypt."


def test_seed_is_deterministic_thread_id():
    conn1, s1 = _store(); conn2, s2 = _store()
    tpl = ExpansionQuestTemplate(signature="reach_deep", title="t", objective="o")
    a = seed_expansion_quest(campaign_seed=77, expansion=_exp(), manifests_by_region={}, template=tpl, store=s1, started_at_depth_score=40.0)
    b = seed_expansion_quest(campaign_seed=77, expansion=_exp(), manifests_by_region={}, template=tpl, store=s2, started_at_depth_score=40.0)
    assert a == b
