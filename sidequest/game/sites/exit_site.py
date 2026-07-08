"""exit_site seam resolver — bind THIS PC back to the site's owning region."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sidequest.game.seams.base import SeamCrossingError, SeamCrossingResult
from sidequest.game.session import WorldStatePatch
from sidequest.telemetry.spans.site import site_exit_span

if TYPE_CHECKING:
    from sidequest.game.session import GameSnapshot
    from sidequest.game.sites import SiteDescriptor
    from sidequest.genre.models.world import CartographyConfig

logger = logging.getLogger(__name__)


def resolve_exit_site(
    *,
    snapshot: GameSnapshot,
    player_name: str,
    site: SiteDescriptor,
    cartography: CartographyConfig | None = None,
    resolved_via: str = "site_exit",
    **_context: Any,
) -> SeamCrossingResult:
    """Bind THIS PC back to ``site.attached_to`` (the owning cartography region),
    or raise. Membership-checked — never strand the PC on a phantom region."""
    from_region = snapshot.region_for(perspective=player_name) or ""
    if cartography is None:
        raise SeamCrossingError(
            reason="no_cartography",
            surface=(
                f"There is a way out of {site.name}, but the map itself was "
                "never loaded — a wiring fault, not a sealed door."
            ),
        )
    surface_id = site.attached_to
    regions = getattr(cartography, "regions", None) or {}
    if not surface_id or surface_id not in regions:
        raise SeamCrossingError(
            reason="dangling_site_owner",
            surface=(
                f"There is a way out of {site.name}, but it returns to a place "
                "that isn't on the map — a wiring fault, not a sealed door."
            ),
        )
    snapshot.apply_world_patch(WorldStatePatch(pc_region={player_name: surface_id}))
    with site_exit_span(pc_name=player_name, site_id=site.site_id, from_region=from_region) as span:
        span.set_attribute("to_region", surface_id)
        span.set_attribute("resolved_via", resolved_via)
    logger.debug(
        "site.exit pc=%s site=%s from=%s to=%s",
        player_name,
        site.site_id,
        from_region,
        surface_id,
    )
    return SeamCrossingResult(to_region=surface_id)
