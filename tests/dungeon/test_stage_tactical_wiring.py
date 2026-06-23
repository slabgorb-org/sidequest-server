"""Wiring test: _stage_tactical derives a record per filled region and the merge
helper rides it inside the per-region mask dict (ADR-096 token+feature)."""
from sidequest.dungeon.tactical import RegionTactical


def test_stage_tactical_produces_record_per_filled_region(tactical_fill_fixture):
    from sidequest.dungeon.materializer import _stage_tactical

    fixt = tactical_fill_fixture
    out = _stage_tactical(
        expansion=fixt["expansion"],
        graph=fixt["graph"],
        fill_result=fixt["fill_result"],
        curation=fixt["curation"],
        attach_result=fixt["attach_result"],
    )
    assert set(out.keys()) == set(fixt["fill_result"].keys())
    for region_id, rt in out.items():
        assert isinstance(rt, RegionTactical)
        assert rt.region_id == region_id


def test_persisted_mask_dict_carries_tactical(tactical_fill_fixture):
    from sidequest.dungeon.materializer import _stage_tactical, _tactical_into_mask_dicts

    fixt = tactical_fill_fixture
    tactical = _stage_tactical(
        expansion=fixt["expansion"],
        graph=fixt["graph"],
        fill_result=fixt["fill_result"],
        curation=fixt["curation"],
        attach_result=fixt["attach_result"],
    )
    mask_dicts = {rid: fr.mask.to_dict() for rid, fr in fixt["fill_result"].items() if fr.mask}
    merged = _tactical_into_mask_dicts(mask_dicts, tactical)
    for rid in merged:
        assert "tactical" in merged[rid]
        RegionTactical.from_dict(merged[rid]["tactical"])  # must parse


def test_hazard_setpieces_sourced_from_attach_reports(tactical_fill_fixture):
    """The set-piece-derived hazard source (spec §4.1) is the region's attach reports."""
    from sidequest.dungeon.materializer import _stage_tactical

    fixt = tactical_fill_fixture
    out = _stage_tactical(
        expansion=fixt["expansion"],
        graph=fixt["graph"],
        fill_result=fixt["fill_result"],
        curation=fixt["curation"],
        attach_result=fixt["attach_result"],
    )
    # fixture attaches one set-piece to exp001.r0 → at least one hazard feature there.
    assert any(f.feature_type == "hazard" for f in out["exp001.r0"].features)
