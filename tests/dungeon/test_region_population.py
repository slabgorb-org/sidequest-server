"""Tests for Task 2: CuratedCreature.threat_level derived from CR band.

Keith ruling 2026-06-22: derive a 1-4 B/X threat tier from the region's CR
band — ordinal of the band in affinities.cr_bands, clamped to [1, 4].
Big-bad gets region tier + 1 (capped at 4). ADR-114: no raw cr on
CuratedCreature.
"""
from pathlib import Path

import pytest

from sidequest.dungeon.materializer import CuratedCreature, CurationError, _threat_from_band
from sidequest.game.cookbook.loader import load_cookbook

# Discovery mirrors test_materializer.py: parents[3] is the orchestrator root.
_BENEATH_SUNDEN_WORLD = (
    Path(__file__).resolve().parents[3]
    / "sidequest-content/genre_packs/caverns_and_claudes/worlds/beneath_sunden"
)


def _bundle():
    """Load the real beneath_sunden cookbook — established pattern from
    test_materializer.py::_real_cookbook_bundle(). No Postgres needed."""
    return load_cookbook(_BENEATH_SUNDEN_WORLD)


def test_threat_from_band_shallow_is_tier_one():
    bundle = _bundle()
    shallow = bundle.affinities.cr_bands[0].id
    assert _threat_from_band(bundle, shallow) == 1


def test_threat_from_band_clamps_to_four():
    bundle = _bundle()
    deepest = bundle.affinities.cr_bands[-1].id
    assert _threat_from_band(bundle, deepest) == min(4, len(bundle.affinities.cr_bands))


def test_threat_from_band_unknown_raises():
    bundle = _bundle()
    with pytest.raises(CurationError, match="cr_band"):
        _threat_from_band(bundle, "nonexistent_band_xyz")


def test_curated_creature_has_threat_level_field():
    from sidequest.game.creature_core import hp_pool_from_hp

    c = CuratedCreature(
        name="x",
        creature_type="t",
        telegraph="g",
        hp=hp_pool_from_hp(4),
        threat_level=2,
    )
    assert c.threat_level == 2
