"""LOCATION_OVERLAY_CHANGED message + payload (Story 54-7)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.protocol.enums import MessageType
from sidequest.protocol.messages import LocationOverlayChangedMessage
from sidequest.protocol.models import (
    LocationDescriptionOverlaySummary,
    LocationOverlayChangedPayload,
)


def test_payload_minimum():
    payload = LocationOverlayChangedPayload(
        region_id="glenross_pub",
        overlays=[],
    )
    assert payload.region_id == "glenross_pub"
    assert payload.overlays == []


def test_payload_with_overlay_summary():
    payload = LocationOverlayChangedPayload(
        region_id="glenross_pub",
        overlays=[
            LocationDescriptionOverlaySummary(
                encounter_id="tavern_brawl-7",
                prose_suffix="A chair lies in splinters by the door.",
                entity_delta_count=1,
            ),
        ],
    )
    assert len(payload.overlays) == 1
    assert payload.overlays[0].encounter_id == "tavern_brawl-7"


def test_payload_blank_region_id_rejected():
    with pytest.raises(ValidationError):
        LocationOverlayChangedPayload(region_id="", overlays=[])


def test_payload_extra_field_rejected():
    with pytest.raises(ValidationError):
        LocationOverlayChangedPayload(  # type: ignore[call-arg]
            region_id="x", overlays=[], surprise="!"
        )


def test_message_roundtrip():
    msg = LocationOverlayChangedMessage(
        payload=LocationOverlayChangedPayload(
            region_id="glenross_pub",
            overlays=[
                LocationDescriptionOverlaySummary(
                    encounter_id="tavern_brawl-7",
                    prose_suffix="",
                    entity_delta_count=2,
                ),
            ],
        ),
        player_id="",
    )
    assert msg.type == MessageType.LOCATION_OVERLAY_CHANGED
    dumped = msg.model_dump()
    assert dumped["type"] == "LOCATION_OVERLAY_CHANGED"
    assert dumped["payload"]["region_id"] == "glenross_pub"
    assert dumped["payload"]["overlays"][0]["entity_delta_count"] == 2


def test_dispatch_registry_includes_location_overlay_changed():
    """Wiring: the GameMessage discriminated union must include
    LocationOverlayChangedMessage so the type=LOCATION_OVERLAY_CHANGED
    wire payload decodes to the right class.

    Post-port this codebase uses a pydantic discriminated union
    (``Field(discriminator="type")``) instead of a dict registry — that
    IS the dispatch.
    """
    from sidequest.protocol.messages import GameMessage

    msg_in = LocationOverlayChangedMessage(
        payload=LocationOverlayChangedPayload(region_id="glenross_pub", overlays=[]),
        player_id="",
    )
    wire = GameMessage(root=msg_in).to_json()
    decoded = GameMessage.parse_json(wire)
    assert isinstance(decoded.root, LocationOverlayChangedMessage)
    assert decoded.root.type == MessageType.LOCATION_OVERLAY_CHANGED
    assert decoded.root.payload.region_id == "glenross_pub"
