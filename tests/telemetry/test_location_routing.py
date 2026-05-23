"""Focused routing check for the ``SPAN_LOCATION_*`` family (Story 54-8).

The repo-wide ``test_routing_completeness.py`` already covers every
``SPAN_*`` constant. This test exists so a future engineer touching only
the location telemetry can run a tight, named check without depending on
unrelated routes — and so it fails loudly with a location-specific
message if any future location span constant is added without a route.
"""

from __future__ import annotations

from sidequest.telemetry import spans
from sidequest.telemetry.spans import FLAT_ONLY_SPANS, SPAN_ROUTES


def _location_span_constants() -> set[str]:
    return {
        v
        for name, v in vars(spans).items()
        if name.startswith("SPAN_LOCATION_") and isinstance(v, str)
    }


def test_all_span_location_constants_are_routed() -> None:
    constants = _location_span_constants()
    # Sanity: the family exists. If this fails, the location span module
    # never imported (probably from .location was forgotten in __init__).
    assert constants, "expected SPAN_LOCATION_* constants to exist"
    missing = constants - set(SPAN_ROUTES) - set(FLAT_ONLY_SPANS)
    assert not missing, f"unrouted location spans: {sorted(missing)}"


def test_location_routes_target_state_transition_under_location_component() -> None:
    """Every ``location.*`` route is a state_transition under ``location``.

    If a future span needs a different event_type, weigh whether splitting
    it out of the family is the cleaner decision — this lint enforces the
    family invariant deliberately so renames don't sneak past review.
    """
    found = False
    for name, route in SPAN_ROUTES.items():
        if not name.startswith("location."):
            continue
        found = True
        assert route.event_type == "state_transition", (
            f"{name} routes to event_type={route.event_type!r}; expected 'state_transition'"
        )
        assert route.component == "location", (
            f"{name} routes to component={route.component!r}; expected 'location'"
        )
    assert found, "no location.* routes registered — location.py import missing?"
