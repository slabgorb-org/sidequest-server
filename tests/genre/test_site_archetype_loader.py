"""SiteArchetype model + generic-loader wiring — Track B, Task 10 (story 164-6).

RED: ``sidequest.genre.models.site_archetype`` does not exist yet, and
``GenrePack`` has no ``site_archetypes`` field — every test here fails
(ModuleNotFoundError, then AttributeError) until Task 10 lands.

Contract under test (plan 2026-07-08-mapping-track-b-site-system.md §Task 10,
consumed by Tasks 11/12):

  - ``SiteArchetype`` is a pydantic model with ``archetype_id``,
    ``interior_algorithm`` (validated ∈ ``ALGORITHMS`` — fail loud, no silent
    fallback), ``room_count_min``/``room_count_max`` (>=1),
    ``grid_width``/``grid_height`` (>=5), ``cell_scale_feet`` (>=1, default 5),
    ``room_vocabulary``/``feature_palette`` (list, default empty).
  - ``site_archetypes.yaml`` is an OPTIONAL genre-root file wired through the
    generic loader; a pack WITH the file gets a populated
    ``GenrePack.site_archetypes`` dict keyed by ``archetype_id``, a pack
    WITHOUT it gets an empty dict (additive — no behavior change).

Content-boundary: the loader test clones a REAL pack into ``tmp_path`` (the
dominant ``tests/genre/`` pattern) so we exercise the real ``load_genre_pack``,
and skips loudly when sidequest-content is not on disk.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from tests._helpers.genre_paths import find_pack_path

try:
    _CAVERNS_PACK_DIR: Path | None = find_pack_path("caverns_and_claudes")
except Exception:  # PackNotFound — sidequest-content not on disk
    _CAVERNS_PACK_DIR = None

_needs_content = pytest.mark.skipif(
    _CAVERNS_PACK_DIR is None or not _CAVERNS_PACK_DIR.is_dir(),
    reason="sidequest-content not on disk",
)

_TAVERN_YAML = [
    {
        "archetype_id": "tavern",
        "interior_algorithm": "roomcorridor",
        "room_count_min": 3,
        "room_count_max": 6,
        "grid_width": 15,
        "grid_height": 20,
        "cell_scale_feet": 5,
        "room_vocabulary": ["common room", "kitchen", "cellar"],
        "feature_palette": ["hearth", "bar", "stairs"],
    }
]


def _clone_pack(src: Path, dst: Path) -> Path:
    """Clone a real pack into tmp; rewrite genre_key so the loader's
    genre_key == dir-name invariant holds (mirrors tests/genre/test_loader.py)."""
    shutil.copytree(src, dst)
    lethality_yaml = dst / "lethality_policy.yaml"
    if lethality_yaml.exists():
        with lethality_yaml.open("r", encoding="utf-8") as f:
            policy = yaml.safe_load(f)
        policy["genre_key"] = dst.name
        with lethality_yaml.open("w", encoding="utf-8") as f:
            yaml.dump(policy, f, default_flow_style=False, sort_keys=False)
    return dst


# ---------------------------------------------------------------------------
# Model validation (pure — no content needed)
# ---------------------------------------------------------------------------


def test_site_archetype_validates_known_algorithm() -> None:
    """A well-formed archetype with a known interior algorithm loads."""
    from sidequest.genre.models.site_archetype import SiteArchetype

    a = SiteArchetype.model_validate(_TAVERN_YAML[0])
    assert a.archetype_id == "tavern"
    assert a.interior_algorithm == "roomcorridor"
    assert a.room_count_min == 3
    assert a.room_count_max == 6
    assert a.grid_width == 15
    assert a.grid_height == 20
    assert a.cell_scale_feet == 5
    assert a.room_vocabulary == ["common room", "kitchen", "cellar"]
    assert a.feature_palette == ["hearth", "bar", "stairs"]


def test_unknown_algorithm_fails_loud() -> None:
    """No Silent Fallbacks: an interior_algorithm not in ALGORITHMS is a loud
    ValueError at construction, not a silent default (rule: validated ctor)."""
    from sidequest.genre.models.site_archetype import SiteArchetype

    with pytest.raises(ValueError, match="interior_algorithm|nope|ALGORITHMS|algorithm"):
        SiteArchetype.model_validate(
            {
                "archetype_id": "x",
                "interior_algorithm": "nope",
                "room_count_min": 1,
                "room_count_max": 1,
                "grid_width": 5,
                "grid_height": 5,
                "cell_scale_feet": 5,
            }
        )


def test_cell_scale_defaults_to_five() -> None:
    """cell_scale_feet is optional and defaults to 5 (a tabletop 5-ft cell)."""
    from sidequest.genre.models.site_archetype import SiteArchetype

    a = SiteArchetype.model_validate(
        {
            "archetype_id": "closet",
            "interior_algorithm": "roomcorridor",
            "room_count_min": 1,
            "room_count_max": 2,
            "grid_width": 5,
            "grid_height": 5,
        }
    )
    assert a.cell_scale_feet == 5
    assert a.room_vocabulary == []
    assert a.feature_palette == []


def test_room_count_min_rejects_zero() -> None:
    """A site must have at least one room — room_count_min >= 1 (boundary)."""
    from sidequest.genre.models.site_archetype import SiteArchetype

    with pytest.raises(ValueError):
        SiteArchetype.model_validate(
            {
                "archetype_id": "empty",
                "interior_algorithm": "roomcorridor",
                "room_count_min": 0,
                "room_count_max": 3,
                "grid_width": 10,
                "grid_height": 10,
            }
        )


def test_grid_dims_reject_too_small() -> None:
    """A grid smaller than 5x5 cannot host a tactical interior — grid dims >= 5
    (boundary; a 4-wide grid must fail loud, not clamp silently)."""
    from sidequest.genre.models.site_archetype import SiteArchetype

    with pytest.raises(ValueError):
        SiteArchetype.model_validate(
            {
                "archetype_id": "sliver",
                "interior_algorithm": "roomcorridor",
                "room_count_min": 1,
                "room_count_max": 2,
                "grid_width": 4,
                "grid_height": 10,
            }
        )


# ---------------------------------------------------------------------------
# Loader wiring (needs a real pack skeleton to clone)
# ---------------------------------------------------------------------------


@_needs_content
def test_loader_populates_site_archetypes(tmp_path: Path) -> None:
    """A pack carrying site_archetypes.yaml lands a populated
    GenrePack.site_archetypes dict keyed by archetype_id."""
    from sidequest.genre.loader import load_genre_pack

    pack_dir = _clone_pack(_CAVERNS_PACK_DIR, tmp_path / "caverns_with_sites")
    with (pack_dir / "site_archetypes.yaml").open("w", encoding="utf-8") as f:
        yaml.dump(_TAVERN_YAML, f, sort_keys=False)

    pack = load_genre_pack(pack_dir)
    assert "tavern" in pack.site_archetypes
    tavern = pack.site_archetypes["tavern"]
    assert tavern.interior_algorithm == "roomcorridor"
    assert tavern.grid_width == 15


@_needs_content
def test_loader_omits_field_when_file_absent(tmp_path: Path) -> None:
    """Additive contract: a pack WITHOUT site_archetypes.yaml loads with an
    EMPTY site_archetypes dict — no crash, no behavior change for the other
    ten packs that never author one."""
    from sidequest.genre.loader import load_genre_pack

    pack_dir = _clone_pack(_CAVERNS_PACK_DIR, tmp_path / "caverns_no_sites")
    # deliberately do NOT write site_archetypes.yaml
    pack = load_genre_pack(pack_dir)
    assert pack.site_archetypes == {}
