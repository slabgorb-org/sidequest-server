"""RED tests for Story 63-8 AC-6 — pf-validate-locations POI-image slug check.

Fixtures only (no live packs). The new check the story asks for: a
``history.yaml`` POI manifest entry (``chapters[].points_of_interest[].slug``)
must correspond to a renderable location (a ``locations.yaml`` location slug or
a ``cartography.yaml`` region key) — otherwise the generated landscape image
can never attach to a card. A dangling POI slug is a loud finding, not silence.

Deterministic slug-consistency only. The "the R2 object actually exists" half
of AC-6 needs a defined mechanism (HTTP HEAD vs local mirror) and is captured
as a delivery finding rather than prescribed here.
"""

from __future__ import annotations

from pathlib import Path

from sidequest.cli.validate.locations import validate_locations_in_world

_WORLDS = Path(__file__).parent.parent / "fixtures" / "packs" / "reference_v2_fixture" / "worlds"
_POI_WORLD = _WORLDS / "poi_fixture"
_ORPHAN_WORLD = _WORLDS / "poi_orphan_fixture"


def test_validator_flags_history_poi_slug_with_no_matching_location() -> None:
    """history POI 'ghost-station' matches no location in poi_orphan_fixture —
    the validator must record an issue naming the dangling slug."""
    result = validate_locations_in_world(_ORPHAN_WORLD)
    issues = result.errors + result.warnings
    assert any("ghost-station" in i.message for i in issues), (
        "validator should flag the history POI slug that maps to no location "
        f"(errors={[i.message for i in result.errors]}, "
        f"warnings={[i.message for i in result.warnings]})"
    )


def test_validator_clean_when_poi_slug_matches_location() -> None:
    """history POI 'vaskov-centrum' matches a location in poi_fixture — the
    validator must NOT raise a POI-slug error for it."""
    result = validate_locations_in_world(_POI_WORLD)
    assert not any(
        "vaskov-centrum" in i.message for i in result.errors
    ), f"matched POI slug should not be an error: {[i.message for i in result.errors]}"
