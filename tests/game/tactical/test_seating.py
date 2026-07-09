"""RED tests for Task 4 — position-as-combat-state seat mapping (ADR-096 v2, Track C2).

``seat_actor_cells`` maps a region's ``TokenAnchor`` set onto seated actors and
stashes durable per-actor positions in ``EncounterActor.per_actor_state['cell']``
(a JSON-round-trippable ``[x, y]`` list — resume-safe, no schema/DDL change).
Contract: entrance anchor -> first player-side actor, creature anchors ->
opponents in order; idempotent (an actor already carrying a cell keeps it);
pure over the passed anchors.
"""

from sidequest.game.tactical.seating import seat_actor_cells

from sidequest.dungeon.tactical import TokenAnchor
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter


def _encounter(actors=None):
    return StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="tension", threshold=10),
        opponent_metric=EncounterMetric(name="fear", threshold=10),
        actors=actors
        or [
            EncounterActor(name="Rux", role="combatant", side="player"),
            EncounterActor(name="rope-spider", role="combatant", side="opponent"),
            EncounterActor(name="cave-bat", role="combatant", side="opponent"),
        ],
    )


ANCHORS = [
    TokenAnchor((1, 1), "entrance"),
    TokenAnchor((3, 1), "creature"),
    TokenAnchor((3, 3), "creature"),
]


def test_seats_players_on_entrance_opponents_on_creature_anchors():
    enc = _encounter()
    placed = seat_actor_cells(enc, ANCHORS)
    assert placed["Rux"] == (1, 1)
    assert placed["rope-spider"] == (3, 1)
    assert placed["cave-bat"] == (3, 3)
    assert enc.actors[0].per_actor_state["cell"] == [1, 1]
    assert enc.actors[1].per_actor_state["cell"] == [3, 1]


def test_idempotent_skips_already_seated():
    enc = _encounter()
    enc.actors[0].per_actor_state["cell"] = [4, 4]
    placed = seat_actor_cells(enc, ANCHORS)
    assert placed["Rux"] == (4, 4)  # not overwritten
    assert enc.actors[0].per_actor_state["cell"] == [4, 4]


def test_no_anchors_places_nothing():
    enc = _encounter()
    placed = seat_actor_cells(enc, [])
    assert placed == {}
    assert "cell" not in enc.actors[0].per_actor_state


def test_overflow_opponents_beyond_anchors_are_unplaced():
    """More opponents than creature anchors -> the surplus is left unseated
    (no crash, no stacking onto a taken anchor, absent from the returned map)."""
    enc = _encounter(
        actors=[
            EncounterActor(name="Rux", role="combatant", side="player"),
            EncounterActor(name="spider-a", role="combatant", side="opponent"),
            EncounterActor(name="spider-b", role="combatant", side="opponent"),
            EncounterActor(name="spider-c", role="combatant", side="opponent"),
        ]
    )
    placed = seat_actor_cells(enc, ANCHORS)  # entrance + 2 creature anchors
    assert placed["spider-a"] == (3, 1)
    assert placed["spider-b"] == (3, 3)
    assert "spider-c" not in placed
    assert "cell" not in enc.actors[3].per_actor_state


def test_seated_cell_is_json_list_of_ints():
    """The stored cell must be a plain ``list[int]`` — a tuple or numpy scalar
    would not survive JSON round-trip on resume (the reason we ride
    ``per_actor_state`` rather than adding a typed field)."""
    enc = _encounter()
    seat_actor_cells(enc, ANCHORS)
    stored = enc.actors[0].per_actor_state["cell"]
    assert isinstance(stored, list)
    assert all(isinstance(component, int) for component in stored)
