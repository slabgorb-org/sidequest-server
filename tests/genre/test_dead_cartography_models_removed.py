"""Weed-whack rider (spec §2): the dead world-graph models are deleted.

Reflection-based tripwire (CLAUDE.md sanctioned exception) — interrogates
runtime types/fields, not source text. Guards the story-163-2 deletion of the
coordinate-free cartography graph models that had no non-test consumers, while
proving the weed-whack did NOT over-reach into the surviving symbols.
"""

import sidequest.genre.models as models
from sidequest.genre.models.world import CartographyConfig

DEAD_GRAPH_MODELS = ("Terrain", "WorldGraphNode", "GraphEdge", "SubGraph", "WorldGraph")


def test_dead_graph_models_are_gone() -> None:
    for name in DEAD_GRAPH_MODELS:
        assert not hasattr(models, name), f"{name} should be deleted (dead cartography model)"


def test_cartography_config_has_no_graph_fields() -> None:
    fields = set(CartographyConfig.model_fields)
    assert "world_graph" not in fields
    assert "sub_graphs" not in fields
    # The graph of record survives:
    assert "regions" in fields and "routes" in fields


def test_terrain_scar_is_preserved() -> None:
    """AC5: TerrainScar (a DIFFERENT symbol, from legends.py) must NOT be
    swept up by the weed-whack. A grep-delete on 'Terrain' would nuke it — this
    guards against that over-reach.
    """
    assert hasattr(models, "TerrainScar"), (
        "TerrainScar must survive — it is a distinct legends.py symbol, "
        "not part of the dead cartography graph"
    )
    from sidequest.genre.models.legends import TerrainScar

    assert TerrainScar is models.TerrainScar
