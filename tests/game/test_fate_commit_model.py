from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.game.encounter import EncounterMetric, FateSealedCommit, StructuredEncounter
from sidequest.game.fate_sheet import Aspect


def _enc() -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="duel",
        category="combat",
        player_metric=EncounterMetric(name="p", threshold=10),
        opponent_metric=EncounterMetric(name="o", threshold=10),
    )


def test_fate_sealed_commit_shape():
    c = FateSealedCommit(
        actor="Sleuth",
        action="attack",
        skill="Fight",
        target="Thug",
        ladder_total=6,
        dice=(1, 1, 0, -1),
    )
    assert c.action == "attack"
    assert c.difficulty == 0
    assert c.aspect_text == ""


def test_fate_sealed_commit_rejects_unknown_action():
    with pytest.raises(ValidationError):
        FateSealedCommit(actor="x", action="parry", skill="Fight")  # not one of the four


def test_encounter_carries_empty_fate_ledgers_by_default():
    enc = _enc()
    assert enc.fate_commits == []
    assert enc.situation_aspects == []
    assert enc.zones == []


def test_encounter_with_fate_state_round_trips_json():
    enc = _enc()
    enc.zones = ["The Bar", "The Alley"]
    enc.fate_commits.append(
        FateSealedCommit(
            actor="Sleuth",
            action="attack",
            skill="Fight",
            target="Thug",
            ladder_total=5,
            dice=(1, 1, 0, -1),
        )
    )
    enc.situation_aspects.append(Aspect(text="Spilled Whiskey", kind="situation", free_invokes=1))

    restored = StructuredEncounter.model_validate_json(enc.model_dump_json())
    assert restored.zones == ["The Bar", "The Alley"]
    assert restored.fate_commits[0].target == "Thug"
    # tuple coerces back from the JSON list
    assert restored.fate_commits[0].dice == (1, 1, 0, -1)
    assert restored.situation_aspects[0].text == "Spilled Whiskey"
