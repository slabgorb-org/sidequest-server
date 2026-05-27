"""SWN P4: StructuredEncounter persists the rolled initiative order."""

from __future__ import annotations

from sidequest.game.encounter import EncounterMetric, StructuredEncounter
from sidequest.protocol.models import InitiativeEntry


def _enc(**overrides) -> StructuredEncounter:
    base = dict(
        encounter_type="firefight",
        player_metric=EncounterMetric(name="hp", current=0, starting=0, threshold=1),
        opponent_metric=EncounterMetric(name="hp", current=0, starting=0, threshold=1),
    )
    base.update(overrides)
    return StructuredEncounter(**base)


def test_initiative_defaults_empty():
    assert _enc().initiative == []


def test_initiative_can_be_set():
    enc = _enc()
    enc.initiative = [InitiativeEntry(token_id="Rux", value=9)]
    assert enc.initiative[0].token_id == "Rux"
    assert enc.initiative[0].value == 9
