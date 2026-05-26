"""Region initialization on chargen confirmation — Story 37-31.

Populates ``snap.current_region`` and seeds ``snap.discovered_regions``
from the world's ``cartography.starting_region`` so a player's region is
load-bearing from turn 1. Runs for both ``region`` and ``room_graph``
navigation modes — room-graph worlds still have a canonical region
label (Grimvault → Ashgate Square) that downstream consumers can surface
alongside the room-level position.

No silent fallback per project principle: a world that declares no
``starting_region`` — or one that does not match any declared region
— is a pack authoring bug. :func:`init_region_location` raises
:class:`RegionInitError`; the wired call site logs at error level,
emits an OTEL ``region.init_failed`` event, and continues so chargen
never hard-fails mid-commit. Mirrors the :class:`RoomGraphInitError`
handling pattern.
"""

from __future__ import annotations

from sidequest.game.session import GameSnapshot
from sidequest.genre.models.world import CartographyConfig


class RegionInitError(Exception):
    """Raised when :func:`init_region_location` cannot resolve a starting region."""


def init_region_location(snap: GameSnapshot, cartography: CartographyConfig) -> str:
    """Set ``snap.current_region`` from ``cartography.starting_region``.

    Mutates ``snap`` in place:

    - ``snap.current_region`` = ``cartography.starting_region``
    - ``snap.discovered_regions`` gains that region id (dedup-append,
      preserving existing ordering for save compatibility).

    Returns the chosen region id so the caller can emit OTEL without
    re-reading ``snap.current_region``.

    Raises :class:`RegionInitError` when the starting region is blank
    or does not match a key in ``cartography.regions`` (when regions
    are declared). An empty ``regions`` dict is legacy flavor-only
    cartography — accept ``starting_region`` as authoritative so those
    packs still initialize current_region rather than hard-failing.
    """
    starting = cartography.starting_region.strip()
    if not starting:
        raise RegionInitError(
            "cartography.starting_region is blank — world must declare an opening region"
        )

    if cartography.regions and starting not in cartography.regions:
        raise RegionInitError(
            f"starting_region '{starting}' is not a declared region "
            f"(declared: {sorted(cartography.regions)})"
        )

    snap.current_region = starting
    if starting not in snap.discovered_regions:
        snap.discovered_regions.append(starting)
    # Movement subsystem §Q0: seed seated PCs' per-PC region from the turn-1
    # starting region so region_for(perspective=pc) resolves by movement time
    # (no current_region fallback).
    snap.seed_pc_regions(starting)
    return starting


__all__ = ["RegionInitError", "init_region_location"]
