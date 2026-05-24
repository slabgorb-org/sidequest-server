"""WIRING: the public contract oq-1's materializer invokes. Real bundle,
real content, real signature. Not mocked. (spec §9, CLAUDE.md.)"""

from __future__ import annotations

import inspect
from pathlib import Path

from sidequest.game.cookbook import assemble_region, load_cookbook, validate_bundle

WORLD = (
    Path(__file__).parents[3]
    / "sidequest-content/genre_packs/caverns_and_claudes/worlds/beneath_sunden"
)


def test_public_contract_signature_is_stable() -> None:
    sig = inspect.signature(assemble_region)
    params = list(sig.parameters)
    # bundle is positional; the rest are the named oq-1 contract inputs.
    assert params[0] == "bundle"
    assert set(params[1:]) == {
        "campaign_seed",
        "expansion_id",
        "depth_score",
        "burst_magnitude",
        "look",
        "is_first_band_entry",
        # Story 55-1 / ADR-109: per-room deterministic seed.
        "room_id",
        # Reference Pages v2 / Task 9: threaded through to compose_room_prose
        # so LocationEntity.reference_url can resolve at construction time.
        "pack_id",
        "world_slug",
    }


def test_real_bundle_validates_and_assembles() -> None:
    bundle = load_cookbook(WORLD)
    validate_bundle(bundle)  # spec §7 gates pass on shipped content
    man = assemble_region(
        bundle,
        campaign_seed="wire",
        expansion_id="w1",
        depth_score=0.5,
        burst_magnitude=3,
        look="necropolis",
        is_first_band_entry=False,
        room_id="wiring_test_region",
    )
    # oq-1 consumes this dict shape (then does CR→Edge at its seam).
    payload = man.model_dump()
    assert set(payload) == {
        "race",
        "cr_band",
        "size_budget",
        "wandering_table",
        "loot_table",
        "special_rooms",
        "big_bad",
        # Story 55-1: cookbook-composed per-region prose + manifest.
        "room_descriptions",
    }
    for row in payload["wandering_table"]:
        assert {"name", "cr", "xp", "type", "weight", "count", "telegraph"} <= set(row)
