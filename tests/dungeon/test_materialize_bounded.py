"""Cookbook-free bounded materialization (ADR-157, story 164-10).

Unit coverage that needs no DB: the synthetic archetype→palette build and the
room-identity helper. Behavioral (committed-graph) coverage lives in
tests/dungeon/test_bounded_site.py against a real PgDungeonRepository.
"""

from __future__ import annotations

from typing import Any


def _tavern_archetype(**over: Any) -> Any:
    from sidequest.genre.models.site_archetype import SiteArchetype

    base = dict(
        archetype_id="tavern",
        interior_algorithm="roomcorridor",
        room_count_min=3,
        room_count_max=6,
        grid_width=15,
        grid_height=20,
        cell_scale_feet=5,
        room_vocabulary=["common room", "cellar", "kitchen", "private booth"],
        feature_palette=["hearth", "long bar", "ale barrels"],
    )
    base.update(over)
    return SiteArchetype(**base)


def test_build_bounded_palette_single_theme_matches_algorithm() -> None:
    from sidequest.dungeon.materializer import build_bounded_palette

    palette = build_bounded_palette(_tavern_archetype())
    assert list(palette.themes) == ["bounded_tavern"]
    theme = palette.themes["bounded_tavern"]
    assert theme.interior.algorithm == "roomcorridor"
    assert theme.generator_class == "built"  # roomcorridor's class
    # Eligible at every depth (one theme covers the whole bounded site).
    assert palette.themes_for_depth(0.0) == [theme]
    assert palette.themes_for_depth(99.0) == [theme]
    # Cookbook-free: no creatures, no set-pieces.
    assert theme.creature_table == []
    assert theme.set_pieces == []


def test_build_bounded_palette_maps_each_algorithm_to_its_class() -> None:
    from sidequest.dungeon.materializer import build_bounded_palette

    cases = {
        "cellular": "organic",
        "depthfirst": "labyrinthine",
        "prim": "structured",
        "roomcorridor": "built",
    }
    for algorithm, generator_class in cases.items():
        palette = build_bounded_palette(
            _tavern_archetype(archetype_id=f"a_{algorithm}", interior_algorithm=algorithm)
        )
        theme = next(iter(palette.themes.values()))
        assert theme.generator_class == generator_class
        assert theme.interior.algorithm == algorithm


def test_bounded_room_identities_are_deterministic_and_from_vocabulary() -> None:
    from sidequest.dungeon.materializer import _bounded_room_identities

    arch = _tavern_archetype()
    ids = ["gilded_boar:r1", "gilded_boar:r2", "gilded_boar:r3"]
    a = _bounded_room_identities(arch, campaign_seed=777, site_id="gilded_boar", region_ids=ids)
    b = _bounded_room_identities(arch, campaign_seed=777, site_id="gilded_boar", region_ids=ids)
    assert a == b  # deterministic
    assert set(a) == set(ids)
    for rid in ids:
        assert a[rid]["region_id"] == rid
        assert a[rid]["label"] in arch.room_vocabulary
        assert set(a[rid]["features"]).issubset(set(arch.feature_palette))
    # A different seed can change assignments (no constant fallback).
    c = _bounded_room_identities(arch, campaign_seed=778, site_id="gilded_boar", region_ids=ids)
    assert c != a or len(arch.room_vocabulary) == 1


def test_bounded_room_identities_empty_vocabulary_is_no_op() -> None:
    from sidequest.dungeon.materializer import _bounded_room_identities

    arch = _tavern_archetype(room_vocabulary=[], feature_palette=[])
    out = _bounded_room_identities(arch, campaign_seed=1, site_id="s", region_ids=["s:r1"])
    assert out == {}
