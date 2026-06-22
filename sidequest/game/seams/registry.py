"""Seam-kind registry — mirrors sidequest/game/ruleset/registry.py."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from sidequest.game.seams.base import SeamCrossingResult, UnknownSeamKindError
from sidequest.game.seams.deep_descent import resolve_deep_descent

if TYPE_CHECKING:
    from sidequest.genre.models.world import CartographyConfig, Route

SeamResolver = Callable[..., SeamCrossingResult]

# NOTE: the intent-router prompt nudge (sidequest/agents/intent_router.py,
# movement section) hardcodes deep_descent's semantics — a "seam" exit goes
# DOWN, direction "deeper". A future seam kind with different geometry
# (ascent, lateral crossing) must update that nudge alongside this registry.
_REGISTRY: dict[str, SeamResolver] = {
    "deep_descent": resolve_deep_descent,
}


def get_seam_resolver(kind: str) -> SeamResolver:
    """Resolve a registered seam kind. Fails loud — never a default."""
    resolver = _REGISTRY.get(kind)
    if resolver is None:
        known = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise UnknownSeamKindError(f"Unknown seam kind {kind!r}; registered kinds: {known}")
    return resolver


def seam_route_for(cartography: CartographyConfig | None, region_id: str) -> Route | None:
    """The seam route owned by ``region_id``, or None.

    Uses ``getattr`` for the ``routes`` attribute so duck-typed test doubles
    that supply only ``navigation_mode`` (no ``routes`` field) do not raise —
    they simply have no seam routes, which is correct for that fixture shape.
    """
    if cartography is None:
        return None
    for route in getattr(cartography, "routes", ()):
        if route.from_id == region_id and (route.to_id or "") in _REGISTRY:
            return route
    return None


def seam_route_via_adjacency(cartography: CartographyConfig | None, region_id: str) -> Route | None:
    """The seam route owned by a region ADJACENT to ``region_id``, or None.

    The companion to :func:`seam_route_for` for the one-step-from-the-seam
    surface case. A PC on a surface region that does not itself own a seam, but
    sits directly adjacent to the region that does, can descend in a single
    deliberate action — the rope and winch are at the camp's lip, not a separate
    journey (beneath_sünden: ``ropefoot`` is adjacent to ``the_dropmouth``, which
    owns the ``deep_descent`` seam; the player who says "down the rope" at the
    camp expects to descend, not to first walk to the shaft mouth).

    Returns the UNIQUE adjacent owner's route, or ``None`` when no adjacent
    region owns a seam OR more than one does. The ambiguous case returns ``None``
    so the caller does NOT guess which descent was meant (No Silent Fallbacks);
    a multi-seam surface ring is a documented follow-up, mirroring the ambiguity
    rule in :func:`surface_owner_for_entrance`.
    """
    if cartography is None:
        return None
    region = getattr(cartography, "regions", {}).get(region_id)
    if region is None:
        return None
    found: list[Route] = []
    for adj_id in getattr(region, "adjacent", ()) or ():
        route = seam_route_for(cartography, adj_id)
        if route is not None:
            found.append(route)
    if len(found) != 1:
        return None
    return found[0]


def surface_owner_for_entrance(cartography: CartographyConfig | None) -> Route | None:
    """The seam route a PC at the dungeon entrance ascends back along (Story 105-3).

    The reverse of ``seam_route_for``: instead of "which route does this surface
    region own", it answers "which surface region owns the deep crossing" — the
    route whose ``to_id`` is a registered seam kind. Its ``from_id`` is the
    surface cartography region the entrance node returns to.

    Returns None when no route owns a crossing OR the owner is ambiguous (more
    than one distinct ``from_id`` among registered-kind routes) — in the
    ambiguous case the caller does NOT invent a surface (No Silent Fallbacks);
    multi-descent worlds are a documented follow-up.
    """
    if cartography is None:
        return None
    owners = [r for r in getattr(cartography, "routes", ()) if (r.to_id or "") in _REGISTRY]
    if len({r.from_id for r in owners}) != 1:
        return None
    return owners[0]
