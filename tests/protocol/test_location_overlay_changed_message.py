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
    """Wiring: the dispatch table that decodes incoming messages must
    list LOCATION_OVERLAY_CHANGED → LocationOverlayChangedMessage. Without
    this entry the message is unaddressable from a client even though the
    class exists."""
    import sidequest.protocol.messages as messages_mod

    # Find the dispatch table by name patterns used by 54-2 + earlier stories.
    # The registry maps the enum string value to the message class.
    candidate_tables = []
    for name in dir(messages_mod):
        obj = getattr(messages_mod, name)
        if isinstance(obj, dict) and obj:
            sample = next(iter(obj.values()))
            if isinstance(sample, type) and name.lower().endswith(
                ("messages", "registry", "_map", "by_type")
            ):
                candidate_tables.append((name, obj))

    # Fall back to a source-text grep if no convention-named table is found —
    # the dispatch entry must exist somewhere in the messages module.
    if not candidate_tables:
        from pathlib import Path

        src = Path(messages_mod.__file__).read_text()
        assert '"LOCATION_OVERLAY_CHANGED": LocationOverlayChangedMessage' in src, (
            "expected dispatch registration "
            '"LOCATION_OVERLAY_CHANGED": LocationOverlayChangedMessage '
            "in sidequest/protocol/messages.py"
        )
        return

    matched = False
    for _, table in candidate_tables:
        if (
            "LOCATION_OVERLAY_CHANGED" in table
            and table["LOCATION_OVERLAY_CHANGED"] is LocationOverlayChangedMessage
        ):
            matched = True
            break
    assert matched, (
        "no dispatch table maps LOCATION_OVERLAY_CHANGED → "
        "LocationOverlayChangedMessage"
    )
