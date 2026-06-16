"""Beneath Sünden — per-turn region projection (the BETTER fix, seam 1+2).

The materialized dungeon (``dungeon_map`` / ``RegionGraph``) is durable
truth in SQLite. Each narration turn this module projects the party's
*current* region — its theme register/flavor/motifs, its depth tone, and
its concrete adjacent region ids + edge kinds — into a structured
``RegionProjection``.

Two consumers, one source:

1. **Narrator prompt** — ``RegionProjection`` is rendered as a
   high-attention "you are here" section (gaslight discipline: a
   structured canonical-state section like the NPC roster, NOT an
   appended ``exits:`` advisory string). This is what stops the narrator
   improvising geography.
2. **Constrained move vocabulary** — the projection hands the narrator
   the *real* adjacent region ids. When the narrator emits a
   ``current_region`` WorldStatePatch it targets a VALID graph node, so
   ``frontier_hook.notify_region_transition`` fires on a real id and the
   look-ahead worker expands the dungeon — instead of location advancing
   by parsing invented scene titles.

Re-derived every turn from ``DungeonStore.load_map`` (single source of
truth — never mirrored onto the persisted snapshot; this codebase has a
documented snapshot-vs-SQLite divergence disease the recency window was
explicitly moved off the snapshot to cure).

No Silent Fallbacks: a current_region that is not a node in the graph,
or a theme id absent from the palette, raises loudly — that is a real
materialization/seed bug, never a quiet empty projection.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sidequest.dungeon.region_graph.model import RegionGraph
from sidequest.dungeon.themes import ThemePalette

__all__ = [
    "DUNGEON_GENRE",
    "DUNGEON_WORLD",
    "RegionExit",
    "RegionProjection",
    "applies_to",
    "project_region",
]

# The single dungeon this projection applies to. Mirrors the
# ``session_integration`` attach gate (kept here as the public,
# importable form so the per-turn turn-context seam can gate without
# reaching into that module's private ``_GENRE``/``_WORLD`` or
# duplicating the literals — a single source for "is this the
# megadungeon").
DUNGEON_GENRE = "caverns_and_claudes"
DUNGEON_WORLD = "beneath_sunden"


def applies_to(genre_slug: str, world_slug: str) -> bool:
    """True iff this session is the Beneath Sünden megadungeon.

    The per-turn region projection is a clean, observable no-op for every
    other world (the caller emits ``dungeon.region_projection
    outcome=no_dungeon`` so the skip is visible, never silent)."""
    return genre_slug == DUNGEON_GENRE and world_slug == DUNGEON_WORLD


@dataclass(frozen=True)
class RegionExit:
    """One concrete adjacency the narrator may move the party through.

    ``to_region_id`` is the EXACT graph node id the narrator must place in
    a ``current_region`` patch (the constrained move vocabulary). ``kind``
    is the edge kind (corridor|stairs|shaft|chute|secret). ``hidden``
    edges (secret/conditional) are valid move targets once discovered but
    must not be volunteered in prose unprompted. ``shortcut`` collapses
    distance toward the surface entrance.
    """

    to_region_id: str
    kind: str
    hidden: bool = False
    shortcut: bool = False


@dataclass(frozen=True)
class RegionProjection:
    """The party's current region, projected for one turn.

    Sourced from the live ``RegionGraph`` + curated ``ThemePalette``;
    consumed by the narrator-prompt region section and the DUNGEON_MAP
    wire frame. ``exits`` is the authoritative move vocabulary.
    """

    region_id: str
    theme_id: str
    theme_display: str
    register: str
    flavor: str
    motifs: list[str] = field(default_factory=list)
    depth_score: float | None = None
    exits: list[RegionExit] = field(default_factory=list)
    # True only for the procedural megadungeon. Gates the render's
    # "THE DUNGEON IS ALIVE AND HOSTILE" lethality directive so cartography
    # region-mode worlds (e.g. tea_and_murder/glenross) reuse the same
    # YOU-ARE-HERE section + MOVEMENT RULE without inheriting Moria-grade
    # dread. Default False keeps cartography projections cosy.
    is_dungeon: bool = False


def project_region(
    graph: RegionGraph,
    current_region: str,
    palette: ThemePalette,
) -> RegionProjection:
    """Project ``current_region`` against the live graph + palette.

    Fail-loud (CLAUDE.md No Silent Fallbacks):
      - ``current_region`` blank          -> ValueError (caller must not
        project before the entrance is bound; that is the #314 seam)
      - ``current_region`` not a node     -> ValueError (a real seed /
        binding bug — the narrator must never be fed a phantom region)
      - node.theme absent from palette    -> KeyError (palette.get)
    """
    if not current_region:
        raise ValueError(
            "project_region called with a blank current_region — the "
            "session must bind the entrance (session_integration bootstrap) "
            "before any region projection; a blank region is a wiring bug, "
            "not an empty projection (No Silent Fallbacks)"
        )
    node = graph.nodes.get(current_region)
    if node is None:
        raise ValueError(
            f"current_region {current_region!r} is not a node in the live "
            f"dungeon graph (have: {sorted(graph.nodes)}); the narrator must "
            "never be projected a phantom region — this is a seed/binding "
            "bug, never a quiet empty projection (No Silent Fallbacks)"
        )

    theme = palette.get(node.theme)  # fail-loud on unknown theme id

    exits: list[RegionExit] = []
    for edge in graph.edges:
        if edge.a == current_region:
            other = edge.b
        elif edge.b == current_region:
            other = edge.a
        else:
            continue
        exits.append(
            RegionExit(
                to_region_id=other,
                kind=edge.kind,
                hidden=edge.hidden,
                shortcut=edge.shortcut,
            )
        )
    # Deterministic ordering: visible before hidden, then by id, so the
    # prompt section and the wire frame are stable turn-to-turn (an
    # unstable exit list reads as the dungeon "shifting" to a career GM).
    exits.sort(key=lambda e: (e.hidden, e.to_region_id))

    return RegionProjection(
        region_id=node.id,
        theme_id=node.theme,
        theme_display=theme.display_name,
        register=theme.narrator.register,
        flavor=theme.narrator.flavor,
        motifs=list(theme.narrator.motifs),
        depth_score=node.depth_score,
        exits=exits,
        is_dungeon=True,
    )
