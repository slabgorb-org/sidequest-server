"""Movement subsystem spans (Movement Subsystem §OTEL Contract).

The GM panel is the lie detector: movement must be provably engaged
vs. improvised. ``movement.resolved`` (INFO) fires once per PC who
actually moved through a real graph adjacency; ``movement.unresolved``
(ERROR) fires once per PC whose coarse intent could not be resolved to
a real adjacency (fail-loud, never a silent stay-put or invented
region).

Both spans are PER-PC — every span carries ``pc_name`` so the panel
sees each PC's move independently (split-party legibility). They match
the ``frontier_region_transition_span`` context-manager shape in
``dungeon_materialize.py``.
"""

from __future__ import annotations

import json as _json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from sidequest.telemetry.watcher_hub import publish_event

from ._core import SPAN_ROUTES, SpanRoute
from .span import Span

# ---------------------------------------------------------------------------
# Span name constants
# ---------------------------------------------------------------------------

SPAN_MOVEMENT_RESOLVED = "movement.resolved"
SPAN_MOVEMENT_UNRESOLVED = "movement.unresolved"

# A region-mode world (cartography navigation_mode == region, e.g.
# wry_whimsy/oz) has NO procedural DungeonStore by design — travel is
# resolved by the narration_apply heading→region path (#577), not this
# procedural-dungeon navigator. ``movement.region_mode`` (INFO, NON-error)
# fires when the movement subsystem recognizes the mode and defers cleanly,
# so the GM panel shows movement engaged-and-deferred instead of a false
# ERROR (``movement.unresolved`` reason=no_dungeon_store) on every move.
SPAN_MOVEMENT_REGION_MODE = "movement.region_mode"

# Story 71-15 (ADR-055): per-transition trope progression on room-graph
# traversal. The single per-turn trope advance is done by the trope engine
# (tick_tropes); this span correlates that progression with the movement
# that earned it so the GM panel sees the dungeon clock turning on descent.
SPAN_ROOM_TRANSITION_TICK = "room.transition_tick"

# Story 153-24 (ADR-055): the persisted room axis advanced. The region axis
# (discovered_regions / current_region) already emits ``dungeon.map_emitted``;
# this span proves the ROOM axis (discovered_rooms / room_states /
# Character.current_room) was written on a room-graph transition — without it
# forensics/reload saw an empty dungeon. ``newly_discovered`` distinguishes
# genuine exploration (first entry) from backtracking (re-entry of a known
# room) so the GM panel can tell them apart.
SPAN_ROOM_DISCOVERED = "room.discovered"

# ---------------------------------------------------------------------------
# Routing registrations
# ---------------------------------------------------------------------------


def _attr(field: str):
    return lambda span, f=field: (span.attributes or {}).get(f)


SPAN_ROUTES[SPAN_MOVEMENT_RESOLVED] = SpanRoute(
    event_type="state_transition",
    component="movement",
    extract=lambda s: {
        "field": "pc_regions",
        "op": "movement.resolved",
        "pc_name": _attr("pc_name")(s),
        "from_region": _attr("from_region")(s),
        "to_region": _attr("to_region")(s),
        "intent.direction": _attr("intent.direction")(s),
        "intent.exit_descriptor": _attr("intent.exit_descriptor")(s),
        "resolved_via": _attr("resolved_via")(s),
        "candidate_exits": _attr("candidate_exits")(s),
        "edge_kind": _attr("edge_kind")(s),
        "target_pre_materialized": _attr("target_pre_materialized")(s),
        "materialize_triggered": _attr("materialize_triggered")(s),
        "party_split_after": _attr("party_split_after")(s),
    },
)

SPAN_ROUTES[SPAN_MOVEMENT_UNRESOLVED] = SpanRoute(
    event_type="state_transition",
    component="movement",
    extract=lambda s: {
        "field": "pc_regions",
        "op": "movement.unresolved",
        "pc_name": _attr("pc_name")(s),
        "reason": _attr("reason")(s),
        "from_region": _attr("from_region")(s),
        "intent.direction": _attr("intent.direction")(s),
        "intent.exit_descriptor": _attr("intent.exit_descriptor")(s),
        "available_exits": _attr("available_exits")(s),
    },
)

SPAN_ROUTES[SPAN_MOVEMENT_REGION_MODE] = SpanRoute(
    event_type="state_transition",
    component="movement",
    extract=lambda s: {
        "field": "pc_regions",
        "op": "movement.region_mode",
        "pc_name": _attr("pc_name")(s),
        "from_region": _attr("from_region")(s),
        "world_slug": _attr("world_slug")(s),
        "intent.direction": _attr("intent.direction")(s),
        "intent.exit_descriptor": _attr("intent.exit_descriptor")(s),
    },
)

SPAN_ROUTES[SPAN_ROOM_TRANSITION_TICK] = SpanRoute(
    event_type="state_transition",
    component="tropes",
    extract=lambda s: {
        "field": "active_tropes",
        "op": "room_transition_tick",
        "advanced_tropes": _attr("advanced_tropes_json")(s),
        "from_room": _attr("from_room")(s),
        "to_room": _attr("to_room")(s),
        "pc_name": _attr("pc_name")(s),
    },
)

SPAN_ROUTES[SPAN_ROOM_DISCOVERED] = SpanRoute(
    event_type="state_transition",
    component="movement",
    extract=lambda s: {
        "field": "discovered_rooms",
        "op": "room.discovered",
        "room_id": _attr("room_id")(s),
        "newly_discovered": _attr("newly_discovered")(s),
        "discovered_count": _attr("discovered_count")(s),
        "character": _attr("character")(s),
    },
)

# ---------------------------------------------------------------------------
# DB-sink mirror helper
# ---------------------------------------------------------------------------


def _mirror_movement_span_to_sink(span_name: str, span: trace.Span) -> None:
    """Mirror a finished movement span into the turn_telemetry DB sink.

    Movement spans reach Jaeger via Span.open but never the turn_telemetry
    Postgres sink — a firing engine reads as DEAD in saves (the 2026-06-22 root
    cause behind 8 failed crossing attempts). EVERY movement emit site flows
    through the three context managers below — the subsystem navigator, the
    region-mode defer, the unresolved path, narration_apply's movement, AND both
    seam resolvers (deep_descent = the real ropefoot->entrance crossing,
    surface_ascent = the reverse) — so mirroring HERE covers all of them with no
    per-site edits and no field drift: we reuse the SAME SPAN_ROUTES extract the
    GM-panel dashboard uses. tx defaults to None -> the out-of-frame sink (NULL
    event_seq); movement fires in the ADR-113 engine-first pass, before the turn
    event frame, so there is no SaveTransaction to ride.
    """
    route = SPAN_ROUTES.get(span_name)
    if route is None:
        return
    # A NonRecordingSpan — produced when no recording TracerProvider is
    # installed: a process that never called ``init_tracer`` (the realistic
    # case — tests, or any non-telemetry context). A *sampling* drop is NOT a
    # realistic cause here: ``init_tracer`` (telemetry/setup.py) leaves the SDK
    # default sampler in place (``ParentBased(ALWAYS_ON)``) and movement spans
    # are roots with no remote parent, so they are always recorded unless an
    # external ``OTEL_TRACES_SAMPLER`` env override is deliberately configured.
    # Such a span exposes no ``attributes`` for ``route.extract`` to read, so
    # reading them raises ``AttributeError``. That exception must NEVER
    # propagate: this mirror is a telemetry side-channel wrapped around the
    # movement dispatch, and a non-recording span carries nothing to mirror.
    # Skip silently (No Silent Fallbacks does not apply — there is no
    # alternative path being masked; the recording-span path is unchanged, and
    # a telemetry helper crashing a region advance is the 158-7 tail we are
    # removing). In production ``init_tracer`` runs at startup, so spans are
    # recording and the mirror fires exactly as before.
    if not hasattr(span, "attributes"):
        return
    publish_event(route.event_type, route.extract(span), component=route.component)


# ---------------------------------------------------------------------------
# Context-manager helpers
# ---------------------------------------------------------------------------


@contextmanager
def movement_resolved_span(
    *,
    pc_name: str,
    from_region: str,
    to_region: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Open the ``movement.resolved`` INFO span for one PC's successful
    move. The handler writes the remaining lie-detector attributes
    (``resolved_via`` / ``candidate_exits`` / ``edge_kind`` /
    ``target_pre_materialized`` / ``materialize_triggered`` /
    ``party_split_after`` / ``intent.*``) onto the span."""
    with Span.open(
        SPAN_MOVEMENT_RESOLVED,
        {
            "pc_name": pc_name,
            "from_region": from_region,
            "to_region": to_region,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span
    _mirror_movement_span_to_sink(SPAN_MOVEMENT_RESOLVED, span)


@contextmanager
def movement_unresolved_span(
    *,
    pc_name: str,
    reason: str,
    from_region: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Open the ``movement.unresolved`` ERROR span for one PC's failed
    move (fail-loud, §Q4). Marks the span status ERROR so the GM panel
    surfaces it as a failure, never a silent stay-put. The handler writes
    ``intent.*`` + ``available_exits``."""
    with Span.open(
        SPAN_MOVEMENT_UNRESOLVED,
        {
            "pc_name": pc_name,
            "reason": reason,
            "from_region": from_region,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        span.set_status(Status(StatusCode.ERROR, reason))
        yield span
    _mirror_movement_span_to_sink(SPAN_MOVEMENT_UNRESOLVED, span)


@contextmanager
def movement_region_mode_span(
    *,
    pc_name: str,
    from_region: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Open the ``movement.region_mode`` INFO span for one PC's move in a
    cartography region-mode world. The procedural-dungeon navigator does NOT
    own travel in region-mode worlds (no DungeonStore by design); the
    narration_apply heading→region path resolves it. This span records that
    the movement subsystem recognized the mode and deferred cleanly — and,
    unlike ``movement.unresolved``, it carries NO ERROR status, so the GM
    panel reads movement as engaged-and-deferred rather than failing on every
    move. The handler writes ``intent.*`` + ``world_slug``."""
    with Span.open(
        SPAN_MOVEMENT_REGION_MODE,
        {
            "pc_name": pc_name,
            "from_region": from_region,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span
    _mirror_movement_span_to_sink(SPAN_MOVEMENT_REGION_MODE, span)


@contextmanager
def room_transition_tick_span(
    *,
    advanced_tropes: list[str],
    from_room: str,
    to_room: str,
    pc_name: str = "",
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """ADR-055 / Story 71-15: one span per room-graph transition recording
    which progressing tropes the traversal advanced this turn. The trope
    ids are JSON-encoded — OTEL drops list attribute values."""
    with Span.open(
        SPAN_ROOM_TRANSITION_TICK,
        {
            "advanced_tropes_json": _json.dumps(list(advanced_tropes)),
            "from_room": from_room,
            "to_room": to_room,
            "pc_name": pc_name,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def room_discovered_span(
    *,
    room_id: str,
    newly_discovered: bool,
    discovered_count: int,
    character: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """ADR-055 / Story 153-24: one span per room-graph transition recording
    that the persisted room axis advanced (``discovered_rooms`` /
    ``room_states`` / ``Character.current_room`` written). Complements the
    region axis' ``dungeon.map_emitted`` so the GM panel can confirm the room
    axis moved, not just the region graph. ``newly_discovered`` separates
    genuine exploration (first entry) from backtracking (re-entry)."""
    with Span.open(
        SPAN_ROOM_DISCOVERED,
        {
            "room_id": room_id,
            "newly_discovered": newly_discovered,
            "discovered_count": discovered_count,
            "character": character,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


__all__ = [
    "SPAN_MOVEMENT_REGION_MODE",
    "SPAN_MOVEMENT_RESOLVED",
    "SPAN_MOVEMENT_UNRESOLVED",
    "SPAN_ROOM_DISCOVERED",
    "SPAN_ROOM_TRANSITION_TICK",
    "movement_region_mode_span",
    "movement_resolved_span",
    "movement_unresolved_span",
    "room_discovered_span",
    "room_transition_tick_span",
]
