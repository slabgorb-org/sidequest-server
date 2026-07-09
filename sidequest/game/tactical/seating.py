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
    from sidequest.game.encounter import EncounterActor, StructuredEncounter

Cell = tuple[int, int]


def seat_actor_cells(
    encounter: StructuredEncounter,
    anchors: list[TokenAnchor],
    *,
    player_side: str = "player",
) -> dict[str, Cell]:
    """Assign cells to unseated actors and return the full name->cell map.

    Seating is collision-free over a single shared occupancy set (165-3 BLOCKER
    3 — Keith's playgroup is multiplayer, so a table of PCs is the common case,
    not an edge). Sides are served in priority order — players, then opponents,
    then neutrals — so a player who overflows the entrance anchor cannot land on
    the same cell as an opponent, and a neutral bystander cannot pre-empt a
    monster's anchor. Players prefer the entrance anchor then spill to creature
    cells; opponents/neutrals prefer creature cells then spill to whatever is
    free. Actors already carrying a ``per_actor_state['cell']`` keep it (and
    reserve that cell); with no anchors, nothing is placed."""
    entrance = [a.cell for a in anchors if a.role == "entrance"]
    creature = [a.cell for a in anchors if a.role == "creature"]

    placed: dict[str, Cell] = {}
    used: set[Cell] = set()

    # Idempotent: actors already seated keep their cell AND reserve it so no one
    # else is placed on top (dispatch outcomes, not re-seating, move a token).
    unseated: list[EncounterActor] = []
    for actor in encounter.actors:
        existing = actor.per_actor_state.get("cell")
        if existing is not None:
            cell = (int(existing[0]), int(existing[1]))
            placed[actor.name] = cell
            used.add(cell)
        else:
            unseated.append(actor)

    def _take(pools: list[list[Cell]]) -> Cell | None:
        for pool in pools:
            for cell in pool:
                if cell not in used:
                    used.add(cell)
                    return cell
        return None

    def _priority(actor: EncounterActor) -> int:
        side = actor.side
        if side == player_side:
            return 0
        if side == "opponent":
            return 1
        return 2  # neutral / bystander — seated last, never pre-empts a monster

    for actor in sorted(unseated, key=_priority):
        if actor.side == player_side:
            cell = _take([entrance, creature])
        else:
            cell = _take([creature, entrance])
        if cell is not None:
            actor.per_actor_state["cell"] = [cell[0], cell[1]]
            placed[actor.name] = cell
    return placed
