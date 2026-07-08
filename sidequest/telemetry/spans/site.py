"""Site seam spans (Track B, Story 164-2).

Every helper mirrors to ``turn_telemetry`` via ``publish_event`` AFTER the span
closes — ``Span.open`` alone reaches Jaeger + the live GM dashboard but NOT the
Postgres sink (see ``spans/movement.py:_mirror_movement_span_to_sink``). A site
seam that opened a span without mirroring would read as DEAD in the GM panel /
saves, exactly the movement-crossing failure class this pattern was written to
close.

The mirror reuses the SAME SPAN_ROUTES ``extract`` the GM dashboard reads, so
there is no field drift. It SKIPS a non-recording span (no
``TracerProvider`` installed → the span exposes no ``attributes``): the span
wraps the LIVE seam resolver, and a telemetry side-channel must never crash the
player's turn.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from sidequest.telemetry.watcher_hub import publish_event

from ._core import SPAN_ROUTES, SpanRoute
from .span import Span

SPAN_SITE_ENTER = "site.enter"
SPAN_SITE_EXIT = "site.exit"
SPAN_SITE_ENTER_UNRESOLVED = "site.enter_unresolved"


def _attr(field: str):
    return lambda span, f=field: (span.attributes or {}).get(f)


SPAN_ROUTES[SPAN_SITE_ENTER] = SpanRoute(
    event_type="state_transition",
    component="sites",
    extract=lambda s: {
        "field": "pc_regions",
        "op": "site.enter",
        "pc_name": _attr("pc_name")(s),
        "site_id": _attr("site_id")(s),
        "from_region": _attr("from_region")(s),
        "to_region": _attr("to_region")(s),
        "resolved_via": _attr("resolved_via")(s),
        "extent": _attr("extent")(s),
    },
)

SPAN_ROUTES[SPAN_SITE_EXIT] = SpanRoute(
    event_type="state_transition",
    component="sites",
    extract=lambda s: {
        "field": "pc_regions",
        "op": "site.exit",
        "pc_name": _attr("pc_name")(s),
        "site_id": _attr("site_id")(s),
        "from_region": _attr("from_region")(s),
        "to_region": _attr("to_region")(s),
        "resolved_via": _attr("resolved_via")(s),
    },
)

SPAN_ROUTES[SPAN_SITE_ENTER_UNRESOLVED] = SpanRoute(
    event_type="state_transition",
    component="sites",
    extract=lambda s: {
        "field": "pc_regions",
        "op": "site.enter_unresolved",
        "pc_name": _attr("pc_name")(s),
        "from_region": _attr("from_region")(s),
        "reason": _attr("reason")(s),
        "descriptor": _attr("descriptor")(s),
    },
)


def _mirror(span_name: str, span: trace.Span) -> None:
    """Mirror a finished site span into the turn_telemetry DB sink.

    Skips a non-recording span (no ``attributes`` to read) — the mirror wraps
    the live seam resolver and must never raise out of it.
    """
    route = SPAN_ROUTES.get(span_name)
    if route is None or not hasattr(span, "attributes"):
        return
    publish_event(route.event_type, route.extract(span), component=route.component)


@contextmanager
def site_enter_span(
    *,
    pc_name: str,
    site_id: str,
    from_region: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_SITE_ENTER,
        {"pc_name": pc_name, "site_id": site_id, "from_region": from_region, **attrs},
        tracer_override=_tracer,
    ) as span:
        yield span
    _mirror(SPAN_SITE_ENTER, span)


@contextmanager
def site_exit_span(
    *,
    pc_name: str,
    site_id: str,
    from_region: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_SITE_EXIT,
        {"pc_name": pc_name, "site_id": site_id, "from_region": from_region, **attrs},
        tracer_override=_tracer,
    ) as span:
        yield span
    _mirror(SPAN_SITE_EXIT, span)


@contextmanager
def site_enter_unresolved_span(
    *,
    pc_name: str,
    from_region: str,
    reason: str,
    descriptor: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_SITE_ENTER_UNRESOLVED,
        {
            "pc_name": pc_name,
            "from_region": from_region,
            "reason": reason,
            "descriptor": descriptor,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        span.set_status(Status(StatusCode.ERROR, reason))
        yield span
    _mirror(SPAN_SITE_ENTER_UNRESOLVED, span)


__all__ = [
    "SPAN_SITE_ENTER",
    "SPAN_SITE_ENTER_UNRESOLVED",
    "SPAN_SITE_EXIT",
    "site_enter_span",
    "site_enter_unresolved_span",
    "site_exit_span",
]
