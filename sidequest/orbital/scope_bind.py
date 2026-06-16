"""Region -> orbital-scope binding errors (Story 95-1).

The per-location orrery follows the party's system: when the party's
cartography region changes, the chart re-centers on the matching star body.
The join is an *identity* join — a star body whose id equals the region id
(region ``yula`` -> star body ``yula``), guaranteed by how perseus_cloud's
``orbits.yaml`` is authored (content#383).

The bind mechanism lives on ``Session.bind_region_scope``; this module owns
only the fail-loud error so the orbital surface (not the server tier) is the
home of the type, per the RED contract.
"""

from __future__ import annotations


class RegionScopeBindError(RuntimeError):
    """Raised when a region required to seed the chart has no star body.

    Bind-on-init must fail loud (No Silent Fallbacks): a blank/foreign
    ``starting_region`` cannot silently fall back to the system root, which
    would leave the orrery un-centered with no signal that the content join
    is broken.
    """
