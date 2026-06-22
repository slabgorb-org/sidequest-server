"""Procedural megadungeon generation (spec: Beneath Sünden)."""

from sidequest.dungeon.materializer import MaterializationRequest, materialize
from sidequest.dungeon.seed_bootstrap import is_procedural_region_id

__all__ = [
    "MaterializationRequest",
    "is_procedural_region_id",
    "materialize",
]
