"""StructuredEncounter carries an optional EncounterLocationOverlay (Story 54-7)."""

from __future__ import annotations

from sidequest.game.encounter import (
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.protocol.models import (
    EncounterLocationOverlay,
    LocationEntity,
)


def _base_kwargs() -> dict:
    return {
        "encounter_type": "tavern_brawl",
        "player_metric": EncounterMetric(
            name="composure", current=10, starting=10, threshold=20
        ),
        "opponent_metric": EncounterMetric(
            name="brawl_energy", current=10, starting=10, threshold=20
        ),
    }


def test_encounter_default_has_no_location_overlay():
    enc = StructuredEncounter(**_base_kwargs())
    assert enc.location_overlay is None


def test_encounter_accepts_location_overlay():
    overlay = EncounterLocationOverlay(
        bound_room_id="glenross_pub",
        entity_delta=[
            LocationEntity(
                id="overturned_table",
                label="an overturned table",
                tier="yes_and",
            ),
        ],
        prose_suffix="A chair lies in splinters by the door.",
    )
    enc = StructuredEncounter(**_base_kwargs(), location_overlay=overlay)
    assert enc.location_overlay is not None
    assert enc.location_overlay.bound_room_id == "glenross_pub"
    assert len(enc.location_overlay.entity_delta) == 1
    assert "splinters" in enc.location_overlay.prose_suffix
