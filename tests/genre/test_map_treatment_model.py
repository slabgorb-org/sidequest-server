"""RED (spec §2, plan task 3): MapTreatmentConfig + MapProvenance models.

The model enforces STRUCTURE only (Literal-enum kind, types, ``extra="forbid"``)
and fails loud on a malformed map.yaml. Content completeness (raster requires
image + provenance; every region has an anchor) belongs to the pack validator
(story 163-3), never here — the model cannot see the sibling cartography.
"""

import pytest
from pydantic import ValidationError

from sidequest.genre.models.world import MapProvenance, MapTreatmentConfig


def test_raster_treatment_parses_full_shape() -> None:
    mt = MapTreatmentConfig.model_validate(
        {
            "treatment": "raster",
            "image": "os_one_inch_1900.jpg",
            "provenance": {
                "source": "Ordnance Survey One-Inch",
                "date": "1900",
                "archive": "NLS Map Images",
                "pd_basis": "Crown copyright expired",
            },
            "node_anchors": {"the_glenross_arms": [512, 340]},
            "style_hints": {"faction_layer": "default"},
        }
    )
    assert mt.treatment == "raster"
    assert mt.image == "os_one_inch_1900.jpg"
    assert mt.node_anchors["the_glenross_arms"] == [512, 340]
    assert isinstance(mt.provenance, MapProvenance)


def test_dag_treatment_needs_no_image_or_provenance() -> None:
    mt = MapTreatmentConfig.model_validate({"treatment": "dag"})
    assert mt.treatment == "dag"
    assert mt.image is None and mt.provenance is None


def test_unknown_treatment_kind_fails_loud() -> None:
    with pytest.raises(ValidationError):
        MapTreatmentConfig.model_validate({"treatment": "hologram"})


def test_extra_key_fails_loud() -> None:
    with pytest.raises(ValidationError):
        MapTreatmentConfig.model_validate({"treatment": "dag", "bogus": 1})


# --- Rule-enforcement tests (Argus, beyond the plan's happy-path set) --------


def test_provenance_forbids_extra_keys() -> None:
    """MapProvenance is a fail-loud PD-metadata block: an unknown key (a typo'd
    ``pd_bassis``) must not slip through silently (extra="forbid")."""
    with pytest.raises(ValidationError):
        MapProvenance.model_validate(
            {
                "source": "OS",
                "date": "1900",
                "archive": "NLS",
                "pd_basis": "expired",
                "pd_bassis": "typo",
            }
        )


def test_provenance_requires_all_four_fields() -> None:
    """Every PD scan must name source/date/archive/pd_basis — a missing field
    is a hard error, not an empty default."""
    with pytest.raises(ValidationError):
        MapProvenance.model_validate({"source": "OS", "date": "1900"})


def test_default_collections_are_not_shared_between_instances() -> None:
    """lang-review #2 (mutable defaults): two treatments must own independent
    node_anchors / style_hints dicts — mutating one may not leak into the other.
    """
    a = MapTreatmentConfig(treatment="dag")
    b = MapTreatmentConfig(treatment="dag")
    assert a.node_anchors is not b.node_anchors
    assert a.style_hints is not b.style_hints
    a.node_anchors["r1"] = [1, 2]
    a.style_hints["faction_layer"] = "x"
    assert b.node_anchors == {}
    assert b.style_hints == {}
