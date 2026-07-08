"""SiteRegistry — indexes a world's authored sites for the movement/seam and
map-emit layers. Built once per dispatch from the active cartography.

Pure/additive (Track B, Task 1): a world with no ``sites:`` yields an inert
registry that answers every query with "no site". Consumed by the movement
and map-emit cutovers in later stories (164-2/3/4); this module lands the
model + index only, with no dispatch-path wiring.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sidequest.game.sites.models import SiteDescriptor
from sidequest.game.sites.namespacing import site_id_of

if TYPE_CHECKING:
    from sidequest.genre.models.world import CartographyConfig


class SiteRegistry:
    """Read-only index of ``SiteDescriptor`` by owning node and by id."""

    def __init__(self, sites: list[SiteDescriptor], adjacency: dict[str, list[str]]) -> None:
        self._sites = sites
        self._by_id = {s.site_id: s for s in sites}
        self._by_owner: dict[str, list[SiteDescriptor]] = {}
        for s in sites:
            self._by_owner.setdefault(s.attached_to, []).append(s)
        self._adjacency = adjacency

    @classmethod
    def from_cartography(cls, cart: CartographyConfig | None) -> SiteRegistry:
        """Build a registry from a world's cartography (or an inert one for
        ``None`` / an empty ``sites`` list)."""
        if cart is None:
            return cls([], {})
        sites = [SiteDescriptor.from_decl(d) for d in getattr(cart, "sites", []) or []]
        adjacency = {
            rid: list(getattr(region, "adjacent", []) or [])
            for rid, region in (getattr(cart, "regions", {}) or {}).items()
        }
        return cls(sites, adjacency)

    def by_id(self, site_id: str) -> SiteDescriptor | None:
        return self._by_id.get(site_id)

    def sites_for_node(self, region_id: str) -> list[SiteDescriptor]:
        """Sites the PC can enter from ``region_id``: those OWNED by this node
        plus those owned by an ADJACENT node (the "down the rope at the camp"
        one-action reach). Owner-first, de-duplicated, order-stable."""
        seen: set[str] = set()
        out: list[SiteDescriptor] = []
        for s in self._by_owner.get(region_id, []):
            if s.site_id not in seen:
                seen.add(s.site_id)
                out.append(s)
        for adj in self._adjacency.get(region_id, []):
            for s in self._by_owner.get(adj, []):
                if s.site_id not in seen:
                    seen.add(s.site_id)
                    out.append(s)
        return out

    def resolve_descriptor(
        self, region_id: str, descriptor: str
    ) -> tuple[SiteDescriptor | None, bool]:
        """Match a free-text descriptor against the node's enterable sites.

        Returns ``(site, ambiguous)``. A NAMED descriptor disambiguates:
        substring match on name or id (either direction). A sole enterable
        site with an empty descriptor still resolves (there is only one way
        in). Ambiguous only when a descriptor matches >1 enterable site (or an
        empty descriptor faces >1 candidate)."""
        candidates = self.sites_for_node(region_id)
        if not candidates:
            return None, False
        desc = (descriptor or "").strip().lower()
        if not desc:
            return (candidates[0], False) if len(candidates) == 1 else (None, True)
        matches = [
            s
            for s in candidates
            if desc in s.name.lower() or desc in s.site_id.lower() or s.name.lower() in desc
        ]
        if len(matches) == 1:
            return matches[0], False
        if len(matches) > 1:
            return None, True
        return None, False

    def site_owning_node(self, node_id: str) -> SiteDescriptor | None:
        """The site whose namespace a graph node belongs to (for exit)."""
        sid = site_id_of(node_id)
        return self._by_id.get(sid) if sid else None
