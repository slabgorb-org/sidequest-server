"""Seam 2 — authored-cast staging on region entry (epic-157, story 157-3).

The "right cast appears" complement to Seam 1's creature filter. On a real
region transition (the ``dungeon.frontier_hook`` seam), push-stage that region's
authored cartography NPC cast into ``snapshot.npc_pool`` so the narrator surfaces
the Emperor / Reldresal on entering Mildendo instead of electing
``resolve_location_entity`` and inventing cross-voyage extras (the gulliver
bleed, session ``2026-06-20-gulliver-e721409c``).

Per the OTEL Observability Principle this emits ``zone_eligibility.cast_staged``
so the GM panel sees the engine surfaced the cast (vs. the narrator naming them
by luck) — the lie-detector sibling of ``zone_eligibility.filtered``.

Design: ``docs/superpowers/specs/2026-06-20-faction-zone-content-eligibility-design.md``
(§ "Seam 2 — NPCs", second bullet) + implementation plan Task 3.

**Concurrency.** ``frontier_hook._OBSERVERS`` is a process-global registry shared
across every live session. :func:`stage_region_cast` therefore resolves the
pack/cartography from the **snapshot it is handed** (``snapshot.genre_slug`` /
``world_slug``) via the loader cache — never from a per-session captured closure —
so a transition in session A can never stage session B's cast. The observer is
registered ONCE at server startup (:func:`register_cast_staging_observer`), not
per session.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sidequest.dungeon.frontier_hook import register_frontier_observer
from sidequest.game import zone_eligibility
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.genre.loader import load_genre_pack_cached
from sidequest.telemetry.spans import Span
from sidequest.telemetry.spans.zone_eligibility import SPAN_ZONE_ELIGIBILITY_CAST_STAGED

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sidequest.game.session import GameSnapshot

logger = logging.getLogger(__name__)


def stage_region_cast(
    *,
    snapshot: GameSnapshot,
    pc_name: str,
    from_region: str | None,
    to_region: str,
) -> None:
    """Stage the entered region's authored NPC cast into ``snapshot.npc_pool``.

    Frontier-observer signature (called by ``frontier_hook.notify_region_transition``
    on every real per-PC region transition). Reads ``to_region``'s cartography
    ``entities`` where ``binding.kind == "npc"`` and materializes each as a
    ``world_authored`` pool member, idempotently (a member already present by name
    is skipped, so re-entry stages nothing new). When any cast is staged, emits
    ``zone_eligibility.cast_staged`` carrying the region + staged names.

    The pack is resolved from ``snapshot.genre_slug`` so the staging always
    reflects the session that owns this transition. A session whose **world has
    no cartography** stages nothing (an authored-content gap, not a crash). The
    genre itself is assumed resolvable — ``load_genre_pack_cached`` raises
    ``GenreNotFoundError`` on an unknown genre (a real misconfiguration that
    fails loud per No Silent Fallbacks); in production ``snapshot.genre_slug`` is
    a server-set, always-valid slug, so that path is unreachable.
    """
    pack = load_genre_pack_cached(snapshot.genre_slug)
    cartography = zone_eligibility.cartography_for(snapshot, pack)
    if cartography is None:
        logger.debug(
            "zone_eligibility.cast_staged.skip reason=no_cartography genre=%r world=%r",
            snapshot.genre_slug,
            snapshot.world_slug,
        )
        return
    region = cartography.regions.get(to_region)
    if region is None:
        # An actionable discrepancy: the transition names a region that is not in
        # the world's cartography (e.g. a narrator-authored / misspelled region id
        # from a WorldStatePatch, which the load-time validator in 157-7 does not
        # gate). Warn so the operator can catch it without a GM-panel span hunt.
        logger.warning(
            "zone_eligibility.cast_staged.skip reason=unknown_region world=%r to_region=%r",
            snapshot.world_slug,
            to_region,
        )
        return

    existing = {member.name for member in snapshot.npc_pool}
    staged: list[str] = []
    for entity in region.entities:
        binding = entity.binding
        if binding is None or binding.kind != "npc":
            continue
        name = entity.label
        if name in existing:
            continue
        snapshot.npc_pool.append(NpcPoolMember(name=name, drawn_from="world_authored"))
        existing.add(name)
        staged.append(name)

    if not staged:
        return

    logger.info(
        "zone_eligibility.cast_staged region=%r npc_names=%s pc=%r",
        to_region,
        staged,
        pc_name,
    )
    with Span.open(
        SPAN_ZONE_ELIGIBILITY_CAST_STAGED,
        {"region": to_region, "npc_names": staged},
    ):
        pass


def register_cast_staging_observer() -> None:
    """Register :func:`stage_region_cast` as a process-lifetime frontier observer.

    Called once at server startup. ``register_frontier_observer`` is idempotent
    per identity, so a uvicorn ``--reload`` re-run of the startup hook does not
    double-register (which would double-stage every transition).
    """
    register_frontier_observer(stage_region_cast)
