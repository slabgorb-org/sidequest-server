"""Faction/zone-scoped content eligibility — the shared predicate (epic-157).

Multi-region worlds leak content across zones (the wry_whimsy/gulliver bleed: a
4th-voyage Yahoo on the 1st-voyage Lilliput shore). The fix, per the design spec
``docs/superpowers/specs/2026-06-20-faction-zone-content-eligibility-design.md``
(ADR-059 amendment): derive a region's zone from its authored
``Region.controlled_by`` faction, tag pooled/home-less content with the
faction(s) it belongs to, and gate every draw/inject seam through ONE predicate.

This module is that predicate plus its two helpers. Three rules from the design:

1. **Reuse the authored key.** The eligibility axis is ``Region.controlled_by``
   — already authored exactly where the bleed lives (gulliver, oz, wonderland,
   the_circuit) and absent in the 11 single-zone worlds (which stay unaffected).
2. **Runtime is permissive; strictness is the validator's job (story 157-7).**
   The only exclusion is *tagged-but-wrong-zone*. Untagged content, an
   unresolvable region, and unzoned worlds all stay eligible — the engine fails
   toward *showing* content, never a silent empty scene.
3. **Split-party safe.** :func:`active_factions` never raises on an unresolvable
   region and never injects ``None`` into the active set; a split party yields
   the UNION of every seated PC's zone.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sidequest.game.session import GameSnapshot
    from sidequest.genre.models.world import CartographyConfig


def world_is_zoned(cartography: CartographyConfig | None) -> bool:
    """Whether this world scopes content by faction.

    True iff ANY region declares a ``controlled_by`` faction. The 11 worlds with
    no ``controlled_by`` → ``False`` → every predicate short-circuits to eligible
    (zero behavior change). Computed once per loaded world by callers and cached.
    """
    if cartography is None:
        return False
    return any(region.controlled_by for region in cartography.regions.values())


def is_eligible(content_factions: Iterable[str], active: Iterable[str], *, zoned: bool) -> bool:
    """The one eligibility predicate (runtime is permissive).

    ``content_factions`` is the content item's faction tags; ``active`` is the
    party's currently-active faction set (see :func:`active_factions`).

    Returns ``True`` (eligible) unless the content is tagged for a faction set
    that is disjoint from the active set — that *tagged-but-wrong-zone* case is
    the only exclusion (the bug fix). Untagged content, an empty active set
    (region unresolvable), the ``"*"`` world-global sentinel, and an unzoned
    world are all permissive: the load validator (story 157-7), not this
    predicate, guarantees a zoned world ships no untagged pooled content.
    """
    if not zoned:
        return True
    active_set = set(active)
    if not active_set:
        return True
    content = set(content_factions)
    if "*" in content:
        return True
    if not content:
        return True
    return bool(content & active_set)


def cartography_for(snapshot: GameSnapshot, pack: Any) -> CartographyConfig | None:
    """Resolve the loaded world's cartography from ``pack`` + ``snapshot``.

    Uses ``pack.worlds[snapshot.world_slug].cartography`` — the same accessor
    convention as ``pregen._seed_authored_npcs`` (``pack.worlds[slug]`` →
    ``World.cartography``). Returns ``None`` (→ resolver yields ∅ → permissive)
    when the pack/world/cartography is absent rather than raising, so a pre-bind
    or stub session never crashes the draw/inject seam.
    """
    world_slug = getattr(snapshot, "world_slug", None)
    worlds = getattr(pack, "worlds", None)
    if not world_slug or worlds is None:
        return None
    world = worlds.get(world_slug)
    if world is None:
        return None
    return getattr(world, "cartography", None)


def _faction_for_region(cartography: CartographyConfig | None, region_id: str | None) -> str | None:
    """The ``controlled_by`` faction of ``region_id``, or ``None`` (unowned/absent)."""
    if cartography is None or not region_id:
        return None
    region = cartography.regions.get(region_id)
    if region is None:
        return None
    return region.controlled_by


def active_factions(snapshot: GameSnapshot, pack: Any, *, perspective: str | None = None) -> set[str]:
    """The party's currently-active faction set (split-party safe; never raises).

    - ``perspective`` given (per-perspective seams: creature/NPC injection) →
      ``{controlled_by of that PC's region}`` or ``∅`` if unresolvable.
    - ``perspective`` omitted (party-global seams: trope/seed ticks) + a
      consensus region → ``{controlled_by of the consensus region}``.
    - ``perspective`` omitted + a split party (no consensus) → the UNION of
      every seated PC's region ``controlled_by``.
    - No region resolvable anywhere (pre-bind / malformed turn / unowned hub) →
      ``∅``.

    Region resolution goes through ``snapshot.region_for`` (the canonical per-PC
    graph-region truth), NOT the free-text ``current_location`` scene string. An
    unowned region (``controlled_by is None``) contributes nothing — ``None`` is
    never put in the set.
    """
    cartography = cartography_for(snapshot, pack)
    if cartography is None:
        return set()

    if perspective is not None:
        faction = _faction_for_region(cartography, snapshot.region_for(perspective=perspective))
        return {faction} if faction else set()

    consensus = snapshot.region_for()
    if consensus is not None:
        faction = _faction_for_region(cartography, consensus)
        return {faction} if faction else set()

    # Split party (or no consensus): union every seated PC's zone — eligible if
    # the content matches ANY seated PC's faction.
    factions: set[str] = set()
    for name in snapshot.player_seats.values():
        if not name:
            continue
        faction = _faction_for_region(cartography, snapshot.pc_regions.get(name))
        if faction:
            factions.add(faction)
    return factions
