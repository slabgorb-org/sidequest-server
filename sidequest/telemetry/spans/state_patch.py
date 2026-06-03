"""State-patch spans — apply_world_patch, quest updates, handshake delta, HP delta."""

from __future__ import annotations

import json as _json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

from ._core import FLAT_ONLY_SPANS, SPAN_ROUTES, SpanRoute
from .span import Span

# Port-artifact constants — kept flat-only.
SPAN_APPLY_WORLD_PATCH = "apply_world_patch"
SPAN_BUILD_PROTOCOL_DELTA = "build_protocol_delta"
SPAN_COMPUTE_DELTA = "compute_delta"

FLAT_ONLY_SPANS.update(
    {
        SPAN_APPLY_WORLD_PATCH,
        SPAN_BUILD_PROTOCOL_DELTA,
        SPAN_COMPUTE_DELTA,
    }
)

# Live spans.
SPAN_QUEST_UPDATE = "quest_update"
SPAN_ROUTES[SPAN_QUEST_UPDATE] = SpanRoute(
    event_type="state_transition",
    component="quest_log",
    extract=lambda span: {
        "field": "quest_log",
        "updates": (span.attributes or {}).get("updates_json", "{}"),
        "updates_count": (span.attributes or {}).get("updates_count", 0),
        "player_name": (span.attributes or {}).get("player_name", ""),
        "turn_number": (span.attributes or {}).get("turn_number", 0),
    },
)
# Story 77-1 (ADR-137 Option A) — seed-at-creation quest spine span.
# Fires once at session creation when the chargen PC's drive/calling is
# materialized into quest_log + quest_anchors + active_stakes. On an empty
# drive AND calling (the prose-pack case, e.g. wry_whimsy) it STILL fires,
# carrying severity="warning" so the GM panel sees the seed ran with nothing to
# seed from — never a silent skip (CLAUDE.md "No Silent Fallbacks"). This is
# the ONLY span this story adds; quest.created / quest.updated /
# quest.anchor.added / stakes.set belong to 77-2 / 77-3.
SPAN_QUEST_SEEDED_AT_CREATION = "quest.seeded_at_creation"
SPAN_ROUTES[SPAN_QUEST_SEEDED_AT_CREATION] = SpanRoute(
    event_type="state_transition",
    component="quest_log",
    extract=lambda span: {
        "field": "quest_spine",
        "op": "seeded_at_creation",
        "quest_id": (span.attributes or {}).get("quest_id", ""),
        "anchor_id": (span.attributes or {}).get("anchor_id", ""),
        "source_drive": (span.attributes or {}).get("source_drive", ""),
        "has_stakes": (span.attributes or {}).get("has_stakes", False),
        "severity": (span.attributes or {}).get("severity", "info"),
        "deferred": (span.attributes or {}).get("deferred", False),
    },
)
SPAN_GAME_HANDSHAKE_DELTA_APPLIED = "game.handshake.delta_applied"
SPAN_ROUTES[SPAN_GAME_HANDSHAKE_DELTA_APPLIED] = SpanRoute(
    event_type="state_transition",
    component="game",
    extract=lambda span: {
        "field": "shared_world_delta",
        "op": "applied",
        "delta_fields": (span.attributes or {}).get("delta_fields", []),
        "conflict_count": (span.attributes or {}).get("conflict_count", 0),
        "resolution_path": (span.attributes or {}).get("resolution_path", ""),
    },
)


# ADR-114 §6 — HP-delta span on the state_patch route.
# Every real HP mutation in apply_beat_hp_channel emits this span so the
# GM panel can verify the engine applied ablative damage rather than the
# narrator improvising a wound (lie-detector discipline, CLAUDE.md).
SPAN_STATE_PATCH_HP = "state_patch.hp"
SPAN_ROUTES[SPAN_STATE_PATCH_HP] = SpanRoute(
    event_type="state_transition",
    component="combat",
    extract=lambda span: {
        "field": "hp",
        "actor": (span.attributes or {}).get("actor", ""),
        "delta": (span.attributes or {}).get("delta", 0),
        "source": (span.attributes or {}).get("source", ""),
        "current": (span.attributes or {}).get("current", 0),
        "maximum": (span.attributes or {}).get("maximum", 0),
    },
)


def state_patch_hp_span(
    *,
    actor: str,
    delta: int,
    source: str,
    current: int,
    maximum: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit a state_patch.hp span (ADR-114 §6 lie-detector).

    Not a context manager — the HP delta is a point mutation, not a span
    of work. Opens and immediately closes the span so the WatcherSpanProcessor
    can route it to the GM panel's state_transition feed.
    """
    attributes: dict[str, Any] = {
        "field": "hp",
        "actor": actor,
        "delta": delta,
        "source": source,
        "current": current,
        "maximum": maximum,
        **attrs,
    }
    with Span.open(SPAN_STATE_PATCH_HP, attributes, tracer_override=_tracer):
        pass


@contextmanager
def quest_update_span(
    *,
    updates: dict[str, str],
    player_name: str,
    turn_number: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """``updates`` is JSON-encoded — OTEL silently drops dict/list values."""
    attributes: dict[str, Any] = {
        "updates_json": _json.dumps(dict(updates), sort_keys=True),
        "updates_count": len(updates),
        "player_name": player_name,
        "turn_number": turn_number,
        **attrs,
    }
    with Span.open(SPAN_QUEST_UPDATE, attributes, tracer_override=_tracer) as span:
        yield span


def quest_seeded_at_creation_span(
    *,
    quest_id: str,
    anchor_id: str,
    source_drive: str,
    has_stakes: bool,
    severity: str,
    deferred: bool = False,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit the Story 77-1 seed-at-creation span (point event, opens+closes).

    Three GM-panel-distinguishable cases:
    - real seed: ``severity="info"``, ids set, ``source_drive`` = the PC drive.
    - empty drive AND calling: ``severity="warning"``, ids empty, ``has_stakes``
      False — the loud tell that nothing could be seeded (No Silent Fallbacks).
    - deferred to a world-authored spine: ``severity="info"``, ``deferred=True``,
      ids empty, ``source_drive`` empty, ``has_stakes`` True — the seed found an
      authored ``active_stakes`` and preserved it rather than clobbering it.
    """
    attributes: dict[str, Any] = {
        "quest_id": quest_id,
        "anchor_id": anchor_id,
        "source_drive": source_drive,
        "has_stakes": has_stakes,
        "severity": severity,
        "deferred": deferred,
        **attrs,
    }
    with Span.open(SPAN_QUEST_SEEDED_AT_CREATION, attributes, tracer_override=_tracer):
        pass
