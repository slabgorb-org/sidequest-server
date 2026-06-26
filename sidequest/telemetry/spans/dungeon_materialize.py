"""Dungeon materializer spans (Beneath Sünden Plan 7 §OTEL).

Every span constant owned by Plan 7's materializer pipeline lives here.
The five stage spans nest under the parent ``dungeon.materialize`` span
for the duration of one ``materialize()`` call. ``frontier.expand`` is
also Plan-7-owned (the async look-ahead worker fires it when it picks the
next edge to expand); the helper is provided now so the catalog is complete
and routed even though the worker is a later task.

No spans were emitted here before this module — Plan 5/6 deferred them
deliberately (emitting spans with no caller would be the exact Illusionism
the GM panel exists to catch). Plan 7 is the first caller.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

from ._core import SPAN_ROUTES, SpanRoute
from .span import Span

# ---------------------------------------------------------------------------
# Span name constants
# ---------------------------------------------------------------------------

SPAN_DUNGEON_MATERIALIZE = "dungeon.materialize"
SPAN_DUNGEON_MATERIALIZE_DESIGN = "dungeon.materialize.design"
# Story 158-37 — a region themed against the frontier spawn_depth_score can land
# at a final depth its own depth_band excludes; the design stage re-resolves it
# against its own depth. This span is the GM-panel lie-detector for that
# correction (which regions changed theme, from/to, and at what depth).
SPAN_DUNGEON_MATERIALIZE_THEME_RESOLVE = "dungeon.materialize.theme_resolve"
SPAN_DUNGEON_MATERIALIZE_FILL = "dungeon.materialize.fill"
# Story 52-2 — ADR-096 mask emit, one per region inside the fill stage.
SPAN_DUNGEON_MATERIALIZE_MASK = "dungeon.materialize.mask"
SPAN_DUNGEON_MATERIALIZE_CURATE = "dungeon.materialize.curate"
# Story 153-26 (rework) — a region's AUTHORED room binding could not be resolved
# while surfacing authored content on the deterministic curate path (a dangling/
# absent bestiary id — an authoring error). LOUD-but-graceful: curate proceeds
# with procedural content rather than crashing the player-facing connect.
# (ADR-106 Amendment C retired the parse_failed/degraded LLM-robustness spans;
# this authored-bind span survives — _append_authored_creatures is now main-path.)
SPAN_DUNGEON_CURATE_AUTHORED_BIND_FAILED = "dungeon.curate.authored_bind_failed"
SPAN_DUNGEON_MATERIALIZE_ATTACH = "dungeon.materialize.attach"
SPAN_DUNGEON_MATERIALIZE_COMMIT = "dungeon.materialize.commit"
# ADR-096 token+feature: tactical data derived and persisted in the mask blob.
SPAN_DUNGEON_MATERIALIZE_TACTICAL = "dungeon.materialize.tactical"
SPAN_FRONTIER_EXPAND = "frontier.expand"
SPAN_FRONTIER_REGION_TRANSITION = "frontier.region_transition"
SPAN_FRONTIER_LOOKAHEAD = "frontier.lookahead"

# ---------------------------------------------------------------------------
# Routing registrations
# ---------------------------------------------------------------------------


def _attr(field: str):
    return lambda span, f=field: (span.attributes or {}).get(f)


SPAN_ROUTES[SPAN_DUNGEON_MATERIALIZE] = SpanRoute(
    event_type="state_transition",
    component="dungeon",
    extract=lambda s: {
        "field": "dungeon_map",
        "op": "materialize",
        "expansion_id": _attr("expansion_id")(s),
        "heading": _attr("heading")(s),
        "burst_magnitude": _attr("burst_magnitude")(s),
    },
)

SPAN_ROUTES[SPAN_DUNGEON_MATERIALIZE_DESIGN] = SpanRoute(
    event_type="state_transition",
    component="dungeon",
    # report.as_dict() keys are the byte-pinned attribute contract (Plan 7 Task 2).
    # error/failing are the ExpansionGenerationError lie-detector markers: they
    # read None on the success path (graceful-get idiom — harmless) and surface
    # the generation failure on the GM panel on the failure path.
    extract=lambda s: {
        "field": "dungeon_map",
        "op": "materialize.design",
        "expansion_id": _attr("expansion_id")(s),
        "attempts": _attr("attempts")(s),
        "stitch_edges": _attr("stitch_edges")(s),
        "loops_into_explored": _attr("loops_into_explored")(s),
        "hidden_edges": _attr("hidden_edges")(s),
        "shortcut_edges": _attr("shortcut_edges")(s),
        "new_regions": _attr("new_regions")(s),
        "invariants_passed": _attr("invariants_passed")(s),
        "error": _attr("error")(s),
        "failing": _attr("failing")(s),
    },
)

SPAN_ROUTES[SPAN_DUNGEON_MATERIALIZE_THEME_RESOLVE] = SpanRoute(
    event_type="state_transition",
    component="dungeon",
    # Story 158-37 lie-detector: `resolved_count` is how many regions were
    # re-themed to honor their own depth_band; `resolutions` is the JSON
    # from/to/depth audit. resolved_count==0 (the common case) proves the
    # corrector ran and found the generation already depth-coherent.
    extract=lambda s: {
        "field": "dungeon_map",
        "op": "materialize.theme_resolve",
        "expansion_id": _attr("expansion_id")(s),
        "resolved_count": _attr("resolved_count")(s),
        "resolutions": _attr("resolutions")(s),
    },
)

SPAN_ROUTES[SPAN_DUNGEON_MATERIALIZE_FILL] = SpanRoute(
    event_type="state_transition",
    component="dungeon",
    # `regions` is the per-region fill payload (Plan 7 Task 3): a JSON list
    # of {region_id, algorithm, width, height, braid_ratio} — the
    # ACTUALLY-applied braid_ratio per region (lie-detector: proves it was
    # not silently defaulted). `error` is the failure marker: it reads None
    # on the success path (graceful-get idiom — harmless) and surfaces a
    # missing-theme / roomcorridor-floor / degenerate-seed / unknown-algorithm
    # failure on the GM panel on the failure path.
    extract=lambda s: {
        "field": "dungeon_map",
        "op": "materialize.fill",
        "expansion_id": _attr("expansion_id")(s),
        "stage": "fill",
        "regions": _attr("regions")(s),
        "region_count": _attr("region_count")(s),
        "error": _attr("error")(s),
    },
)

SPAN_ROUTES[SPAN_DUNGEON_MATERIALIZE_MASK] = SpanRoute(
    event_type="state_transition",
    component="dungeon",
    # Story 52-2: one span per region inside the fill stage carrying the
    # ADR-096 mask emit lie-detector surface. `mask_sha` is the dedupe key +
    # GM-panel "did mask emission actually run?" signal; `cell_width` is the
    # ADR-096 canonical constant (locked to 28); grid_width/grid_height are
    # the source-grid cell counts. A set-but-not-routed marker is the
    # Plan-7-Task-2 defect class — route it.
    extract=lambda s: {
        "field": "dungeon_map",
        "op": "materialize.mask",
        "region_id": _attr("region_id")(s),
        "grid_width": _attr("grid_width")(s),
        "grid_height": _attr("grid_height")(s),
        "cell_width": _attr("cell_width")(s),
        "mask_sha": _attr("mask_sha")(s),
    },
)

SPAN_ROUTES[SPAN_DUNGEON_MATERIALIZE_CURATE] = SpanRoute(
    event_type="state_transition",
    component="dungeon",
    # `curated` is the lie-detector verdict (Plan 7 Task 4): True only when
    # the bounded `claude -p` curation pass succeeded AND every corpus
    # creature was CR→Edge translated; False (with a specific `reason`) on
    # any failure path (assemble error / subprocess failure / unparseable
    # verdict). The Task-2 lesson: a set-but-not-routed marker is a defect
    # — `curated`/`reason` are routed here so the GM panel renders the
    # failure, never a raw-manifest-stamped-curated lie. The success
    # summary (region/creature counts, races, cr_bands) reads None on the
    # failure path via the graceful-get idiom (harmless).
    extract=lambda s: {
        "field": "dungeon_map",
        "op": "materialize.curate",
        "expansion_id": _attr("expansion_id")(s),
        "stage": "curate",
        "curated": _attr("curated")(s),
        "reason": _attr("reason")(s),
        "region_count": _attr("region_count")(s),
        "creature_count": _attr("creature_count")(s),
        "manifest_race": _attr("manifest_race")(s),
        "cr_band": _attr("cr_band")(s),
        "raw_seed_reproducible": _attr("raw_seed_reproducible")(s),
    },
)

SPAN_ROUTES[SPAN_DUNGEON_CURATE_AUTHORED_BIND_FAILED] = SpanRoute(
    event_type="state_transition",
    component="dungeon",
    # Story 153-26 (rework): emitted when a degraded region's AUTHORED
    # encounter_creatures binding cannot be resolved (a dangling/absent
    # bestiary id). The GM-panel lie-detector for the loud-but-graceful catch:
    # the authored content was DROPPED (procedural coal shipped instead) and
    # the author's broken binding is surfaced rather than silently swallowed.
    extract=lambda s: {
        "field": "dungeon_map",
        "op": "curate.authored_bind_failed",
        "region_id": _attr("region_id")(s),
        "world_slug": _attr("world_slug")(s),
        "error": _attr("error")(s),
    },
)

SPAN_ROUTES[SPAN_DUNGEON_MATERIALIZE_ATTACH] = SpanRoute(
    event_type="state_transition",
    component="dungeon",
    # DepthReport.as_dict()'s 4 keys are the byte-pinned attribute contract
    # (Plan 3 pinned them for THIS consumer; Plan 7 Task 5). NOT
    # AttachReport — that is Plan 6's nested setpiece.attach span (routed
    # in dungeon_setpiece.py; Plan 7 does NOT re-route it). error/reason
    # are the routed failure markers: they read None on the success path
    # (graceful-get idiom — harmless) and surface an attach_expansion
    # global-invariant violation / unreachable-region / attach_set_piece
    # (PersistError, trope_id-not-in-pack) failure on the GM panel on the
    # failure path (the Task-2 lesson: a set-but-not-routed marker is a
    # defect — route it).
    extract=lambda s: {
        "field": "dungeon_map",
        "op": "materialize.attach",
        "expansion_id": _attr("expansion_id")(s),
        "stage": "attach",
        "regions_scored": _attr("regions_scored")(s),
        "depth_min": _attr("depth_min")(s),
        "depth_max": _attr("depth_max")(s),
        "depth_mean": _attr("depth_mean")(s),
        "error": _attr("error")(s),
        "reason": _attr("reason")(s),
    },
)
# Note: "stage" above is a routed CONSTANT (the GM-panel column), not a
# span attribute lookup — the span no longer pre-bakes a "stage" attr so
# the stage can write EXACTLY DepthReport.as_dict()'s 4 keys (byte-pinned).

SPAN_ROUTES[SPAN_DUNGEON_MATERIALIZE_TACTICAL] = SpanRoute(
    event_type="state_transition",
    component="dungeon",
    # ADR-096 token+feature: one point-event span per materialize() call,
    # emitted inside _stage_commit after tactical data has been derived and
    # merged into the per-region mask dicts. region_count proves derivation
    # ran for every filled region; feature_count + anchor_count are the GM-
    # panel lie-detector signal that the derivation was non-trivial (not a
    # silent empty pass). They read None on skip (expansion_masks=None —
    # harmless via the graceful-get idiom).
    extract=lambda s: {
        "field": "dungeon_map",
        "op": "materialize.tactical",
        "region_count": _attr("region_count")(s),
        "feature_count": _attr("feature_count")(s),
        "anchor_count": _attr("anchor_count")(s),
    },
)

SPAN_ROUTES[SPAN_DUNGEON_MATERIALIZE_COMMIT] = SpanRoute(
    event_type="state_transition",
    component="dungeon",
    # Plan 7 Task 6 real attribute surface (NOT the Task-1 placeholder).
    # On success _stage_commit writes the seed/regions/edges/rolled/
    # frontier-edges summary + the generator_version actually stamped
    # (lie-detector: proves the Seed=Expansion-0 commit ran and which
    # version froze this expansion). error/reason are the routed failure
    # markers: they read None on the success path (graceful-get idiom —
    # harmless) and surface a PersistError (commit re-commit-freeze /
    # mid-write rollback) on the GM panel on the failure path (the Task-2
    # lesson: a set-but-not-routed marker is a defect — route it).
    extract=lambda s: {
        "field": "dungeon_map",
        "op": "materialize.commit",
        "expansion_id": _attr("expansion_id")(s),
        "stage": "commit",
        "seeded_entrance": _attr("seeded_entrance")(s),
        "regions_committed": _attr("regions_committed")(s),
        "edges_committed": _attr("edges_committed")(s),
        "rolled_persisted": _attr("rolled_persisted")(s),
        "frontier_edges_added": _attr("frontier_edges_added")(s),
        "generator_version": _attr("generator_version")(s),
        "error": _attr("error")(s),
        "reason": _attr("reason")(s),
    },
)

SPAN_ROUTES[SPAN_FRONTIER_EXPAND] = SpanRoute(
    event_type="state_transition",
    component="dungeon",
    # Plan 7 Task 6 real attribute surface (NOT the Task-1 placeholder):
    # one span per new unexpanded frontier edge the commit put_frontier'd.
    # from_region_id/heading/spawn_depth_score are the derived edge's real
    # fields so the GM panel sees exactly where + at what depth the next
    # expansion would spawn (lie-detector: the frontier actually grew).
    extract=lambda s: {
        "field": "dungeon_frontier",
        "op": "frontier_expand",
        "expansion_id": _attr("expansion_id")(s),
        "frontier_edge_id": _attr("frontier_edge_id")(s),
        "from_region_id": _attr("from_region_id")(s),
        "heading": _attr("heading")(s),
        "spawn_depth_score": _attr("spawn_depth_score")(s),
    },
)

SPAN_ROUTES[SPAN_FRONTIER_LOOKAHEAD] = SpanRoute(
    event_type="state_transition",
    component="dungeon",
    # Plan 7 Task 7 — the async look-ahead WORKER's lie-detector surface.
    # `deduped` is the idempotency proof: True when a second approach
    # signal for an already-in-flight frontier_edge_id was a no-op (NOT a
    # double-materialize) — the GM panel can SEE the dedupe rather than
    # trusting that two rapid signals didn't grow the dungeon twice.
    # `targets` is the count of frontier edges selected along the heading
    # (lookahead_breadth resolution); `no_frontier_along_heading` is True
    # for the genuine no-op case (not every transition approaches the
    # frontier — observable, NOT a silent skip). `error`/`reason` are the
    # routed terminal-failure markers: a background worker/materialize
    # failure surfaces here (the GM panel must see the dungeon failed to
    # grow), never silently swallowed; they read None on the success path
    # (graceful-get idiom — harmless). The Task-2 lesson: a set-but-not-
    # routed marker is a defect — route it.
    extract=lambda s: {
        "field": "dungeon_frontier",
        "op": "frontier_lookahead",
        "to_region": _attr("to_region")(s),
        "heading": _attr("heading")(s),
        "frontier_edge_id": _attr("frontier_edge_id")(s),
        "expansion_id": _attr("expansion_id")(s),
        "targets": _attr("targets")(s),
        "deduped": _attr("deduped")(s),
        "no_frontier_along_heading": _attr("no_frontier_along_heading")(s),
        "error": _attr("error")(s),
        "reason": _attr("reason")(s),
    },
)

SPAN_ROUTES[SPAN_FRONTIER_REGION_TRANSITION] = SpanRoute(
    event_type="state_transition",
    component="dungeon",
    # The real production region-transition seam fired (Plan 7 Task 6
    # wiring). `observers` is the count of registered frontier-approach
    # observers (Task 7's worker) — the GM panel can tell the seam is
    # wired-but-unconsumed (observers == 0, honest-deferral) from
    # wired-and-live (observers >= 1) rather than guessing.
    extract=lambda s: {
        "field": "current_region",
        "op": "frontier_region_transition",
        "from_region": _attr("from_region")(s),
        "to_region": _attr("to_region")(s),
        "observers": _attr("observers")(s),
        # Movement subsystem §Q2 — WHICH PC moved (split-party legibility;
        # two PCs moving = two spans with distinct pc_name).
        "pc_name": _attr("pc_name")(s),
    },
)

# ---------------------------------------------------------------------------
# Context-manager helpers
# ---------------------------------------------------------------------------


@contextmanager
def dungeon_materialize_span(
    *,
    expansion_id: int,
    heading: str,
    burst_magnitude: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Open the parent ``dungeon.materialize`` span for one materialize() call."""
    with Span.open(
        SPAN_DUNGEON_MATERIALIZE,
        {
            "expansion_id": expansion_id,
            "heading": heading,
            "burst_magnitude": burst_magnitude,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def dungeon_materialize_design_span(
    *,
    expansion_id: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Open the ``dungeon.materialize.design`` child span.

    No ``stage`` attribute is pre-baked here: the design stage itself writes
    exactly ``report.as_dict()`` onto the span after ``generate_expansion``
    returns (byte-pinned GM-panel contract, Plan 7 Task 2).  The only
    pre-baked attribute is ``expansion_id``; the stage's ``set_attribute``
    calls overwrite it with the same value from the report.
    """
    with Span.open(
        SPAN_DUNGEON_MATERIALIZE_DESIGN,
        {"expansion_id": expansion_id, **attrs},
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def dungeon_materialize_theme_resolve_span(
    *,
    expansion_id: int,
    resolved_count: int,
    resolutions: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Open the ``dungeon.materialize.theme_resolve`` child span (Story 158-37).

    Nests under the design span. ``resolutions`` is a JSON string (the
    ThemeResolutionReport's ``resolutions`` list) so OTEL can carry the
    per-region from/to/depth audit; ``resolved_count`` is the scalar headline.
    """
    with Span.open(
        SPAN_DUNGEON_MATERIALIZE_THEME_RESOLVE,
        {
            "expansion_id": expansion_id,
            "resolved_count": resolved_count,
            "resolutions": resolutions,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def dungeon_materialize_fill_span(
    *,
    expansion_id: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Open the ``dungeon.materialize.fill`` child span."""
    with Span.open(
        SPAN_DUNGEON_MATERIALIZE_FILL,
        {"expansion_id": expansion_id, "stage": "fill", **attrs},
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def dungeon_materialize_mask_span(
    *,
    region_id: str,
    grid_width: int,
    grid_height: int,
    cell_width: int,
    mask_sha: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Open the ``dungeon.materialize.mask`` child span for one region's
    ADR-096 mask emit (story 52-2). Opened inside the fill stage's loop so
    it nests under the live ``dungeon.materialize.fill`` span. All
    lie-detector attributes are routed (see ``SPAN_ROUTES``)."""
    with Span.open(
        SPAN_DUNGEON_MATERIALIZE_MASK,
        {
            "region_id": region_id,
            "grid_width": grid_width,
            "grid_height": grid_height,
            "cell_width": cell_width,
            "mask_sha": mask_sha,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def dungeon_materialize_curate_span(
    *,
    expansion_id: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Open the ``dungeon.materialize.curate`` child span."""
    with Span.open(
        SPAN_DUNGEON_MATERIALIZE_CURATE,
        {"expansion_id": expansion_id, "stage": "curate", **attrs},
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def dungeon_curate_authored_bind_failed_span(
    *,
    region_id: str,
    world_slug: str,
    error: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Story 153-26 (rework): a degraded region's AUTHORED room binding could not
    be resolved (a dangling/absent bestiary id — an authoring error). The
    loud-but-graceful signal: the binding error is surfaced to the GM panel
    rather than crashing the connect, and the degrade proceeds with procedural
    coal. Closed immediately — a point event, not a nested stage."""
    with Span.open(
        SPAN_DUNGEON_CURATE_AUTHORED_BIND_FAILED,
        {
            "region_id": region_id,
            "world_slug": world_slug,
            "error": error,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def dungeon_materialize_attach_span(
    *,
    expansion_id: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Open the ``dungeon.materialize.attach`` child span.

    No ``stage`` attribute is pre-baked here (same deliberate choice as
    ``dungeon_materialize_design_span``): the attach stage itself writes
    exactly ``DepthReport.as_dict()``'s 4 keys onto the span on success
    (byte-pinned GM-panel contract, Plan 7 Task 5 — Plan 3 pinned the key
    set for THIS consumer), and a routed ``error``/``reason`` failure
    marker on the failure path. The only pre-baked attribute is
    ``expansion_id`` (pipeline scaffold, like the design span).
    """
    with Span.open(
        SPAN_DUNGEON_MATERIALIZE_ATTACH,
        {"expansion_id": expansion_id, **attrs},
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def dungeon_materialize_tactical_span(
    *,
    region_count: int,
    feature_count: int,
    anchor_count: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Open the ``dungeon.materialize.tactical`` point-event span.

    Emitted once per materialize() call inside _stage_commit after
    ``_stage_tactical`` has derived and ``_tactical_into_mask_dicts`` has
    merged the tactical records into the per-region mask dicts.  Closed
    immediately — this is a point event, not a long-lived stage span.
    """
    with Span.open(
        SPAN_DUNGEON_MATERIALIZE_TACTICAL,
        {
            "region_count": region_count,
            "feature_count": feature_count,
            "anchor_count": anchor_count,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def dungeon_materialize_commit_span(
    *,
    expansion_id: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Open the ``dungeon.materialize.commit`` child span."""
    with Span.open(
        SPAN_DUNGEON_MATERIALIZE_COMMIT,
        {"expansion_id": expansion_id, "stage": "commit", **attrs},
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def frontier_expand_span(
    *,
    expansion_id: int,
    frontier_edge_id: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Open the ``frontier.expand`` span for the async look-ahead worker."""
    with Span.open(
        SPAN_FRONTIER_EXPAND,
        {
            "expansion_id": expansion_id,
            "frontier_edge_id": frontier_edge_id,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def frontier_lookahead_span(
    *,
    to_region: str,
    heading: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Open the ``frontier.lookahead`` span for one async look-ahead
    worker run (Plan 7 Task 7). The worker writes ``deduped`` /
    ``no_frontier_along_heading`` / ``targets`` / ``frontier_edge_id`` /
    ``expansion_id`` (success) or ``error`` / ``reason`` (terminal
    failure) onto the span — the GM-panel lie-detector for the
    background prefetch (the only way to tell the look-ahead engaged vs.
    the narrator improvising the dungeon grew)."""
    with Span.open(
        SPAN_FRONTIER_LOOKAHEAD,
        {"to_region": to_region, "heading": heading, **attrs},
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def frontier_region_transition_span(
    *,
    from_region: str,
    to_region: str,
    observers: int,
    pc_name: str = "",
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Open the ``frontier.region_transition`` span for one real
    production PER-PC region transition (Plan 7 Task 6 wiring seam,
    Movement subsystem §Q2). ``pc_name`` is which PC moved."""
    with Span.open(
        SPAN_FRONTIER_REGION_TRANSITION,
        {
            "from_region": from_region,
            "to_region": to_region,
            "observers": observers,
            "pc_name": pc_name,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


__all__ = [
    "SPAN_DUNGEON_CURATE_AUTHORED_BIND_FAILED",
    "SPAN_DUNGEON_MATERIALIZE",
    "SPAN_DUNGEON_MATERIALIZE_ATTACH",
    "SPAN_DUNGEON_MATERIALIZE_COMMIT",
    "SPAN_DUNGEON_MATERIALIZE_CURATE",
    "SPAN_DUNGEON_MATERIALIZE_DESIGN",
    "SPAN_DUNGEON_MATERIALIZE_FILL",
    "SPAN_DUNGEON_MATERIALIZE_MASK",
    "SPAN_DUNGEON_MATERIALIZE_TACTICAL",
    "SPAN_FRONTIER_EXPAND",
    "SPAN_FRONTIER_LOOKAHEAD",
    "SPAN_FRONTIER_REGION_TRANSITION",
    "dungeon_curate_authored_bind_failed_span",
    "dungeon_materialize_attach_span",
    "dungeon_materialize_commit_span",
    "dungeon_materialize_curate_span",
    "dungeon_materialize_design_span",
    "dungeon_materialize_fill_span",
    "dungeon_materialize_mask_span",
    "dungeon_materialize_span",
    "dungeon_materialize_tactical_span",
    "frontier_expand_span",
    "frontier_lookahead_span",
    "frontier_region_transition_span",
]
