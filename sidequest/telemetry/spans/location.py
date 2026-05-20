"""OTEL spans for the location subsystem. ADR-109 / Story 54-8.

Five state-transition spans:

- ``location.entity.resolve`` — every resolver call. Lie-detector flag
  set when ``mode=narrator_proactive`` AND ``resolved=False`` — the
  narrator's prose claimed something the manifest can't back.
- ``location.entity.minted`` — player-initiated mint of a new
  ``yes_and`` entity. Positive-canon flag.
- ``location.entity.promoted`` — authored ``flavor_only`` engaged
  mechanically and promoted to ``yes_and``. Positive-canon flag.
- ``location.overlay.activate`` / ``deactivate`` — encounter
  ``location_overlay`` state transitions.

All five are routed as ``state_transition`` events under
``component="location"`` so the GM panel renders them on the location
lane. The route extractor sets explicit boolean ``is_lie_detector`` and
``is_positive_canon`` fields so the UI can apply the colour rule
without re-deriving the logic from raw attributes.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

from ._core import SPAN_ROUTES, SpanRoute
from .span import Span

SPAN_LOCATION_ENTITY_RESOLVE = "location.entity.resolve"
SPAN_LOCATION_ENTITY_MINTED = "location.entity.minted"
SPAN_LOCATION_ENTITY_PROMOTED = "location.entity.promoted"
SPAN_LOCATION_OVERLAY_ACTIVATE = "location.overlay.activate"
SPAN_LOCATION_OVERLAY_DEACTIVATE = "location.overlay.deactivate"


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def _extract_resolve(span: Any) -> dict[str, Any]:
    attrs = span.attributes or {}
    mode = attrs.get("mode", "")
    resolved = attrs.get("resolved", False)
    is_lie_detector = (mode == "narrator_proactive") and (resolved is False)
    return {
        "field": "location_entity",
        "op": "entity_resolve",
        "region_id": attrs.get("region_id", ""),
        "label": attrs.get("label", ""),
        "mode": mode,
        "engagement_kind": attrs.get("engagement_kind", ""),
        "resolved": resolved,
        "mode_outcome": attrs.get("mode_outcome", ""),
        "from_promotion": attrs.get("from_promotion", False),
        "entity_id": attrs.get("entity_id", ""),
        "tier": attrs.get("tier", ""),
        "binding_kind": attrs.get("binding_kind", ""),
        "is_lie_detector": is_lie_detector,
    }


def _extract_minted(span: Any) -> dict[str, Any]:
    attrs = span.attributes or {}
    return {
        "field": "location_entity",
        "op": "entity_minted",
        "region_id": attrs.get("region_id", ""),
        "entity_id": attrs.get("entity_id", ""),
        "label": attrs.get("label", ""),
        "canon": attrs.get("canon", ""),
        "turn": attrs.get("turn", 0),
        "is_positive_canon": True,
    }


def _extract_promoted(span: Any) -> dict[str, Any]:
    attrs = span.attributes or {}
    return {
        "field": "location_entity",
        "op": "entity_promoted",
        "region_id": attrs.get("region_id", ""),
        "entity_id": attrs.get("entity_id", ""),
        "from_tier": attrs.get("from_tier", ""),
        "to_tier": attrs.get("to_tier", ""),
        "canon": attrs.get("canon", ""),
        "turn": attrs.get("turn", 0),
        "is_positive_canon": True,
    }


def _extract_overlay_activate(span: Any) -> dict[str, Any]:
    attrs = span.attributes or {}
    return {
        "field": "location_overlay",
        "op": "overlay_activate",
        "region_id": attrs.get("region_id", ""),
        "encounter_id": attrs.get("encounter_id", ""),
        "delta_count": attrs.get("delta_count", 0),
        "suffix_chars": attrs.get("suffix_chars", 0),
    }


def _extract_overlay_deactivate(span: Any) -> dict[str, Any]:
    attrs = span.attributes or {}
    return {
        "field": "location_overlay",
        "op": "overlay_deactivate",
        "region_id": attrs.get("region_id", ""),
        "encounter_id": attrs.get("encounter_id", ""),
        "delta_count": attrs.get("delta_count", 0),
        "suffix_chars": attrs.get("suffix_chars", 0),
    }


SPAN_ROUTES[SPAN_LOCATION_ENTITY_RESOLVE] = SpanRoute(
    event_type="state_transition",
    component="location",
    extract=_extract_resolve,
)
SPAN_ROUTES[SPAN_LOCATION_ENTITY_MINTED] = SpanRoute(
    event_type="state_transition",
    component="location",
    extract=_extract_minted,
)
SPAN_ROUTES[SPAN_LOCATION_ENTITY_PROMOTED] = SpanRoute(
    event_type="state_transition",
    component="location",
    extract=_extract_promoted,
)
SPAN_ROUTES[SPAN_LOCATION_OVERLAY_ACTIVATE] = SpanRoute(
    event_type="state_transition",
    component="location",
    extract=_extract_overlay_activate,
)
SPAN_ROUTES[SPAN_LOCATION_OVERLAY_DEACTIVATE] = SpanRoute(
    event_type="state_transition",
    component="location",
    extract=_extract_overlay_deactivate,
)


# ---------------------------------------------------------------------------
# Context-manager helpers
# ---------------------------------------------------------------------------


@contextmanager
def location_entity_resolve_span(
    *,
    region_id: str,
    label: str,
    mode: str,
    engagement_kind: str,
    resolved: bool,
    mode_outcome: str,
    from_promotion: bool,
    entity_id: str | None = None,
    tier: str | None = None,
    binding_kind: str | None = None,
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    """Fires on every ``resolve_location_entity`` call.

    ``mode=narrator_proactive`` with ``resolved=False`` is the lie-detector
    signal — the route extractor sets ``is_lie_detector=True`` so the GM
    panel can flag the row yellow.
    """
    attrs: dict[str, Any] = {
        "region_id": region_id,
        "label": label,
        "mode": mode,
        "engagement_kind": engagement_kind,
        "resolved": resolved,
        "mode_outcome": mode_outcome,
        "from_promotion": from_promotion,
    }
    if entity_id is not None:
        attrs["entity_id"] = entity_id
    if tier is not None:
        attrs["tier"] = tier
    if binding_kind is not None:
        attrs["binding_kind"] = binding_kind
    with Span.open(
        SPAN_LOCATION_ENTITY_RESOLVE, attrs, tracer_override=_tracer
    ) as span:
        yield span


@contextmanager
def location_entity_minted_span(
    *,
    region_id: str,
    entity_id: str,
    label: str,
    canon: str,
    turn: int,
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    """Fires when ``player_initiated`` mode mints a brand-new ``yes_and`` entity."""
    attrs: dict[str, Any] = {
        "region_id": region_id,
        "entity_id": entity_id,
        "label": label,
        "canon": canon,
        "turn": turn,
    }
    with Span.open(
        SPAN_LOCATION_ENTITY_MINTED, attrs, tracer_override=_tracer
    ) as span:
        yield span


@contextmanager
def location_entity_promoted_span(
    *,
    region_id: str,
    entity_id: str,
    from_tier: str,
    to_tier: str,
    canon: str,
    turn: int,
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    """Fires when an authored entity is promoted (flavor_only → yes_and)."""
    attrs: dict[str, Any] = {
        "region_id": region_id,
        "entity_id": entity_id,
        "from_tier": from_tier,
        "to_tier": to_tier,
        "canon": canon,
        "turn": turn,
    }
    with Span.open(
        SPAN_LOCATION_ENTITY_PROMOTED, attrs, tracer_override=_tracer
    ) as span:
        yield span


@contextmanager
def location_overlay_activate_span(
    *,
    region_id: str,
    encounter_id: str,
    delta_count: int,
    suffix_chars: int,
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    """Fires when an encounter ``location_overlay`` becomes live."""
    attrs: dict[str, Any] = {
        "region_id": region_id,
        "encounter_id": encounter_id,
        "delta_count": delta_count,
        "suffix_chars": suffix_chars,
    }
    with Span.open(
        SPAN_LOCATION_OVERLAY_ACTIVATE, attrs, tracer_override=_tracer
    ) as span:
        yield span


@contextmanager
def location_overlay_deactivate_span(
    *,
    region_id: str,
    encounter_id: str,
    delta_count: int,
    suffix_chars: int,
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    """Fires when an encounter ``location_overlay`` resolves/clears."""
    attrs: dict[str, Any] = {
        "region_id": region_id,
        "encounter_id": encounter_id,
        "delta_count": delta_count,
        "suffix_chars": suffix_chars,
    }
    with Span.open(
        SPAN_LOCATION_OVERLAY_DEACTIVATE, attrs, tracer_override=_tracer
    ) as span:
        yield span
