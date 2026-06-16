"""Psionic discipline-catalog resolver — world-tier precedence over genre-tier.

Story 102-6. Mirrors :mod:`sidequest.server.dispatch.wwn_spell_catalog_resolve`:
a world's psionic discipline catalog is a world-tier CAST/CATALOG surface (the
disciplines a world ships, ADR-140 "Crunch in the Genre, Flavor in the World"),
not a genre mechanic. When the selected world declares its own
``psionic_discipline_catalog`` (``worlds/<slug>/disciplines_psionic.yaml``), that
REPLACES the genre-level catalog wholesale — there is no merge. World-empty (or
world-not-in-pack) falls through to the genre-tier
``GenrePack.psionic_discipline_catalog``.

Emits a ``state_transition`` watcher event recording which tier the catalog was
resolved from, so the GM panel can prove the activation pipeline read the
discipline catalog rather than improvising it.
"""

from __future__ import annotations

from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.psionics import PsionicDisciplineCatalog


def resolve_psionic_discipline_catalog(
    pack: GenrePack,
    world_slug: str | None,
) -> PsionicDisciplineCatalog | None:
    """Return the psionic discipline catalog for a connection/turn.

    World-tier when ``pack.worlds[world_slug].psionic_discipline_catalog`` is
    present; otherwise genre-tier ``pack.psionic_discipline_catalog``. The world
    catalog **replaces** the genre catalog — it is not merged. Falsy
    ``world_slug`` and unknown worlds fall through to the genre tier. ``None``
    when neither tier ships a catalog (a valid state for a non-psionic pack — no
    silent fallback to a fabricated catalog).
    """
    if world_slug:
        world = pack.worlds.get(world_slug)
        if world is not None and world.psionic_discipline_catalog is not None:
            resolved = world.psionic_discipline_catalog
            _emit_catalog_resolved(
                world_slug=world_slug,
                tier="world",
                discipline_count=len(resolved.disciplines),
            )
            return resolved
    resolved = pack.psionic_discipline_catalog
    _emit_catalog_resolved(
        world_slug=world_slug or "",
        tier="genre",
        discipline_count=len(resolved.disciplines) if resolved is not None else 0,
    )
    return resolved


def _emit_catalog_resolved(*, world_slug: str, tier: str, discipline_count: int) -> None:
    """Emit a ``state_transition`` span recording the discipline-catalog tier.

    OTEL Observability Principle: every world-tier resolution decision fires a
    span so the GM panel can confirm the activation pipeline got its discipline
    catalog from the world tier and is not improvising.
    """
    from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

    _watcher_publish(
        "state_transition",
        {
            "field": "psionic_discipline_catalog",
            "op": "resolved",
            "world_slug": world_slug,
            "tier": tier,
            "discipline_count": discipline_count,
        },
        component="genre",
    )
