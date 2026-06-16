"""Read-time merge helpers per spec §5.5 (Story 54-7)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sidequest.game.encounter import EncounterMetric, StructuredEncounter
from sidequest.game.location_view import (
    active_overlays_for,
    get_location_manifest,
    get_location_prose,
)
from sidequest.game.persistence import SqliteStore
from sidequest.protocol.models import (
    EncounterLocationOverlay,
    LocationEntity,
)


def _authored() -> list[LocationEntity]:
    return [
        LocationEntity(id="bar", label="the bar", tier="real_object"),
        LocationEntity(id="cobwebs", label="cobwebs", tier="flavor_only"),
    ]


def _enc_with_overlay(bound_room: str, *, resolved: bool = False) -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="tavern_brawl",
        player_metric=EncounterMetric(name="composure", current=10, starting=10, threshold=20),
        opponent_metric=EncounterMetric(name="brawl_energy", current=10, starting=10, threshold=20),
        resolved=resolved,
        location_overlay=EncounterLocationOverlay(
            bound_room_id=bound_room,
            entity_delta=[
                LocationEntity(
                    id="overturned_table",
                    label="an overturned table",
                    tier="yes_and",
                ),
            ],
            prose_suffix="A chair lies in splinters by the door.",
        ),
    )


def test_active_overlays_for_empty_when_no_encounter():
    snapshot = MagicMock()
    snapshot.encounter = None
    assert active_overlays_for(snapshot, region_id="glenross_pub") == []


def test_active_overlays_for_returns_overlay_when_matched_and_live():
    snapshot = MagicMock()
    snapshot.encounter = _enc_with_overlay("glenross_pub", resolved=False)
    overlays = active_overlays_for(snapshot, region_id="glenross_pub")
    assert len(overlays) == 1
    assert overlays[0].bound_room_id == "glenross_pub"


def test_active_overlays_for_empty_when_encounter_resolved():
    snapshot = MagicMock()
    snapshot.encounter = _enc_with_overlay("glenross_pub", resolved=True)
    assert active_overlays_for(snapshot, region_id="glenross_pub") == []


def test_active_overlays_for_empty_when_bound_room_id_mismatch():
    snapshot = MagicMock()
    snapshot.encounter = _enc_with_overlay("other_room", resolved=False)
    assert active_overlays_for(snapshot, region_id="glenross_pub") == []


def test_active_overlays_for_empty_when_encounter_has_no_overlay():
    snapshot = MagicMock()
    snapshot.encounter = StructuredEncounter(
        encounter_type="tavern_brawl",
        player_metric=EncounterMetric(name="composure", current=10, starting=10, threshold=20),
        opponent_metric=EncounterMetric(name="brawl_energy", current=10, starting=10, threshold=20),
    )
    assert active_overlays_for(snapshot, region_id="glenross_pub") == []


@pytest.fixture
def store(tmp_path: Path) -> SqliteStore:
    return SqliteStore(tmp_path / "save.db")


def test_get_location_manifest_no_overlay(store):
    snapshot = MagicMock()
    snapshot.encounter = None
    manifest = get_location_manifest(
        region_id="glenross_pub",
        authored=_authored(),
        snapshot=snapshot,
        store=store,
        save_id="default",
    )
    assert [e.id for e in manifest] == ["bar", "cobwebs"]


def test_get_location_manifest_with_overlay(store):
    snapshot = MagicMock()
    snapshot.encounter = _enc_with_overlay("glenross_pub")
    manifest = get_location_manifest(
        region_id="glenross_pub",
        authored=_authored(),
        snapshot=snapshot,
        store=store,
        save_id="default",
    )
    assert [e.id for e in manifest] == ["bar", "cobwebs", "overturned_table"]


def test_get_location_prose_no_overlay():
    snapshot = MagicMock()
    snapshot.encounter = None
    prose = get_location_prose(
        region_id="glenross_pub",
        authored_description="The pub door is ajar.",
        snapshot=snapshot,
    )
    assert prose == "The pub door is ajar."


def test_get_location_prose_appends_suffix():
    snapshot = MagicMock()
    snapshot.encounter = _enc_with_overlay("glenross_pub")
    prose = get_location_prose(
        region_id="glenross_pub",
        authored_description="The pub door is ajar.",
        snapshot=snapshot,
    )
    assert prose == ("The pub door is ajar.\n\nA chair lies in splinters by the door.")


def test_get_location_prose_empty_authored_with_overlay():
    snapshot = MagicMock()
    snapshot.encounter = _enc_with_overlay("glenross_pub")
    prose = get_location_prose(
        region_id="glenross_pub",
        authored_description="",
        snapshot=snapshot,
    )
    # Don't emit a leading double-newline when base is empty.
    assert prose == "A chair lies in splinters by the door."


def test_get_location_prose_overlay_with_empty_suffix():
    """Overlay carries entity_delta but no prose_suffix — base unchanged."""
    enc = _enc_with_overlay("glenross_pub")
    enc.location_overlay.prose_suffix = ""
    snapshot = MagicMock()
    snapshot.encounter = enc
    prose = get_location_prose(
        region_id="glenross_pub",
        authored_description="The pub door is ajar.",
        snapshot=snapshot,
    )
    assert prose == "The pub door is ajar."
