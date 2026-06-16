"""Genre pack loader exposes chassis_classes when chassis_classes.yaml exists.

Lazy import path is the contract: genres without the file get None;
genres with the file get a populated ChassisClassesConfig.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.chassis import ChassisClassesConfig

REPO_ROOT = Path(__file__).resolve().parents[3]
SPACE_OPERA = REPO_ROOT / "sidequest-content" / "genre_packs" / "space_opera"
CAVERNS = REPO_ROOT / "sidequest-content" / "genre_packs" / "caverns_and_claudes"


def test_space_opera_pack_exposes_chassis_classes() -> None:
    """space_opera has authored chassis_classes.yaml — pack should expose it."""
    if not SPACE_OPERA.exists():
        pytest.skip("space_opera content pack not present")
    pack = load_genre_pack(SPACE_OPERA)
    assert pack.chassis_classes is not None
    assert isinstance(pack.chassis_classes, ChassisClassesConfig)
    ids = {cls.id for cls in pack.chassis_classes.classes}
    assert "voidborn_freighter" in ids


def test_caverns_pack_has_no_chassis_classes() -> None:
    """caverns_and_claudes has no chassis_classes.yaml — pack.chassis_classes is None."""
    if not CAVERNS.exists():
        pytest.skip("caverns_and_claudes content pack not present")
    pack = load_genre_pack(CAVERNS)
    assert pack.chassis_classes is None
