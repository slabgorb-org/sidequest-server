"""Class/calling roster resolver — world-tier precedence over genre-tier.

Epic 94 (genre/world boundary correction, supersedes ADR-120
"mechanics-in-genre"): a world's classes/callings are a world-tier
CAST/CATALOG surface — the roster of playable archetypes a world ships
(C&C kits, Victoria callings) — not a genre mechanic. The genre tier is
the rulebook only.

Mirrors :mod:`sidequest.server.dispatch.char_creation_resolve`: when the
selected world declares its own ``classes`` list, that replaces the
genre-level roster wholesale. There is no merge. World-empty (or
world-not-in-pack) falls through to the genre-tier ``pack.classes``
(which, for migrated packs, is itself the union of every world's roster
— see ``loader.load_genre_pack``).

Emits a ``state_transition`` watcher event recording which tier the
roster was resolved from, so the GM panel can prove the chargen builder
read the class cast from the world tier rather than improvising it.
"""

from __future__ import annotations

from sidequest.genre.models.character import ClassDef
from sidequest.genre.models.pack import GenrePack


def resolve_classes(
    pack: GenrePack,
    world_slug: str | None,
) -> list[ClassDef]:
    """Return the class/calling roster for a connection.

    World-tier when ``pack.worlds[world_slug].classes`` is non-empty;
    otherwise genre-tier ``pack.classes``. The world list **replaces**
    the genre list — it is not merged. Falsy ``world_slug`` (``None`` or
    empty string) and unknown worlds both fall through to the genre tier.

    Returns a fresh list each call so callers can mutate freely without
    aliasing the model's stored list.
    """
    tier = "genre"
    resolved: list[ClassDef]
    if world_slug:
        world = pack.worlds.get(world_slug)
        if world is not None and world.classes:
            resolved = list(world.classes)
            tier = "world"
            _emit_classes_resolved(world_slug=world_slug, tier=tier, count=len(resolved))
            return resolved
    resolved = list(pack.classes)
    _emit_classes_resolved(world_slug=world_slug or "", tier=tier, count=len(resolved))
    return resolved


def _emit_classes_resolved(*, world_slug: str, tier: str, count: int) -> None:
    """Emit a ``state_transition`` span recording the class-roster tier.

    OTEL Observability Principle: every world-tier resolution decision
    fires a span so the GM panel can confirm the chargen builder got its
    class cast from the world tier (epic 94) and is not improvising.
    """
    from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

    _watcher_publish(
        "state_transition",
        {
            "field": "chargen_classes",
            "op": "resolved",
            "world_slug": world_slug,
            "tier": tier,
            "class_count": count,
        },
        component="genre",
    )
