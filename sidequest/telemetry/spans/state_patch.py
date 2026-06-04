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
# Story 77-2 (ADR-137 §OTEL) — typed quest/stakes tool spans. The GM panel is
# the lie-detector for the campaign-spine substrate: each routes to a
# state_transition event the WatcherSpanProcessor re-emits, exactly like
# SPAN_QUEST_UPDATE above. quest.updated is the successor to SPAN_QUEST_UPDATE
# for the quest-update path (77-4 retired the legacy quest_updates lane, so the
# old span no longer fires from there). SPAN_QUEST_UPDATE itself is retained:
# the separate trope-resolution handshake still uses it as its GM-panel surface.
SPAN_QUEST_CREATED = "quest.created"
SPAN_ROUTES[SPAN_QUEST_CREATED] = SpanRoute(
    event_type="state_transition",
    component="quest_log",
    extract=lambda span: {
        "field": "quest_log",
        "op": "created",
        "quest_id": (span.attributes or {}).get("quest_id", ""),
        "title": (span.attributes or {}).get("title", ""),
        "source": (span.attributes or {}).get("source", ""),
        "anchor_count": (span.attributes or {}).get("anchor_count", 0),
    },
)
SPAN_QUEST_UPDATED = "quest.updated"
SPAN_ROUTES[SPAN_QUEST_UPDATED] = SpanRoute(
    event_type="state_transition",
    component="quest_log",
    extract=lambda span: {
        "field": "quest_log",
        "op": "updated",
        "quest_id": (span.attributes or {}).get("quest_id", ""),
        "old_status": (span.attributes or {}).get("old_status", ""),
        "new_status": (span.attributes or {}).get("new_status", ""),
    },
)
# Story 77-4 (ADR-137 AC-3) — the No-Silent-Fallbacks guard span. After the
# legacy ``quest_updates`` lane is retired, a narrator game_patch that STILL
# carries a ``quest_updates`` key is auto-forwarded to record_quest update-mode
# semantics (the status LANDS in quest_log via upsert_quest_status) and this
# loud, GM-visible span fires so the panel can see a stale-contract emit
# happened — never a silent drop. This span is the guard's lie-detector, so its
# counts must NOT lie: ``quest_ids_json`` + ``updates_count`` carry only the
# items that ACTUALLY forwarded (str status), and ``skipped_count`` carries the
# items dropped (non-str status, or a non-dict value) so every drop is visible.
SPAN_QUEST_UPDATES_LEGACY_EMITTED = "quest.updates.legacy_emitted"
SPAN_ROUTES[SPAN_QUEST_UPDATES_LEGACY_EMITTED] = SpanRoute(
    event_type="state_transition",
    component="quest_log",
    extract=lambda span: {
        "field": "quest_log",
        "op": "legacy_updates_auto_forwarded",
        "quest_ids": (span.attributes or {}).get("quest_ids_json", "[]"),
        "updates_count": (span.attributes or {}).get("updates_count", 0),
        "skipped_count": (span.attributes or {}).get("skipped_count", 0),
        "player_name": (span.attributes or {}).get("player_name", ""),
        "turn_number": (span.attributes or {}).get("turn_number", 0),
    },
)
SPAN_STAKES_SET = "stakes.set"
SPAN_ROUTES[SPAN_STAKES_SET] = SpanRoute(
    event_type="state_transition",
    component="active_stakes",
    extract=lambda span: {
        "field": "active_stakes",
        "op": "set",
        "length": (span.attributes or {}).get("length", 0),
        "source": (span.attributes or {}).get("source", ""),
        "is_fresh": (span.attributes or {}).get("is_fresh", False),
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


def quest_created_span(
    *,
    quest_id: str,
    title: str,
    source: str,
    anchor_count: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit the Story 77-2 ``quest.created`` span (point event, opens+closes).

    Fired when ``record_quest`` mints a NEW quest. ``source`` is
    ``creation|narrator``; ``anchor_count`` is the number of anchors written
    by this call (0 or 1 in v1).
    """
    attributes: dict[str, Any] = {
        "quest_id": quest_id,
        "title": title,
        "source": source,
        "anchor_count": anchor_count,
        **attrs,
    }
    with Span.open(SPAN_QUEST_CREATED, attributes, tracer_override=_tracer):
        pass


def quest_updated_span(
    *,
    quest_id: str,
    old_status: str,
    new_status: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit the Story 77-2 ``quest.updated`` span (point event, opens+closes).

    Fired when ``record_quest`` changes the status of an EXISTING quest. The
    behavioural successor to the legacy ``SPAN_QUEST_UPDATE`` span.
    """
    attributes: dict[str, Any] = {
        "quest_id": quest_id,
        "old_status": old_status,
        "new_status": new_status,
        **attrs,
    }
    with Span.open(SPAN_QUEST_UPDATED, attributes, tracer_override=_tracer):
        pass


def quest_updates_legacy_emitted_span(
    *,
    quest_ids: list[str],
    updates_count: int,
    skipped_count: int = 0,
    player_name: str,
    turn_number: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit the Story 77-4 ``quest.updates.legacy_emitted`` span (point event).

    Fired by the narration-apply auto-forward guard when a narrator game_patch
    still carries a retired ``quest_updates`` key. Items with a string status are
    forwarded into ``quest_log`` via ``upsert_quest_status`` — never dropped — and
    this loud span makes the stale-contract emit visible to the GM panel (No
    Silent Fallbacks). As the guard's lie-detector, its counts must reflect
    reality: ``quest_ids`` + ``updates_count`` are the items that ACTUALLY
    forwarded; ``skipped_count`` is the items dropped (non-str status, or a
    non-dict ``quest_updates`` value) so every drop is observable. ``quest_ids``
    is JSON-encoded (OTEL drops list values).
    """
    attributes: dict[str, Any] = {
        "quest_ids_json": _json.dumps(quest_ids),
        "updates_count": updates_count,
        "skipped_count": skipped_count,
        "player_name": player_name,
        "turn_number": turn_number,
        **attrs,
    }
    with Span.open(SPAN_QUEST_UPDATES_LEGACY_EMITTED, attributes, tracer_override=_tracer):
        pass


def stakes_set_span(
    *,
    length: int,
    source: str,
    is_fresh: bool,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit the Story 77-2 ``stakes.set`` span (point event, opens+closes).

    Fired when ``set_stakes`` writes/appends ``active_stakes``. ``is_fresh`` is
    True when the call takes the field from empty to populated (establishment),
    False when it evolves already-present stakes — the GM-panel signal that the
    spine's stakes substrate stopped being empty (the oz turn-13 failure).
    """
    attributes: dict[str, Any] = {
        "length": length,
        "source": source,
        "is_fresh": is_fresh,
        **attrs,
    }
    with Span.open(SPAN_STAKES_SET, attributes, tracer_override=_tracer):
        pass


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
