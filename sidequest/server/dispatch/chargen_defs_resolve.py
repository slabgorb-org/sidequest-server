"""Background and Focus roster resolvers — world-tier precedence over genre-tier.

ADR-143 (Ruleset Chargen Seam): backgrounds / foci are world-tier CAST/CATALOG
surfaces (the definitions a world ships), not genre mechanics. The genre tier
is the rulebook only.

Mirrors :mod:`sidequest.server.dispatch.class_resolve`: when the selected world
declares its own ``backgrounds`` / ``foci`` dict, that replaces the genre-level
roster wholesale.  There is no merge.  World-empty (or world-not-in-pack) falls
through to the genre-tier dict.

Emits ``state_transition`` watcher events recording which tier the roster was
resolved from, so the GM panel can prove the chargen builder read the defs from
the world tier rather than improvising them.
"""

from __future__ import annotations

from sidequest.genre.models.character import Background, Focus
from sidequest.genre.models.pack import GenrePack


def resolve_backgrounds(
    pack: GenrePack,
    world_slug: str | None,
) -> dict[str, Background]:
    """Return the background catalog for a connection.

    World-tier when ``pack.worlds[world_slug].backgrounds`` is non-empty;
    otherwise genre-tier ``pack.backgrounds``.  The world dict **replaces**
    the genre dict — it is not merged.  Falsy ``world_slug`` (``None`` or
    empty string) and unknown worlds both fall through to the genre tier.

    Returns a fresh dict each call so callers can mutate freely without
    aliasing the model's stored dict.
    """
    tier = "genre"
    resolved: dict[str, Background]
    if world_slug:
        world = pack.worlds.get(world_slug)
        if world is not None and world.backgrounds:
            resolved = dict(world.backgrounds)
            tier = "world"
            _emit_backgrounds_resolved(world_slug=world_slug, tier=tier, count=len(resolved))
            return resolved
    resolved = dict(pack.backgrounds)
    _emit_backgrounds_resolved(world_slug=world_slug or "", tier=tier, count=len(resolved))
    return resolved


def resolve_foci(
    pack: GenrePack,
    world_slug: str | None,
) -> dict[str, Focus]:
    """Return the focus catalog for a connection.

    World-tier when ``pack.worlds[world_slug].foci`` is non-empty;
    otherwise genre-tier ``pack.foci``.  The world dict **replaces**
    the genre dict — it is not merged.  Same fall-through rules as
    :func:`resolve_backgrounds`.

    Returns a fresh dict each call so callers can mutate freely without
    aliasing the model's stored dict.
    """
    tier = "genre"
    resolved: dict[str, Focus]
    if world_slug:
        world = pack.worlds.get(world_slug)
        if world is not None and world.foci:
            resolved = dict(world.foci)
            tier = "world"
            _emit_foci_resolved(world_slug=world_slug, tier=tier, count=len(resolved))
            return resolved
    resolved = dict(pack.foci)
    _emit_foci_resolved(world_slug=world_slug or "", tier=tier, count=len(resolved))
    return resolved


def _emit_backgrounds_resolved(*, world_slug: str, tier: str, count: int) -> None:
    """Emit a ``state_transition`` span recording the background-catalog tier.

    OTEL Observability Principle: every world-tier resolution decision fires a
    span so the GM panel can confirm the chargen builder got its background
    catalog from the world tier (ADR-143) and is not improvising.
    """
    from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

    _watcher_publish(
        "state_transition",
        {
            "field": "chargen_backgrounds",
            "op": "resolved",
            "world_slug": world_slug,
            "tier": tier,
            "background_count": count,
        },
        component="genre",
    )


def _emit_foci_resolved(*, world_slug: str, tier: str, count: int) -> None:
    """Emit a ``state_transition`` span recording the focus-catalog tier.

    OTEL Observability Principle: every world-tier resolution decision fires a
    span so the GM panel can confirm the chargen builder got its focus catalog
    from the world tier (ADR-143) and is not improvising.
    """
    from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

    _watcher_publish(
        "state_transition",
        {
            "field": "chargen_foci",
            "op": "resolved",
            "world_slug": world_slug,
            "tier": tier,
            "focus_count": count,
        },
        component="genre",
    )
