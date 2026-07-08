"""RED (spec §4, plan task 5): CartographyTreatmentWire + the optional
``treatment`` field on CartographyMapPayload.

An additive, declared, typed field on the existing MAP_UPDATE payload — absent
by default (None), so a world with no map.yaml ships exactly today's frame and
the UI keeps its d3-dag fallback.
"""

from sidequest.protocol.messages import (
    CartographyMapMessage,
    CartographyMapPayload,
    CartographyTreatmentWire,
)


def test_payload_serializes_with_treatment() -> None:
    msg = CartographyMapMessage(
        payload=CartographyMapPayload(
            current_location="the_glenross_arms",
            treatment=CartographyTreatmentWire(
                kind="raster",
                image_url="https://cdn.example/sheet.jpg",
                node_anchors={"the_glenross_arms": [512, 340]},
                style_hints={"faction_layer": "default"},
            ),
        )
    )
    dumped = msg.model_dump(mode="json")
    assert dumped["type"] == "MAP_UPDATE"
    assert dumped["payload"]["treatment"]["kind"] == "raster"
    assert dumped["payload"]["treatment"]["node_anchors"]["the_glenross_arms"] == [512, 340]


def test_treatment_defaults_none() -> None:
    p = CartographyMapPayload(current_location="x")
    assert p.treatment is None
