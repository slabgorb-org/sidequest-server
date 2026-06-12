"""Static→procedural seam crossings (Story 105-2 / spec 2026-06-12)."""

from sidequest.game.seams.base import (
    SeamCrossingError,
    SeamCrossingResult,
    UnknownSeamKindError,
)
from sidequest.game.seams.registry import get_seam_resolver, seam_route_for

__all__ = [
    "SeamCrossingError",
    "SeamCrossingResult",
    "UnknownSeamKindError",
    "get_seam_resolver",
    "seam_route_for",
]
