"""Site node-id namespacing. Site node ids are ``{site_id}:{suffix}``.

Namespacing is what stops two sites in one session from colliding on the
global ``exp001.r0`` procedural ids — every site's nodes live under its own
``{site_id}:`` prefix (``gilded_boar:entrance``, ``frontier:exp003.r1``).
"""

from __future__ import annotations

import re

_SITE_NODE_RE = re.compile(r"^(?P<site>[a-z0-9_]+):(?P<suffix>[a-z0-9_.]+)$")


def site_entrance_id(site_id: str) -> str:
    """The per-site entrance anchor (replaces the global ENTRANCE_ID)."""
    return f"{site_id}:entrance"


def is_site_node_id(node_id: str) -> bool:
    """True iff ``node_id`` is site-namespaced (``gilded_boar:r2``)."""
    return bool(node_id) and _SITE_NODE_RE.match(node_id) is not None


def site_id_of(node_id: str) -> str | None:
    """The owning site id of a namespaced node, or None for a bare id."""
    m = _SITE_NODE_RE.match(node_id or "")
    return m.group("site") if m else None
