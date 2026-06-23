from sidequest.protocol.models import TacticalFeature, TacticalGridPayload


def test_tactical_feature_serializes_round_trip():
    f = TacticalFeature(feature_type="water", cell=(3, 4), label="knee-deep black water")
    dumped = f.model_dump()
    assert dumped == {"feature_type": "water", "cell": [3, 4], "label": "knee-deep black water"}


def test_payload_features_default_empty_and_omitted():
    # Additive: existing cavern payloads with no features serialize without the key
    # (ProtocolBase omits empty-list defaults).
    p = TacticalGridPayload(room_id="r0", room_name="r0", room_type="cavern")
    assert p.features == []
    assert "features" not in p.model_dump()


def test_payload_carries_features_when_present():
    p = TacticalGridPayload(
        room_id="r0",
        room_name="r0",
        room_type="cavern",
        features=[TacticalFeature(feature_type="hazard", cell=(1, 2), label="loose ceiling")],
    )
    dumped = p.model_dump()
    assert dumped["features"] == [{"feature_type": "hazard", "cell": [1, 2], "label": "loose ceiling"}]
