"""Cartography map-emit span — region-mode MAP_UPDATE visited overlay
(follow-up to ui #330 / ping-pong #329).

``cartography.map_emitted`` fires when ``_build_cartography_map_message``
produces a region-mode MAP_UPDATE payload. It carries the visited-region
census so the GM panel can verify the visited overlay is actually
populated from ``snapshot.discovered_regions`` (not improvised) and that
the filter-to-valid-regions cleanup is doing its job.

``discovered_total`` is the count of incoming ``discovered_regions``.
``visited_count`` is how many of those mapped to a REAL region slug and
made it into ``payload.explored``. ``dropped_count`` is the difference —
the scene-title pollution (e.g. ``"A Field of Blue Flowers, Munchkin
Country"``) the filter intentionally excluded. A high ``dropped_count``
relative to ``discovered_total`` is the dashboard signal that the
narrator is writing scene titles into ``discovered_regions`` rather than
canonical region slugs.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

from ._core import SPAN_ROUTES, SpanRoute
from .span import Span

SPAN_CARTOGRAPHY_MAP_EMITTED = "cartography.map_emitted"


SPAN_ROUTES[SPAN_CARTOGRAPHY_MAP_EMITTED] = SpanRoute(
    event_type="state_transition",
    component="region_state",
    extract=lambda span: {
        "field": "explored",
        "op": "map_emitted",
        "current_location": (span.attributes or {}).get("current_location", ""),
        "world_slug": (span.attributes or {}).get("world_slug", ""),
        "visited_count": (span.attributes or {}).get("visited_count", 0),
        "discovered_total": (span.attributes or {}).get("discovered_total", 0),
        "dropped_count": (span.attributes or {}).get("dropped_count", 0),
    },
)


SPAN_CARTOGRAPHY_CLUSTER_DETECTED = "sidequest.cartography.cluster_detected"


SPAN_ROUTES[SPAN_CARTOGRAPHY_CLUSTER_DETECTED] = SpanRoute(
    event_type="state_transition",
    component="region_state",
    extract=lambda span: {
        "field": "is_cluster",
        "op": "cluster_detected",
        "world": (span.attributes or {}).get("world", ""),
        "signal_source": (span.attributes or {}).get("signal_source", ""),
        "system_count": (span.attributes or {}).get("system_count", 0),
        "is_cluster": (span.attributes or {}).get("is_cluster", False),
    },
)


@contextmanager
def cluster_detected_span(
    *,
    world: str,
    signal_source: str,
    system_count: int,
    is_cluster: bool,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Single-vs-cluster decision for a space-opera world (Story 104-1 / M-A).

    The GM-panel lie-detector for the multi-system flag: it records ``world``,
    the ``signal_source`` that drove the count (``sector_graph`` /
    ``systems_dir`` / ``none``), the resolved ``system_count``, and the
    ``is_cluster`` result (``system_count > 1``). Fires on EVERY decision —
    cluster and single alike — so a misclassification is visible rather than a
    silent skip on the common single-system case.
    """
    attributes: dict[str, Any] = {
        "world": world,
        "signal_source": signal_source,
        "system_count": system_count,
        "is_cluster": is_cluster,
        **attrs,
    }
    with Span.open(
        SPAN_CARTOGRAPHY_CLUSTER_DETECTED,
        attributes,
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def cartography_map_emitted_span(
    *,
    current_location: str,
    world_slug: str,
    visited_count: int,
    discovered_total: int,
    dropped_count: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Region-mode MAP_UPDATE emit with the visited-overlay census.

    ``visited_count`` is the number of real regions written to
    ``payload.explored``; ``discovered_total`` is the raw incoming
    ``discovered_regions`` count; ``dropped_count`` is the pollution the
    valid-region filter excluded (``discovered_total - visited_count``,
    accounting for in-input duplicates).
    """
    attributes: dict[str, Any] = {
        "current_location": current_location,
        "world_slug": world_slug,
        "visited_count": visited_count,
        "discovered_total": discovered_total,
        "dropped_count": dropped_count,
        **attrs,
    }
    with Span.open(
        SPAN_CARTOGRAPHY_MAP_EMITTED,
        attributes,
        tracer_override=_tracer,
    ) as span:
        yield span


__all__ = [
    "SPAN_CARTOGRAPHY_CLUSTER_DETECTED",
    "SPAN_CARTOGRAPHY_MAP_EMITTED",
    "cartography_map_emitted_span",
    "cluster_detected_span",
]
