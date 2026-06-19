"""Persistence spans — save/load/delete of SQLite session files."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

from ._core import FLAT_ONLY_SPANS, SPAN_ROUTES, SpanRoute
from .span import Span

SPAN_PERSISTENCE_SAVE = "persistence_save"
SPAN_PERSISTENCE_LOAD = "persistence_load"
SPAN_PERSISTENCE_DELETE = "persistence_delete"

FLAT_ONLY_SPANS.update(
    {
        SPAN_PERSISTENCE_SAVE,
        SPAN_PERSISTENCE_LOAD,
        SPAN_PERSISTENCE_DELETE,
    }
)


# ---------------------------------------------------------------------------
# Snapshot canonicalize — sidequest/game/migrations.py
# Emitted by ``SqliteStore.load`` when ``migrate_legacy_snapshot`` rewrote
# any field. Per-field migration markers are span attributes (e.g.
# ``s1_world_confrontations_merged: int``). Lie-detector hook for the GM
# panel — Sebastien sees which legacy split-brain shapes are still in the
# wild.
#
# Honesty rule: the extractor only forwards keys that an actual migration
# sub-function emits. S4 (Python class rename) and S5 (``Field(exclude=True)``)
# are NOT per-save migrations — they leave no on-disk trace and have no
# sub-function. Reporting hardcoded ``s4_encounter_tag_renamed: false`` /
# ``s5_pending_queues_dropped: 0`` would lie to the GM panel. When/if a
# future migration sub-function legitimately emits an attribute, extend
# this dict alongside the sub-function — never before.
# ---------------------------------------------------------------------------
SPAN_SNAPSHOT_CANONICALIZE = "snapshot.canonicalize"


def _extract_snapshot_canonicalize(span: Any) -> dict[str, Any]:
    """Forward only the per-field migration attributes the span carries.

    No defaulted keys: if a sub-function did not register an attribute for
    this load, the GM panel must not see a value for it. Otherwise the
    extractor invents zero/false markers for migrations that never ran.
    """
    payload: dict[str, Any] = {"field": "snapshot", "op": "canonicalize"}
    attrs = span.attributes or {}
    for key in (
        "s1_world_confrontations_merged",
        "s1_world_confrontations_dropped_no_target",
        "s3_party_location_seeded",
        "s6_voice_id_stripped",
    ):
        if key in attrs:
            payload[key] = attrs[key]
    return payload


SPAN_ROUTES[SPAN_SNAPSHOT_CANONICALIZE] = SpanRoute(
    event_type="state_transition",
    component="persistence",
    extract=_extract_snapshot_canonicalize,
)


# ---------------------------------------------------------------------------
# Snapshot party-location query — Wave 2B / story 45-48 / S3
# Emitted by ``GameSnapshot.party_location()``. Lie-detector hook: when
# ``party_split=True``, the seated PCs disagree on where the party is —
# Sebastien's GM panel can flag mid-session splits the narrator may be
# papering over. ``perspective_supplied=True`` short-circuits the consensus
# check (single-PC framing).
# ---------------------------------------------------------------------------
SPAN_PARTY_LOCATION_QUERY = "snapshot.party_location_query"
SPAN_ROUTES[SPAN_PARTY_LOCATION_QUERY] = SpanRoute(
    event_type="state_transition",
    component="snapshot",
    extract=lambda span: {
        "field": "snapshot",
        "op": "party_location_query",
        "perspective_supplied": (span.attributes or {}).get("perspective_supplied", False),
        "consensus_found": (span.attributes or {}).get("consensus_found", False),
        "party_split": (span.attributes or {}).get("party_split", False),
    },
)


# ---------------------------------------------------------------------------
# Snapshot per-PC region query — Movement subsystem §Q0 (per-PC analogue of
# the party-location query above). Emitted by ``GameSnapshot.region_for()``.
# Same three-mode contract over ``pc_regions`` instead of ``character_locations``
# — ``party_split=True`` flags seated PCs in disagreement on their graph region
# (a genuinely split party, per ADR-037). The GM panel's lie-detector for the
# movement subsystem: it can SEE a split rather than trusting one stream of prose.
# ---------------------------------------------------------------------------
SPAN_REGION_QUERY = "snapshot.region_query"
SPAN_ROUTES[SPAN_REGION_QUERY] = SpanRoute(
    event_type="state_transition",
    component="snapshot",
    extract=lambda span: {
        "field": "snapshot",
        "op": "region_query",
        "perspective_supplied": (span.attributes or {}).get("perspective_supplied", False),
        "consensus_found": (span.attributes or {}).get("consensus_found", False),
        "party_split": (span.attributes or {}).get("party_split", False),
    },
)


# ---------------------------------------------------------------------------
# Region anchor sync — sq-playtest 2026-06-12 (beneath_sunden split-brain).
# Emitted by ``GameSnapshot._apply_world_patch_inner`` when a per-PC
# ``pc_region`` crossing leaves the seated party in CONSENSUS on a region
# that differs from the singular ``current_region`` anchor: the anchor is
# advanced to the consensus. Without this sync every anchor consumer (the
# per-turn region projection, save forensics, the render trigger) reads a
# stale surface region while the PCs stand inside the dungeon graph — the
# narrator never receives the generated room manifest and improvises the
# crawl. The GM panel sees from/to so a stuck anchor is visible, not silent.
# ---------------------------------------------------------------------------
SPAN_REGION_ANCHOR_SYNCED = "snapshot.region_anchor_synced"
SPAN_ROUTES[SPAN_REGION_ANCHOR_SYNCED] = SpanRoute(
    event_type="state_transition",
    component="snapshot",
    extract=lambda span: {
        "field": "current_region",
        "op": "anchor_synced",
        "from_region": (span.attributes or {}).get("from_region", ""),
        "to_region": (span.attributes or {}).get("to_region", ""),
    },
)


# ---------------------------------------------------------------------------
# Session lifecycle — sidequest/game/persistence.py
# Fires every time SqliteStore.init_session() runs — including on a fresh
# slot — so the GM panel gets the negative confirmation that reinit ran
# cleanly (CLAUDE.md observability principle: a silent half-clear
# regression must not be invisible).
# ---------------------------------------------------------------------------
SPAN_SESSION_SLOT_REINITIALIZED = "session.slot_reinitialized"
SPAN_ROUTES[SPAN_SESSION_SLOT_REINITIALIZED] = SpanRoute(
    event_type="state_transition",
    component="session",
    extract=lambda span: {
        "field": "session_meta",
        "op": "slot_reinitialized",
        "genre_slug": (span.attributes or {}).get("genre_slug", ""),
        "world_slug": (span.attributes or {}).get("world_slug", ""),
        "cleared_tables": (span.attributes or {}).get("cleared_tables", []),
        "prior_narrative_count": (span.attributes or {}).get("prior_narrative_count", 0),
        "prior_event_count": (span.attributes or {}).get("prior_event_count", 0),
        "mode": (span.attributes or {}).get("mode", "clear"),
    },
)


@contextmanager
def persistence_save_span(
    genre: str,
    world: str,
    player: str,
    *,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_PERSISTENCE_SAVE,
        {"genre": genre, "world": world, "player": player, **attrs},
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def persistence_load_span(
    genre: str,
    world: str,
    player: str,
    *,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_PERSISTENCE_LOAD,
        {"genre": genre, "world": world, "player": player, **attrs},
        tracer_override=_tracer,
    ) as span:
        yield span


# ---------------------------------------------------------------------------
# turn_telemetry retention prune — Story 126-22. Unlike projection_cache,
# turn_telemetry is forensic data (ADR-124), not a rebuildable cache, so it
# gets a keep-last-N-rounds retention rather than eviction. The prune emits
# rows_pruned + session_id so the save-DB cleanup is observable, not a silent
# table shrink that could quietly destroy forensic history.
# ---------------------------------------------------------------------------
SPAN_TURN_TELEMETRY_PRUNE = "turn_telemetry.prune"
SPAN_ROUTES[SPAN_TURN_TELEMETRY_PRUNE] = SpanRoute(
    event_type="state_transition",
    component="persistence",
    extract=lambda span: {
        "field": "turn_telemetry.prune",
        "session_id": (span.attributes or {}).get("session_id", 0),
        "rows_pruned": (span.attributes or {}).get("rows_pruned", 0),
    },
)


@contextmanager
def turn_telemetry_prune_span(
    *, session_id: int, _tracer: trace.Tracer | None = None
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_TURN_TELEMETRY_PRUNE,
        {"session_id": session_id},
        tracer_override=_tracer,
    ) as span:
        yield span
