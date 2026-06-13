"""Beneath Sünden — per-turn region projection (the BETTER fix, seam 1+2).

The materialized dungeon (``dungeon_map`` / ``RegionGraph``) is durable
truth in Postgres (``PgDungeonRepository``, ADR-115). Each narration turn
this module projects the party's
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

Re-derived every turn from ``DungeonRepository.load_map`` (single source
of truth — never mirrored onto the persisted snapshot; this codebase has a
documented snapshot-vs-store divergence disease the recency window was
explicitly moved off the snapshot to cure).

No Silent Fallbacks: a current_region that is not a node in the graph,
or a theme id absent from the palette, raises loudly — that is a real
materialization/seed bug, never a quiet empty projection.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from sidequest.dungeon.region_graph.model import RegionGraph
from sidequest.dungeon.themes import ThemePalette

__all__ = [
    "BEARING_SYNONYMS",
    "COMPASS_ORDER",
    "DUNGEON_GENRE",
    "DUNGEON_WORLD",
    "RegionExit",
    "RegionProjection",
    "applies_to",
    "assign_bearings",
    "project_region",
    "requested_bearing",
]

# Compass slots a region's horizontal exits fan out across, in probe order
# (cardinals first so a small room reads "north / east / south / west" before
# it ever reaches a diagonal). Vertical exits (shaft/chute/stairs) take
# ``up``/``down`` instead — see ``assign_bearings``.
COMPASS_ORDER: tuple[str, ...] = (
    "north",
    "east",
    "south",
    "west",
    "northeast",
    "southeast",
    "southwest",
    "northwest",
)

# Player words that name a bearing → the canonical bearing label. The
# movement resolver folds these into each exit's descriptor-match surface so
# "I go north" / "down the stairs" / "the eastern passage" resolve to the
# edge that carries that bearing. Relative words (left/right/ahead/behind)
# are deliberately ABSENT: without a facing vector they cannot map to a
# compass point, and the coarse ``direction`` path (deeper/back) already
# owns ahead/behind. Mapping them here would re-introduce the exact
# ambiguity this whole bearing system exists to kill.
BEARING_SYNONYMS: dict[str, set[str]] = {
    "north": {"north", "northern", "northward"},
    "east": {"east", "eastern", "eastward"},
    "south": {"south", "southern", "southward"},
    "west": {"west", "western", "westward"},
    "northeast": {"northeast", "northeastern", "northeastward"},
    "southeast": {"southeast", "southeastern", "southeastward"},
    "southwest": {"southwest", "southwestern", "southwestward"},
    "northwest": {"northwest", "northwestern", "northwestward"},
    "down": {"down", "downward", "descend", "below", "lower", "downstairs"},
    "up": {"up", "upward", "ascend", "above", "upstairs"},
}

# All bearing words, for ``requested_bearing`` token probing.
_ALL_BEARING_WORDS: dict[str, str] = {
    word: bearing for bearing, words in BEARING_SYNONYMS.items() for word in words
}

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
    bearing: str = ""


def _bearing_home_slot(region_id: str, other_id: str, modulo: int) -> int:
    """Deterministic compass home slot for the (region, neighbor) pair.

    blake2b, NOT Python ``hash()`` — the builtin is per-process salted and
    would reshuffle every region's bearings on each server restart (a career
    GM would see the dungeon's directions rotate between sessions). Matches
    the region-graph generator's anti-XOR sub-seeding convention.
    """
    digest = hashlib.blake2b(f"{region_id}|{other_id}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") % modulo


def assign_bearings(graph: RegionGraph, region_id: str) -> dict[str, str]:
    """Assign each exit of ``region_id`` a stable, DISTINCT bearing.

    Vertical edges (shaft/chute/stairs) take ``up``/``down`` by depth delta;
    everything else fans out across ``COMPASS_ORDER`` from a per-pair
    blake2b home slot with deterministic forward probing on collision. The
    distinctness is the point: the 4-way "the corridor ahead" tie that made
    movement unresolvable becomes "the north corridor / the east corridor /
    the shaft down / the stairs up", each a single edge.

    Pure function of the graph (region id + neighbor id + kind + depth), so
    it needs no persistence and is identical across loads — narrator prompt,
    DUNGEON_MAP frame, and movement resolver all derive the same labels.

    Returns ``{to_region_id: bearing}``. A region with more horizontal exits
    than compass slots (>8 — vanishingly rare) gives the overflow exits a
    still-distinct ``passage-<id>`` label rather than colliding.
    """
    node = graph.nodes.get(region_id)
    cur_depth = node.depth_score if (node and node.depth_score is not None) else 0.0

    neighbors: list[tuple[str, str]] = []
    for edge in graph.edges:
        if edge.a == region_id:
            neighbors.append((edge.b, edge.kind))
        elif edge.b == region_id:
            neighbors.append((edge.a, edge.kind))

    bearings: dict[str, str] = {}
    used: set[str] = set()
    horizontals: list[str] = []

    # Vertical first — a shaft is "down", stairs follow the depth delta. A
    # collision (two shafts) demotes the later one to a compass bearing.
    for other, kind in sorted(neighbors):
        nd_node = graph.nodes.get(other)
        ndepth = nd_node.depth_score if (nd_node and nd_node.depth_score is not None) else cur_depth
        if kind in ("shaft", "chute"):
            label = "down" if ndepth >= cur_depth else "up"
        elif kind == "stairs":
            label = "down" if ndepth > cur_depth else "up"
        else:
            horizontals.append(other)
            continue
        if label in used:
            horizontals.append(other)
        else:
            bearings[other] = label
            used.add(label)

    # Horizontal exits: hashed home slot + forward probe. Process in
    # (home_slot, id) order so the assignment is fully deterministic.
    for other in sorted(horizontals, key=lambda o: (_bearing_home_slot(region_id, o, 8), o)):
        start = _bearing_home_slot(region_id, other, len(COMPASS_ORDER))
        for k in range(len(COMPASS_ORDER)):
            cand = COMPASS_ORDER[(start + k) % len(COMPASS_ORDER)]
            if cand not in used:
                bearings[other] = cand
                used.add(cand)
                break
        else:
            bearings[other] = f"passage-{other}"

    return bearings


def requested_bearing(text: str) -> str | None:
    """The canonical bearing a player's words name, or ``None``.

    Token-exact (not substring) so "downstairs" maps to ``down`` but a
    region id like ``upper_vault`` in flavor text does not spuriously match
    ``up``. Longest compound first so "north" inside "northeast" never wins
    over the diagonal.
    """
    tokens = {t for t in re.findall(r"[a-z]+", (text or "").lower())}
    # Prefer a compound diagonal if both its word and a cardinal appear.
    for word in sorted(_ALL_BEARING_WORDS, key=len, reverse=True):
        if word in tokens:
            return _ALL_BEARING_WORDS[word]
    return None


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

    bearings = assign_bearings(graph, current_region)
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
                bearing=bearings.get(other, ""),
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
