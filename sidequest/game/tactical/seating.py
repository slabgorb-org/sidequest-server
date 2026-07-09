"""Seat encounter actors onto tactical-grid cells (ADR-096 v2, Track C2).

Durable per-actor positions live in ``EncounterActor.per_actor_state['cell']``
(a ``[x, y]`` list — JSON-round-trippable, resume-safe via the persisted
encounter). Seeding maps the generator's ``RegionTactical`` anchors onto the
seated actors: the entrance anchor to the first player-side actor, creature
anchors to opponents in seating order. Idempotent — an actor already carrying a
cell keeps it (dispatch outcomes, not re-seating, move a token).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sidequest.dungeon.tactical import TokenAnchor
    from sidequest.game.encounter import StructuredEncounter

Cell = tuple[int, int]


def seat_actor_cells(
    encounter: StructuredEncounter,
    anchors: list[TokenAnchor],
    *,
    player_side: str = "player",
) -> dict[str, Cell]:
    """Assign cells to unseated actors and return the full name->cell map.

    Players draw the entrance anchor first (then spill to creature anchors);
    opponents/neutrals draw creature anchors in order. Actors already carrying a
    ``per_actor_state['cell']`` are left untouched and reported as-is. With no
    anchors, nothing is placed."""
    entrance = [a.cell for a in anchors if a.role == "entrance"]
    creature = [a.cell for a in anchors if a.role == "creature"]
    player_cells = entrance + creature
    opponent_cells = list(creature)

    placed: dict[str, Cell] = {}
    pi = oi = 0
    for actor in encounter.actors:
        existing = actor.per_actor_state.get("cell")
        if existing is not None:
            placed[actor.name] = (int(existing[0]), int(existing[1]))
            continue
        if actor.side == player_side:
            pool, idx = player_cells, pi
            pi += 1
        else:
            pool, idx = opponent_cells, oi
            oi += 1
        if idx < len(pool):
            cell = pool[idx]
            actor.per_actor_state["cell"] = [cell[0], cell[1]]
            placed[actor.name] = cell
    return placed
