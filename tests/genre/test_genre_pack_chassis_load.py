"""Loader exposes chassis_classes at the WORLD tier (epic 94).

Genre/world boundary correction (supersedes ADR-120 "mechanics-in-genre"):
chassis classes are a world-tier CAST/CATALOG surface — the cast of rigs a
world ships — NOT a genre mechanic. The genre tier is the rulebook only.

Contract:
* Worlds with ``chassis_classes.yaml`` get a populated ``World.chassis_classes``.
* Worlds without the file get ``None``.
* The genre-tier ``GenrePack.chassis_classes`` is ``None`` for migrated packs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.chassis import ChassisClassesConfig

REPO_ROOT = Path(__file__).resolve().parents[3]
SPACE_OPERA = REPO_ROOT / "sidequest-content" / "genre_packs" / "space_opera"
CAVERNS = REPO_ROOT / "sidequest-content" / "genre_packs" / "caverns_and_claudes"


def test_coyote_star_world_exposes_chassis_classes() -> None:
    """coyote_star authored chassis_classes.yaml at the world tier — the bound
    World should expose it (epic 94 moved it down from the space_opera genre)."""
    if not SPACE_OPERA.exists():
        pytest.skip("space_opera content pack not present")
    pack = load_genre_pack(SPACE_OPERA)

    # The genre tier no longer owns chassis_classes — it's a world surface now.
    assert pack.chassis_classes is None

    world = pack.worlds.get("coyote_star")
    assert world is not None, "coyote_star world missing from space_opera pack"
    assert world.chassis_classes is not None
    assert isinstance(world.chassis_classes, ChassisClassesConfig)
    ids = {cls.id for cls in world.chassis_classes.classes}
    assert "voidborn_freighter" in ids


def test_caverns_world_has_no_chassis_classes() -> None:
    """caverns_and_claudes worlds author no chassis_classes.yaml — every world's
    chassis_classes is None, and so is the genre tier's."""
    if not CAVERNS.exists():
        pytest.skip("caverns_and_claudes content pack not present")
    pack = load_genre_pack(CAVERNS)
    assert pack.chassis_classes is None
    for world in pack.worlds.values():
        assert world.chassis_classes is None
