from sidequest.dungeon.tactical import RegionTactical, TacticalFeatureCell, TokenAnchor


def test_to_from_dict_round_trip():
    t = RegionTactical(
        region_id="exp001.r0",
        features=[TacticalFeatureCell("water", (2, 3), "black water")],
        anchors=[TokenAnchor((1, 1), "entrance"), TokenAnchor((3, 3), "creature")],
        pois=[(2, 3)],
        exit_thresholds={"entrance": (1, 1)},
    )
    restored = RegionTactical.from_dict(t.to_dict())
    assert restored == t


def test_to_dict_is_json_safe():
    import json

    t = RegionTactical(region_id="r", features=[TacticalFeatureCell("hazard", (0, 0), "x")])
    json.dumps(t.to_dict())  # must not raise
