"""OTEL spans for the location subsystem (Story 54-8 / ADR-109).

Five state-transition spans under ``component="location"``:

* ``location.entity.resolve`` — every resolver call. Route extractor
  sets ``is_lie_detector=True`` only when ``mode="narrator_proactive"
  AND resolved=False`` (the narrator referenced something the manifest
  can't back); ``False`` in every other case (explicit boolean both ways,
  no absent-key fallback per CLAUDE.md "no silent fallbacks").
* ``location.entity.minted`` — player_initiated mint. Positive-canon.
* ``location.entity.promoted`` — flavor_only → yes_and on mechanical
  engagement. Positive-canon.
* ``location.overlay.activate`` / ``.deactivate`` — encounter
  ``location_overlay`` lifecycle transitions.
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.telemetry.spans import (
    FLAT_ONLY_SPANS,
    SPAN_LOCATION_ENTITY_MINTED,
    SPAN_LOCATION_ENTITY_PROMOTED,
    SPAN_LOCATION_ENTITY_RESOLVE,
    SPAN_LOCATION_OVERLAY_ACTIVATE,
    SPAN_LOCATION_OVERLAY_DEACTIVATE,
    SPAN_ROUTES,
    location_entity_minted_span,
    location_entity_promoted_span,
    location_entity_resolve_span,
    location_overlay_activate_span,
    location_overlay_deactivate_span,
)


@pytest.fixture
def exporter(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    """Install a per-test in-memory exporter. Mirrors the pattern in
    ``tests/server/test_opposed_check_wiring.py`` — monkeypatching
    ``sidequest.telemetry.spans.tracer`` keeps the global tracer provider
    untouched."""
    from sidequest.telemetry import spans as spans_module

    exp = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    test_tracer = provider.get_tracer("test")
    monkeypatch.setattr(spans_module, "tracer", lambda: test_tracer)
    return exp


# ---------------------------------------------------------------------------
# Constants + route registration
# ---------------------------------------------------------------------------


def test_constants_have_canonical_names() -> None:
    assert SPAN_LOCATION_ENTITY_RESOLVE == "location.entity.resolve"
    assert SPAN_LOCATION_ENTITY_MINTED == "location.entity.minted"
    assert SPAN_LOCATION_ENTITY_PROMOTED == "location.entity.promoted"
    assert SPAN_LOCATION_OVERLAY_ACTIVATE == "location.overlay.activate"
    assert SPAN_LOCATION_OVERLAY_DEACTIVATE == "location.overlay.deactivate"


def test_every_location_span_is_routed() -> None:
    """All five constants are routed as state_transition under component=location.

    Membership in ``FLAT_ONLY_SPANS`` would mean the GM panel never sees a
    typed event for the span — that's the wrong choice for the lie-detector
    surface, so the route must exist and the flat-only set must be clear.
    """
    for span_name in (
        SPAN_LOCATION_ENTITY_RESOLVE,
        SPAN_LOCATION_ENTITY_MINTED,
        SPAN_LOCATION_ENTITY_PROMOTED,
        SPAN_LOCATION_OVERLAY_ACTIVATE,
        SPAN_LOCATION_OVERLAY_DEACTIVATE,
    ):
        assert span_name in SPAN_ROUTES, f"{span_name} missing from SPAN_ROUTES"
        assert span_name not in FLAT_ONLY_SPANS
        route = SPAN_ROUTES[span_name]
        assert route.component == "location"
        assert route.event_type == "state_transition"


# ---------------------------------------------------------------------------
# resolve helper
# ---------------------------------------------------------------------------


def test_resolve_span_records_attributes(exporter: InMemorySpanExporter) -> None:
    with location_entity_resolve_span(
        region_id="glenross_pub",
        label="the bar",
        mode="narrator_proactive",
        engagement_kind="mechanical",
        resolved=True,
        mode_outcome="matched",
        from_promotion=False,
        entity_id="bar",
        tier="real_object",
        binding_kind="location_feature",
    ):
        pass

    finished = exporter.get_finished_spans()
    assert len(finished) == 1, f"expected 1 span, got {len(finished)}"
    span = finished[0]
    assert span.name == "location.entity.resolve"
    attrs = span.attributes or {}
    assert attrs["region_id"] == "glenross_pub"
    assert attrs["label"] == "the bar"
    assert attrs["mode"] == "narrator_proactive"
    assert attrs["engagement_kind"] == "mechanical"
    assert attrs["resolved"] is True
    assert attrs["mode_outcome"] == "matched"
    assert attrs["from_promotion"] is False
    assert attrs["entity_id"] == "bar"
    assert attrs["tier"] == "real_object"
    assert attrs["binding_kind"] == "location_feature"


def test_resolve_route_flags_lie_detector_on_proactive_miss(
    exporter: InMemorySpanExporter,
) -> None:
    """The lie-detector signal: narrator referenced something not in the
    manifest. GM panel paints this row amber."""
    with location_entity_resolve_span(
        region_id="glenross_pub",
        label="the dragon",
        mode="narrator_proactive",
        engagement_kind="mechanical",
        resolved=False,
        mode_outcome="no_match",
        from_promotion=False,
    ):
        pass
    [span] = exporter.get_finished_spans()
    fields = SPAN_ROUTES["location.entity.resolve"].extract(span)
    assert fields["is_lie_detector"] is True
    assert fields["mode"] == "narrator_proactive"
    assert fields["resolved"] is False


def test_resolve_route_does_not_flag_lie_detector_on_player_miss(
    exporter: InMemorySpanExporter,
) -> None:
    """``player_initiated resolved=False`` is a positive-canon mint about to
    happen, NOT a lie. ``is_lie_detector`` is explicitly ``False`` (no
    silent absence)."""
    with location_entity_resolve_span(
        region_id="glenross_pub",
        label="the antique sextant",
        mode="player_initiated",
        engagement_kind="mention",
        resolved=False,
        mode_outcome="no_match",
        from_promotion=False,
    ):
        pass
    [span] = exporter.get_finished_spans()
    fields = SPAN_ROUTES["location.entity.resolve"].extract(span)
    # Explicit False, never absent — the UI must not have to infer
    # absence vs negative.
    assert "is_lie_detector" in fields
    assert fields["is_lie_detector"] is False


def test_resolve_route_does_not_flag_lie_detector_on_proactive_match(
    exporter: InMemorySpanExporter,
) -> None:
    """``narrator_proactive resolved=True`` is the happy path. Not a lie."""
    with location_entity_resolve_span(
        region_id="glenross_pub",
        label="the bar",
        mode="narrator_proactive",
        engagement_kind="mention",
        resolved=True,
        mode_outcome="matched",
        from_promotion=False,
        entity_id="bar",
        tier="real_object",
    ):
        pass
    [span] = exporter.get_finished_spans()
    fields = SPAN_ROUTES["location.entity.resolve"].extract(span)
    assert "is_lie_detector" in fields
    assert fields["is_lie_detector"] is False


# ---------------------------------------------------------------------------
# minted helper
# ---------------------------------------------------------------------------


def test_minted_span_records_attributes(exporter: InMemorySpanExporter) -> None:
    with location_entity_minted_span(
        region_id="glenross_pub",
        entity_id="the_antique_sextant",
        label="the antique sextant",
        canon="A brass sextant on the bartop.",
        turn=7,
    ):
        pass
    [span] = exporter.get_finished_spans()
    assert span.name == "location.entity.minted"
    attrs = span.attributes or {}
    assert attrs["region_id"] == "glenross_pub"
    assert attrs["entity_id"] == "the_antique_sextant"
    assert attrs["label"] == "the antique sextant"
    assert attrs["canon"] == "A brass sextant on the bartop."
    assert attrs["turn"] == 7
    fields = SPAN_ROUTES["location.entity.minted"].extract(span)
    assert fields["op"] == "entity_minted"
    assert fields["is_positive_canon"] is True


# ---------------------------------------------------------------------------
# promoted helper
# ---------------------------------------------------------------------------


def test_promoted_span_records_attributes(exporter: InMemorySpanExporter) -> None:
    with location_entity_promoted_span(
        region_id="glenross_pub",
        entity_id="cobwebs",
        from_tier="flavor_only",
        to_tier="yes_and",
        canon="cobwebs",
        turn=11,
    ):
        pass
    [span] = exporter.get_finished_spans()
    assert span.name == "location.entity.promoted"
    attrs = span.attributes or {}
    assert attrs["region_id"] == "glenross_pub"
    assert attrs["entity_id"] == "cobwebs"
    assert attrs["from_tier"] == "flavor_only"
    assert attrs["to_tier"] == "yes_and"
    assert attrs["turn"] == 11
    fields = SPAN_ROUTES["location.entity.promoted"].extract(span)
    assert fields["op"] == "entity_promoted"
    assert fields["is_positive_canon"] is True


# ---------------------------------------------------------------------------
# overlay helpers
# ---------------------------------------------------------------------------


def test_overlay_activate_span_records_attributes(
    exporter: InMemorySpanExporter,
) -> None:
    with location_overlay_activate_span(
        region_id="glenross_pub",
        encounter_id="tavern_brawl@glenross_pub",
        delta_count=1,
        suffix_chars=42,
    ):
        pass
    [span] = exporter.get_finished_spans()
    assert span.name == "location.overlay.activate"
    attrs = span.attributes or {}
    assert attrs["region_id"] == "glenross_pub"
    assert attrs["encounter_id"] == "tavern_brawl@glenross_pub"
    assert attrs["delta_count"] == 1
    assert attrs["suffix_chars"] == 42
    fields = SPAN_ROUTES["location.overlay.activate"].extract(span)
    assert fields["op"] == "overlay_activate"
    assert fields["region_id"] == "glenross_pub"


def test_overlay_deactivate_span_records_attributes(
    exporter: InMemorySpanExporter,
) -> None:
    with location_overlay_deactivate_span(
        region_id="glenross_pub",
        encounter_id="tavern_brawl@glenross_pub",
        delta_count=0,
        suffix_chars=0,
    ):
        pass
    [span] = exporter.get_finished_spans()
    assert span.name == "location.overlay.deactivate"
    attrs = span.attributes or {}
    assert attrs["region_id"] == "glenross_pub"
    assert attrs["delta_count"] == 0
    assert attrs["suffix_chars"] == 0
    fields = SPAN_ROUTES["location.overlay.deactivate"].extract(span)
    assert fields["op"] == "overlay_deactivate"
