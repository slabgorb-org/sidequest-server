"""ADR-153 §3 state graph — resolver surfaces ``next_state`` (158-40, AC-4).

Pure-resolver unit tests: ``resolve_sealed_letter_lookup`` must carry the
matched cell's ``next_state`` onto ``SealedLetterOutcome`` (None = stay),
and the extend-and-return reset must FORCE ``next_state="merge"`` — the
geometry reset IS a transition back to merge and wins over whatever the
cell authored.

RED: ``InteractionCell`` rejects ``next_state`` (extra="forbid") and
``SealedLetterOutcome`` has no ``next_state`` attribute.
"""

from __future__ import annotations

from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.genre.models.rules import InteractionCell, InteractionTable
from sidequest.server.dispatch.sealed_letter import resolve_sealed_letter_lookup


def _enc() -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="dogfight",
        win_condition="hp_depletion",
        player_metric=EncounterMetric(name="hits", current=0, threshold=3),
        opponent_metric=EncounterMetric(name="hits", current=0, threshold=3),
        actors=[
            EncounterActor(name="Red Pilot", role="red", side="player"),
            EncounterActor(name="Blue Pilot", role="blue", side="opponent"),
        ],
    )


def _table(cells: list[InteractionCell], maneuvers: list[str]) -> InteractionTable:
    return InteractionTable(
        version="1",
        starting_state="merge",
        maneuvers_consumed=maneuvers,
        cells=cells,
    )


def test_outcome_carries_cell_next_state() -> None:
    """AC-4: the outcome reports the matched cell's transition target so the
    apply seam can advance ``dogfight_state``."""
    table = _table(
        [
            InteractionCell(
                pair=["straight", "loop"],
                name="Blue reverses onto Red's six",
                red_view={"gun_solution": False, "closure": "closing"},
                blue_view={"gun_solution": False, "closure": "closing"},
                narration_hint="Blue loops onto the six.",
                next_state="tail_chase",
            )
        ],
        ["straight", "loop"],
    )
    out = resolve_sealed_letter_lookup(_enc(), {"red": "straight", "blue": "loop"}, table)
    assert out.next_state == "tail_chase"
    assert out.extend_and_return_triggered is False


def test_outcome_next_state_none_when_cell_stays() -> None:
    """AC-4: a cell with no authored transition reports None — the duel stays
    in the current state (never a silent default to merge)."""
    table = _table(
        [
            InteractionCell(
                pair=["straight", "straight"],
                name="Clean merge",
                red_view={"gun_solution": False, "closure": "closing"},
                blue_view={"gun_solution": False, "closure": "closing"},
                narration_hint="Both ships rip past.",
            )
        ],
        ["straight"],
    )
    out = resolve_sealed_letter_lookup(_enc(), {"red": "straight", "blue": "straight"}, table)
    assert out.next_state is None


def test_extend_and_return_forces_next_state_merge() -> None:
    """AC-4: when the engagement breaks apart (no gun solution anywhere +
    opening_fast) the extend-and-return reset fires and the outcome's
    ``next_state`` is ``"merge"`` — overriding the cell's authored target.
    The reset is the ADR-153 'reset toward merge, energy carried over' rule
    expressed as a real graph transition."""
    table = _table(
        [
            InteractionCell(
                pair=["bank", "bank"],
                name="Mutual break — engagement dissolves",
                red_view={"gun_solution": False, "closure": "opening_fast"},
                blue_view={"gun_solution": False, "closure": "opening_fast"},
                narration_hint="Both pilots break away; the fight goes wide.",
                next_state="scissors",
            )
        ],
        ["bank"],
    )
    enc = _enc()
    out = resolve_sealed_letter_lookup(enc, {"red": "bank", "blue": "bank"}, table)
    assert out.extend_and_return_triggered is True
    assert out.next_state == "merge", (
        "extend-and-return IS the transition back to merge — it must win over "
        f"the cell's authored next_state, got {out.next_state!r}"
    )
    # The geometry reset itself is pre-existing behavior — pin one field so
    # the transition and the reset can't drift apart.
    for actor in enc.actors:
        assert actor.per_actor_state["target_aspect"] == "head_on"
