"""Authored SITE crossings and the site registry (Track B, Task 1).

A ``SiteRegistry`` is built from a world's authored ``CartographyConfig.sites``
and indexes each site by owning node and by id, resolving free-text entry
descriptors and mapping site-namespaced node ids back to their owning site.
"""

from sidequest.game.sites.models import SiteDecl, SiteDescriptor, SiteExtent
from sidequest.game.sites.namespacing import (
    is_site_node_id,
    site_entrance_id,
    site_id_of,
)
from sidequest.game.sites.registry import SiteRegistry

__all__ = [
    "SiteDecl",
    "SiteDescriptor",
    "SiteExtent",
    "SiteRegistry",
    "is_site_node_id",
    "site_entrance_id",
    "site_id_of",
]
