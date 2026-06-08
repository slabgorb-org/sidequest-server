"""SWN P4: the narrator turn input carries the engine-rolled initiative order as
the authoritative resolution sequence (math is engine-owned; narrator describes)."""

from __future__ import annotations

from sidequest.game.encounter import EncounterMetric, StructuredEncounter
from sidequest.handlers.player_action import initiative_preamble
from sidequest.protocol.models import InitiativeEntry


def _enc(initiative, *, resolved=False):
    return StructuredEncounter(
        encounter_type="firefight",
        player_metric=EncounterMetric(name="hp", current=0, starting=0, threshold=1),
        opponent_metric=EncounterMetric(name="hp", current=0, starting=0, threshold=1),
        initiative=initiative,
        resolved=resolved,
    )


def test_preamble_lists_order_and_states_the_rule():
    enc = _enc(
        [
            InitiativeEntry(token_id="Rux", value=9),
            InitiativeEntry(token_id="Raider", value=5),
        ]
    )
    text = initiative_preamble(enc)
    assert text is not None
    assert "Rux" in text and "Raider" in text
    assert text.index("Rux") < text.index("Raider")  # ordered
    assert "0 HP" in text  # the dead-actor-does-not-act rule statement (P5 enforces)


def test_preamble_none_when_no_initiative():
    assert initiative_preamble(_enc([])) is None
    assert initiative_preamble(None) is None


def test_preamble_none_when_encounter_resolved():
    """sq-playtest 2026-06-07 barsoom (#177 defect b): once the fight is over
    (``check_hp_depletion`` sets ``resolved=True`` on a kill but leaves
    ``snapshot.encounter`` populated), the initiative scaffold must STOP
    stamping. Pre-fix the header ran ~10 rounds of looting/resting past the
    opponent's death because the preamble keyed on ``.initiative`` truthiness
    alone, never on ``.resolved`` — unlike every other "active encounter"
    check (``enc is not None and not enc.resolved``)."""
    enc = _enc(
        [
            InitiativeEntry(token_id="Rux", value=9),
            InitiativeEntry(token_id="Raider", value=5),
        ],
        resolved=True,
    )
    assert initiative_preamble(enc) is None
