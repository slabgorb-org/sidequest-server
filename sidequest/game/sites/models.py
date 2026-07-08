"""Resolved runtime descriptor for a declared site.

``SiteDecl`` (the authored pydantic model) lives in
``sidequest.genre.models.world`` so ``CartographyConfig`` can type its
``sites`` field without a ``game -> genre`` import cycle; it is re-exported
here for callers that think in terms of ``game.sites``.
"""

from __future__ import annotations

from dataclasses import dataclass

from sidequest.game.sites.namespacing import site_entrance_id
from sidequest.genre.models.world import SiteDecl, SiteExtent

__all__ = ["SiteDecl", "SiteDescriptor", "SiteExtent"]


@dataclass(frozen=True)
class SiteDescriptor:
    """Resolved, runtime-facing view of a declared site.

    Frozen: a resolved descriptor is a read-only snapshot handed to the
    movement/seam/map-emit layers — mutating it would desync the registry.
    """

    site_id: str
    name: str
    archetype: str
    attached_to: str
    extent: SiteExtent

    @property
    def entrance_node_id(self) -> str:
        return site_entrance_id(self.site_id)

    @classmethod
    def from_decl(cls, decl: SiteDecl) -> SiteDescriptor:
        return cls(
            site_id=decl.site_id,
            name=decl.name,
            archetype=decl.archetype,
            attached_to=decl.attached_to,
            extent=decl.extent,
        )
