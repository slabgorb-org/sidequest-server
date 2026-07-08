"""RED (spec §4, plan task 6): _build_cartography_map_message populates
payload.treatment from World.map_treatment.

No map_treatment -> payload.treatment stays None (today's frame, dag fallback).
A raster treatment -> a CartographyTreatmentWire with the image path resolved
to a CDN/local URL via resolve_asset_url, plus anchors + style hints.
"""

from types import SimpleNamespace

from sidequest.genre.models.world import MapProvenance, MapTreatmentConfig
from sidequest.server.session_helpers import _build_cartography_map_message


def _pack_with_treatment(mt: MapTreatmentConfig | None):
    region = SimpleNamespace(name="The Glenross Arms", description="A pub", summary="", adjacent=[])
    cart = SimpleNamespace(
        navigation_mode="region",
        starting_region="the_glenross_arms",
        regions={"the_glenross_arms": region},
        routes=[],
        discovery_mode="public",
    )
    world = SimpleNamespace(cartography=cart, is_cluster=False, map_treatment=mt)
    return SimpleNamespace(worlds={"glenross": world})


def test_no_treatment_leaves_payload_treatment_none() -> None:
    pack = _pack_with_treatment(None)
    msg = _build_cartography_map_message(
        pack, "glenross", "the_glenross_arms", genre_slug="tea_and_murder"
    )
    assert msg is not None and msg.payload.treatment is None


def test_raster_treatment_populates_payload(monkeypatch) -> None:
    import sidequest.server.session_helpers as sh

    # raising=False: resolve_asset_url is added to session_helpers by task 6, so
    # in RED the name is absent — the no-op patch lets the test fail on the real
    # missing behavior (the genre_slug param / treatment build), not a setup error.
    monkeypatch.setattr(sh, "resolve_asset_url", lambda p, **k: f"https://cdn/{p}", raising=False)
    mt = MapTreatmentConfig(
        treatment="raster",
        image="sheet.jpg",
        provenance=MapProvenance(source="OS", date="1900", archive="NLS", pd_basis="expired"),
        node_anchors={"the_glenross_arms": [512, 340]},
        style_hints={"faction_layer": "default"},
    )
    pack = _pack_with_treatment(mt)
    msg = _build_cartography_map_message(
        pack, "glenross", "the_glenross_arms", genre_slug="tea_and_murder"
    )
    assert msg is not None
    t = msg.payload.treatment
    assert t is not None and t.kind == "raster"
    assert (
        t.image_url
        == "https://cdn/genre_packs/tea_and_murder/worlds/glenross/assets/maps/sheet.jpg"
    )
    assert t.node_anchors["the_glenross_arms"] == [512, 340]
    assert t.style_hints == {"faction_layer": "default"}
