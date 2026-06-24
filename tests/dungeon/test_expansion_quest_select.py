from sidequest.dungeon.expansion_quest import select_signature
from sidequest.dungeon.region_graph.model import Expansion, RegionNode
from sidequest.dungeon.themes import ExpansionQuestTemplate
from sidequest.game.cookbook.models import RegionContentManifest


def _node(rid, depth, theme="bone_crypt"):
    return RegionNode(id=rid, expansion_id=1, theme=theme, depth_score=depth)


def _manifest(big_bad):
    return RegionContentManifest(race="undead", cr_band="mid", size_budget={},
        wandering_table=[], loot_table=[], special_rooms=[], big_bad=big_bad)


def _exp():
    return Expansion(expansion_id=1,
        new_nodes=[_node("exp001.r0", 10.0), _node("exp001.r1", 30.0)], new_edges=[])


def test_big_bad_binds_deepest_region_with_big_bad():
    tpl = ExpansionQuestTemplate(signature="big_bad", title="The {theme} Stirs",
                                 objective="End the {big_bad}.")
    b = select_signature(expansion=_exp(),
        manifests_by_region={"exp001.r0": _manifest(None),
                             "exp001.r1": _manifest({"name": "Bone Tyrant"})},
        template=tpl)
    assert b.kind == "big_bad"
    assert b.ref_id == "Bone Tyrant"
    assert b.anchor_region == "exp001.r1"
    assert b.objective == "End the Bone Tyrant."
    assert b.title == "The bone_crypt Stirs"
    assert b.degraded is False


def test_big_bad_degrades_to_reach_deep_when_absent():
    tpl = ExpansionQuestTemplate(signature="big_bad", title="t", objective="o")
    b = select_signature(expansion=_exp(),
        manifests_by_region={"exp001.r0": _manifest(None), "exp001.r1": _manifest(None)},
        template=tpl)
    assert b.kind == "reach_deep"
    assert b.anchor_region == "exp001.r1"   # deepest
    assert b.degraded is True


def test_reach_deep_binds_deepest_region():
    tpl = ExpansionQuestTemplate(signature="reach_deep", title="t", objective="reach the deep")
    b = select_signature(expansion=_exp(), manifests_by_region={}, template=tpl)
    assert b.kind == "reach_deep"
    assert b.ref_id == "exp001.r1"
    assert b.degraded is False
