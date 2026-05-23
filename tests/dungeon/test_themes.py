"""Unit tests for sidequest.dungeon.themes (schema + loader — Plan 4)."""

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from sidequest.dungeon.interiors import ALGORITHMS
from sidequest.dungeon.region_graph import DepthConfig
from sidequest.dungeon.themes import (
    Adjacency,
    CreatureEntry,
    DepthBand,
    DungeonTheme,
    InteriorSpec,
    LootEntry,
    NarratorFlavor,
    ThemePalette,
    ThemePaletteMissingError,
    load_theme_palette,
    theme_eligible_at_depth,
)


def test_interior_spec_accepts_every_real_algorithm():
    # WIRING: the schema validates against the REAL Plan-1 coordinator
    # registry, not a hard-coded copy.
    for algo in ALGORITHMS:
        spec = InteriorSpec(algorithm=algo, params={}, braid_ratio=0.0)
        assert spec.algorithm == algo


def test_interior_spec_rejects_unknown_algorithm():
    with pytest.raises(ValidationError, match="unknown interior algorithm"):
        InteriorSpec(algorithm="voronoi", params={}, braid_ratio=0.0)


@pytest.mark.parametrize("bad", [-0.01, 1.01, 2.0])
def test_interior_spec_braid_ratio_out_of_range_rejected(bad):
    with pytest.raises(ValidationError, match="braid_ratio"):
        InteriorSpec(algorithm="depthfirst", braid_ratio=bad)


def test_interior_spec_braid_ratio_bounds_inclusive():
    assert InteriorSpec(algorithm="depthfirst", braid_ratio=0.0).braid_ratio == 0.0
    assert InteriorSpec(algorithm="depthfirst", braid_ratio=1.0).braid_ratio == 1.0


def test_interior_spec_defaults():
    s = InteriorSpec(algorithm="cellular")
    assert s.params == {} and s.braid_ratio == 0.0


def test_interior_spec_extra_forbidden():
    with pytest.raises(ValidationError):
        InteriorSpec(algorithm="cellular", oops=1)  # type: ignore[call-arg]


def _theme(**over) -> DungeonTheme:
    base = dict(
        id="bone_crypt",
        display_name="The Bone Crypt",
        generator_class="structured",
        interior={"algorithm": "prim", "braid_ratio": 0.3},
        depth_band={"min": 30.0, "max": 120.0},
        narrator={
            "register": "grave",
            "flavor": "Dry air, stacked femurs, dust that remembers names.",
            "motifs": ["ossuary", "silence"],
        },
        adjacency={"prefers": ["winding_catacomb"], "avoids": ["drowned_cavern"]},
        creature_table=[{"ref": "bone_drake", "weight": 1.0}],
        loot_table=[{"ref": "grave_silver", "weight": 2.0}],
        set_pieces=[
            {
                "id": "false_floor",
                "name": "The False Floor",
                "telegraph": "Newer mortar rings hollow underfoot.",
                "outcome": "The slab drops onto upturned stakes.",
                "depth_band": {"min": 30.0, "max": 120.0},
                "slots": [{"name": "layout", "options": [{"value": "ten_foot_pit"}]}],
            }
        ],
    )
    base.update(over)
    return DungeonTheme.model_validate(base)


def test_dungeon_theme_minimal_valid():
    t = _theme()
    assert t.id == "bone_crypt"
    assert t.interior.algorithm == "prim"
    assert isinstance(t.depth_band, DepthBand)
    assert t.set_pieces[0].telegraph.startswith("Newer mortar")


@pytest.mark.parametrize(
    "gen_class, algo",
    [
        ("organic", "cellular"),
        ("labyrinthine", "depthfirst"),
        ("structured", "prim"),
        ("built", "roomcorridor"),
    ],
)
def test_generator_class_must_match_algorithm_family(gen_class, algo):
    # spec §5.2 mapping table is enforced as a hard invariant
    t = _theme(generator_class=gen_class, interior={"algorithm": algo})
    assert t.generator_class == gen_class


def test_generator_class_mismatch_rejected():
    with pytest.raises(ValidationError, match="generator_class .* does not match"):
        _theme(generator_class="built", interior={"algorithm": "cellular"})


def test_adjacency_same_id_in_prefers_and_avoids_rejected():
    with pytest.raises(ValidationError, match="both prefers and avoids"):
        Adjacency(prefers=["x"], avoids=["x"])


def test_adjacency_blank_avoids_entry_rejected():
    # "flooded clusters" (spec §6) -> a theme may prefer adjacency to itself
    Adjacency(prefers=["drowned_cavern"], avoids=[])  # validated at palette level
    with pytest.raises(ValidationError, match="cannot avoid itself"):
        # self-avoidance is only detectable with the owning id; the model
        # rejects the trivially-nonsensical empty-string form here
        Adjacency(prefers=[], avoids=[" "])


def test_narrator_flavor_rejects_blank_motif():
    with pytest.raises(ValidationError, match="motif cannot be blank"):
        NarratorFlavor(register="grave", flavor="x", motifs=["ok", "  "])


def test_narrator_flavor_requires_nonblank_register_and_flavor():
    with pytest.raises(ValidationError):
        NarratorFlavor(register=" ", flavor="x")
    with pytest.raises(ValidationError):
        NarratorFlavor(register="grave", flavor="  ")


def test_creature_and_loot_entries_require_positive_weight_and_ref():
    CreatureEntry(ref="bone_drake", weight=1.0)
    LootEntry(ref="grave_silver", weight=1.0)
    with pytest.raises(ValidationError):
        CreatureEntry(ref="", weight=1.0)
    with pytest.raises(ValidationError):
        LootEntry(ref="x", weight=0.0)


def test_theme_blank_id_rejected():
    with pytest.raises(ValidationError):
        _theme(id="  ")


_GOOD_A = """
id: drowned_cavern
display_name: The Drowned Cavern
generator_class: organic
interior:
  algorithm: cellular
  braid_ratio: 0.0
depth_band: {min: 0.0, max: 50.0}
narrator:
  register: grave
  flavor: Black water, no echo, the cold of deep stone.
  motifs: [flood, silence]
adjacency:
  prefers: [drowned_cavern]
  avoids: [bone_crypt]
creature_table:
  - {ref: blind_eel, weight: 1.0}
loot_table:
  - {ref: silt_pearl, weight: 1.0}
set_pieces:
  - id: siphon
    name: The Siphon
    telegraph: A steady suck of current pulls toward a black slot in the floor.
    outcome: The current takes the careless under the rock and does not give them back.
    depth_band: {min: 0.0, max: 50.0}
    save_or_die: {save: reflex, dc: 14}
    slots:
      - name: layout
        options: [{value: funnel_chamber, weight: 1.0}]
"""

_GOOD_B = """
id: bone_crypt
display_name: The Bone Crypt
generator_class: structured
interior: {algorithm: prim, braid_ratio: 0.3}
depth_band: {min: 30.0, max: 120.0}
narrator:
  register: grave
  flavor: Dust that remembers names.
  motifs: [ossuary]
adjacency:
  prefers: []
  avoids: [drowned_cavern]
set_pieces:
  - id: false_floor
    name: The False Floor
    telegraph: Newer mortar rings hollow underfoot.
    outcome: The slab drops onto upturned stakes.
"""


def _write(d: Path, name: str, body: str) -> None:
    (d / name).write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")


def test_load_palette_happy_path(tmp_path: Path):
    td = tmp_path / "themes"
    td.mkdir()
    _write(td, "drowned_cavern.yaml", _GOOD_A)
    _write(td, "bone_crypt.yaml", _GOOD_B)
    pal = load_theme_palette(tmp_path)
    assert isinstance(pal, ThemePalette)
    assert set(pal.themes) == {"drowned_cavern", "bone_crypt"}
    assert pal.get("drowned_cavern").interior.algorithm == "cellular"


def test_load_palette_missing_dir_raises(tmp_path: Path):
    with pytest.raises(ThemePaletteMissingError):
        load_theme_palette(tmp_path)


def test_load_palette_empty_dir_raises(tmp_path: Path):
    (tmp_path / "themes").mkdir()
    with pytest.raises(ValueError, match="no theme files"):
        load_theme_palette(tmp_path)


def test_load_palette_duplicate_id_raises(tmp_path: Path):
    td = tmp_path / "themes"
    td.mkdir()
    _write(td, "a.yaml", _GOOD_A)
    _write(td, "a_copy.yaml", _GOOD_A)  # same id inside
    with pytest.raises(ValueError, match="duplicate theme id"):
        load_theme_palette(tmp_path)


def test_load_palette_dangling_affinity_raises(tmp_path: Path):
    td = tmp_path / "themes"
    td.mkdir()
    _write(td, "drowned_cavern.yaml", _GOOD_A)  # avoids bone_crypt — absent
    with pytest.raises(ValueError, match="unknown theme id"):
        load_theme_palette(tmp_path)


def test_load_palette_self_avoidance_raises(tmp_path: Path):
    td = tmp_path / "themes"
    td.mkdir()
    bad = _GOOD_B.replace("avoids: [drowned_cavern]", "avoids: [bone_crypt]")
    _write(td, "bone_crypt.yaml", bad)
    with pytest.raises(ValueError, match="cannot avoid itself"):
        load_theme_palette(tmp_path)


def test_load_palette_schema_violation_is_loud(tmp_path: Path):
    td = tmp_path / "themes"
    td.mkdir()
    broken = _GOOD_A.replace("algorithm: cellular", "algorithm: voronoi")
    _write(td, "drowned_cavern.yaml", broken)
    with pytest.raises(ValueError, match="drowned_cavern.yaml"):
        load_theme_palette(tmp_path)


# ---------------------------------------------------------------------------
# CONTRACT-LOCK: depth_score eligibility cross-plan contract (Plan 4 Task 6)
# Proves bands are interpreted in RAW Plan-3 depth_score units, NOT level
# buckets.  Spec §5: depth_score is the authoritative gradient; "level" is
# never an authoritative key.
# ---------------------------------------------------------------------------


def test_eligibility_uses_raw_depth_score_not_level_buckets():
    shallow = _theme(id="s", depth_band={"min": 0.0, "max": 50.0})
    deep = _theme(id="d", depth_band={"min": 90.0, "max": None})
    # Plan 3: depth_per_hop=10 -> 3 hops == depth_score 30 (NOT "level 1")
    assert theme_eligible_at_depth(shallow, 0.0) is True
    assert theme_eligible_at_depth(shallow, 50.0) is True  # inclusive max
    assert theme_eligible_at_depth(shallow, 50.01) is False
    assert theme_eligible_at_depth(deep, 89.9) is False
    assert theme_eligible_at_depth(deep, 90.0) is True  # inclusive min
    assert theme_eligible_at_depth(deep, 100000.0) is True  # max=None


def test_themes_for_depth_is_sorted_and_filters(tmp_path: Path):
    td = tmp_path / "themes"
    td.mkdir()
    _write(td, "a.yaml", _GOOD_A)  # drowned_cavern  band 0..50
    _write(
        td,
        "b.yaml",
        _GOOD_B.replace("avoids: [drowned_cavern]", "avoids: []"),
    )  # bone_crypt band 30..120
    pal = load_theme_palette(tmp_path)
    at0 = [t.id for t in pal.themes_for_depth(0.0)]
    at40 = [t.id for t in pal.themes_for_depth(40.0)]
    at200 = [t.id for t in pal.themes_for_depth(200.0)]
    assert at0 == ["drowned_cavern"]
    assert at40 == ["bone_crypt", "drowned_cavern"]  # sorted by id
    assert at200 == []  # nothing eligible that deep


def test_depthconfig_scale_sanity_for_authoring():
    # Documents the authoring contract: bands are RAW depth_score units.
    # 9 ordinary hops at the Plan-3 default == depth_score 90.
    cfg = DepthConfig()
    assert cfg.depth_per_hop == 10.0
    assert 9 * cfg.depth_per_hop == 90.0
