"""movement subsystem dispatch handler — Intent Router live engager
(Movement Subsystem, ADR-113 / ADR-106 / ADR-055).

The router classifies a player action and emits a ``DispatchPackage``
whose ``SubsystemDispatch`` entries may include ``subsystem="movement"``
with ``params={"direction": "deeper"|"back"|"toward_exit",
"exit_descriptor": "<free text>"}``. This handler is the SINGLE
movement-resolution mechanism (one-mechanism rule): it traverses the
REAL frontier graph adjacencies, picks the next region deterministically,
and advances THIS PC's ``pc_regions`` entry via the Phase-1
``WorldStatePatch(pc_region={player_name: target})`` path — which fires
``notify_region_transition(pc_name=...)`` → frontier observer →
lookahead worker materialize. The narrator never decides movement.

No fallbacks, fail LOUD (``feedback_no_fallbacks_hard``): a movement
intent that resolves to no real adjacency, is genuinely ambiguous, or
has no ``dungeon_store`` emits an ERROR-level ``movement.unresolved``
span and returns ``directives=[honest-surface]`` + ``data["error"]`` and
applies NO patch — the PC does NOT move and is told the truth. The
handler returns recoverable failures (it does not re-raise); it raises
ONLY for a genuine programmer bug (a resolved region escaping
``neighbors()``), which the bank catches and spans.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from sidequest.agents.subsystems import SubsystemOutput
from sidequest.dungeon.region_graph.model import RegionGraph
from sidequest.dungeon.region_projection import RegionExit, project_region, requested_bearing
from sidequest.dungeon.seed_bootstrap import ENTRANCE_ID as _ENTRANCE_ID
from sidequest.game.seams import (
    SeamCrossingError,
    get_seam_resolver,
    seam_route_for,
    surface_owner_for_entrance,
)
from sidequest.game.seams.deep_descent import resolve_deep_descent
from sidequest.game.seams.surface_ascent import resolve_surface_ascent
from sidequest.game.session import GameSnapshot, WorldStatePatch
from sidequest.genre.models.world import NavigationMode, Route
from sidequest.protocol.dispatch import NarratorDirective, SubsystemDispatch, VisibilityTag
from sidequest.telemetry.spans import (
    movement_region_mode_span,
    movement_resolved_span,
    movement_unresolved_span,
)

if TYPE_CHECKING:
    from sidequest.dungeon.lookahead_worker import LookaheadWorkerHandle
    from sidequest.dungeon.persistence import DungeonStore
    from sidequest.dungeon.themes import ThemePalette
    from sidequest.genre.models.pack import GenrePack

logger = logging.getLogger(__name__)

# Seed=Expansion-0 contract's fixed entrance anchor (load_map needs it to
# rebuild the graph). Single-sourced from the public seed_bootstrap.ENTRANCE_ID
# (imported above) so this never silently diverges if the anchor id changes.

# §Q1 step 4 / O3: hardcoded kind→synonym table for v1 (exit-vocabulary in
# content YAML is a documented follow-up for Jade). ``secret`` is NEVER
# auto-matched (offering a secret exit by descriptor is reverse-Illusionism).
_KIND_SYNONYMS: dict[str, set[str]] = {
    "shaft": {"down", "drop", "shaft", "chute"},
    "chute": {"down", "drop", "shaft", "chute"},
    "stairs": {"stair", "stairs", "up", "down", "descend"},
    "corridor": {"corridor", "hall", "passage", "tunnel"},
    "secret": set(),
}

# kind tie-break order for ``direction="deeper"`` (shaft is the deepest
# descent vector, secret the least preferred).
_DEEPER_KIND_RANK: dict[str, int] = {
    "shaft": 0,
    "chute": 1,
    "stairs": 2,
    "corridor": 3,
    "secret": 4,
}


def _tokens(text: str) -> set[str]:
    """Lowercased alpha tokens for descriptor token-overlap scoring."""
    return {t for t in re.findall(r"[a-z]+", (text or "").lower())}


def _exit_sort_key(e: RegionExit) -> tuple[str, str]:
    """Stable display order for exits: by bearing, then id."""
    return (e.bearing, e.to_region_id)


def _way_phrase(e: RegionExit) -> str:
    """A player-facing description of one exit, by bearing + kind — never the
    raw region id (an ``exp001.r1`` slug in voiced prose is its own leak)."""
    if e.bearing in ("up", "down"):
        return f"the {e.kind} leading {e.bearing}"
    if e.bearing:
        return f"the {e.bearing} {e.kind}"
    return f"the {e.kind}"


def _cartography_for(*, pack: GenrePack | None, world_slug: str):
    """The active world's cartography, or None — the single pack/world probe
    feeding ``_is_region_mode`` and ``seam_route_for`` (computed once per
    dispatch; keep the discriminator single-shaped)."""
    if pack is None or not world_slug:
        return None
    worlds = getattr(pack, "worlds", None)
    if worlds is None:
        return None
    return getattr(worlds.get(world_slug), "cartography", None)


def _is_region_mode(cart) -> bool:
    """True iff the given cartography is region-mode.

    Region-mode worlds (``cartography.navigation_mode == region``) do not use
    the procedural-dungeon navigator — they carry no ``DungeonStore`` and
    resolve travel via the narration_apply heading→region path. Returns False
    (→ caller fails loud on ``no_dungeon_store``, never a silent skip) when the
    cartography is absent (``None`` from ``_cartography_for``) or
    undeterminable. The pack/world probe lives in ``_cartography_for`` —
    mirrors the ``getattr`` cartography probe used by ``narration_apply``
    (#577) and ``project_cartography_region`` so the discriminator stays
    single-shaped.
    """
    return getattr(cart, "navigation_mode", None) == NavigationMode.region


async def run_movement_dispatch(
    dispatch: SubsystemDispatch,
    *,
    snapshot: GameSnapshot,
    player_name: str,
    dungeon_store: DungeonStore | None = None,
    palette: ThemePalette | None = None,
    lookahead_handle: LookaheadWorkerHandle | None = None,
    pack: GenrePack | None = None,
) -> SubsystemOutput:
    """Resolve a coarse movement intent against the real graph and advance
    THIS PC's region (per-PC, §Q5 split-party — no party token).

    Returns an empty-directives ``SubsystemOutput`` on success (the patch
    is the engine truth; the narrator reads ``pc_regions`` downstream).
    On a recoverable failure returns a single ``must_narrate`` directive
    surfacing the truth + ``data["error"]`` and applies NO patch.
    """
    direction = str(dispatch.params.get("direction", "") or "")
    exit_descriptor = str(dispatch.params.get("exit_descriptor", "") or "")

    # --- Region-mode worlds do not use this procedural-dungeon navigator. ---
    # This handler traverses a RegionGraph loaded from a DungeonStore (the
    # procedural megadungeon / room-graph path). A cartography region-mode
    # world (navigation_mode == region, e.g. wry_whimsy/oz) has NO dungeon
    # store by design — travel is resolved deterministically by the
    # narration_apply heading→region path (sq-playtest 2026-06-02 #577), not
    # here. Treating its (expected) missing store as ``no_dungeon_store`` fired
    # an ERROR span on every move (the GM panel showed movement 7 events / 7
    # errors in oz) — a dungeon assumption leaking into region-mode. Recognize
    # the mode and step aside cleanly with an observable, NON-error
    # ``movement.region_mode`` span. This is NOT a silent fallback: a
    # room_graph world that is genuinely missing its store still fails loud on
    # ``no_dungeon_store`` below, and an undeterminable pack/world (pack=None)
    # also falls through to fail-loud rather than silently deferring.
    cart = _cartography_for(pack=pack, world_slug=snapshot.world_slug)
    if _is_region_mode(cart):
        from_region = snapshot.region_for(perspective=player_name) or ""

        # --- Story 105-2: the hybrid case de4f85c8 didn't anticipate. ---
        # A region-mode world whose current region owns a registered seam
        # route (beneath_sunden: the_dropmouth → deep_descent) IS the
        # static→procedural boundary. When the PC's region owns a seam
        # route, that seam is the region's onward boundary — ANY movement
        # intent except ``back`` crosses it; ``back`` is surface adjacency,
        # not a seam, so it stays deferred. Deferring the rest to the
        # heading→region path is what made the 59-12 handoff dead code and
        # the Deep unreachable (epic 105).
        seam_route = seam_route_for(cart, from_region)
        if seam_route is not None and direction != "back":
            try:
                crossing = get_seam_resolver(str(seam_route.to_id))(
                    snapshot=snapshot,
                    player_name=player_name,
                    route=seam_route,
                    resolved_via="surface_descent",
                    dungeon_store=dungeon_store,
                    direction=direction,
                    exit_descriptor=exit_descriptor,
                )
            except SeamCrossingError as err:
                return _unresolved(
                    snapshot=snapshot,
                    player_name=player_name,
                    reason=err.reason,
                    from_region=from_region,
                    direction=direction,
                    exit_descriptor=exit_descriptor,
                    available=[],
                    surface=err.surface,
                )
            return SubsystemOutput(
                data={
                    "to_region": crossing.to_region,
                    "from_region": from_region,
                    "resolved_via": "surface_descent",
                }
            )

        # --- Story 105-3: the reverse seam — leaving the Deep. ---
        # A PC standing on the dungeon entrance node is at the static→procedural
        # threshold seen from BELOW. Any intent except going deeper is a
        # departure: ascend back to the surface cartography region that OWNS the
        # deep crossing (the registered-kind route's from_id), via the same
        # per-PC patch path the descent uses. Symmetric to the descent rule
        # above ("any intent except back crosses down"). Without this, an
        # exit-ward intent at the entrance deferred to the heading→region path,
        # which has no surface node to head to — stranding the party below
        # (epic 105 reverse crossing). No seam owner found (a non-dungeon
        # region-mode world, or an ambiguous multi-descent map) → fall through
        # to the in-dungeon / defer logic below, never an invented surface
        # (No Silent Fallbacks).
        if from_region == _ENTRANCE_ID and direction != "deeper":
            ascent_route = surface_owner_for_entrance(cart)
            if ascent_route is not None:
                # Symmetric to the descent block above: a recoverable seam fault
                # (a malformed registered-kind route — null or unmapped from_id)
                # raises SeamCrossingError and must fail LOUD through
                # movement.unresolved (the OTEL lie-detector), never an uncaught
                # raise and never a silent region_mode defer. surface_owner_for_entrance
                # intentionally still returns the malformed route so the resolver
                # raises here and the GM panel sees the wiring fault.
                try:
                    crossing = resolve_surface_ascent(
                        snapshot=snapshot,
                        player_name=player_name,
                        route=ascent_route,
                        resolved_via="surface_ascent",
                        direction=direction,
                        exit_descriptor=exit_descriptor,
                        cartography=cart,
                    )
                except SeamCrossingError as err:
                    return _unresolved(
                        snapshot=snapshot,
                        player_name=player_name,
                        reason=err.reason,
                        from_region=from_region,
                        direction=direction,
                        exit_descriptor=exit_descriptor,
                        available=[],
                        surface=err.surface,
                    )
                return SubsystemOutput(
                    data={
                        "to_region": crossing.to_region,
                        "from_region": from_region,
                        "resolved_via": "surface_ascent",
                    }
                )

        # --- Pingpong 2026-06-12: the PC is already INSIDE the dungeon. ---
        # A region-mode hybrid world's PC who has crossed the seam stands on
        # a dungeon graph node (pc_regions == 'entrance' / 'expNNN.rN'), not
        # a cartography region. Deferring here hands the in-dungeon crawl to
        # the narration heading→region path — which cannot traverse the graph,
        # so the narrator improvises the whole dungeon (the confabulated-crawl
        # bug). When the PC's region is a live graph node, fall through to the
        # §Q1 procedural navigator below; the region-mode defer is ONLY for
        # PCs standing on surface cartography.
        _in_dungeon = (
            dungeon_store is not None
            and bool(from_region)
            and from_region in dungeon_store.load_map(entrance_id=_ENTRANCE_ID).nodes
        )
        if not _in_dungeon:
            return _defer_region_mode(
                snapshot=snapshot,
                player_name=player_name,
                from_region=from_region,
                direction=direction,
                exit_descriptor=exit_descriptor,
            )
        # In-dungeon: fall through to the §Q1 navigator below.

    # --- §Q1 step 1: no dungeon_store → non-procedural world, fail loud. ---
    # palette is threaded from the SAME lookahead handle as dungeon_store, so
    # a present store with a missing palette is a context-wiring bug — fail
    # loud on the same no_dungeon_store path (No Silent Fallbacks: never
    # project a region against a None palette).
    if dungeon_store is None or palette is None:
        return _unresolved(
            snapshot=snapshot,
            player_name=player_name,
            reason="no_dungeon_store",
            from_region="",
            direction=direction,
            exit_descriptor=exit_descriptor,
            available=[],
            surface=(
                "There is no mapped passage here to travel through; "
                "this place has no further depths to descend into."
            ),
        )

    # --- §Q1 step 2: THIS PC's current region (per-PC; never current_region). ---
    from_region = snapshot.region_for(perspective=player_name)
    if not from_region:
        # Binding/migration bug — the seated PC has no pc_regions entry.
        # Fail loud; NEVER read back the singular current_region.
        return _unresolved(
            snapshot=snapshot,
            player_name=player_name,
            reason="no_pc_region",
            from_region="",
            direction=direction,
            exit_descriptor=exit_descriptor,
            available=[],
            surface="The way ahead is unknown — your bearings have not been set.",
        )

    graph = dungeon_store.load_map(entrance_id=_ENTRANCE_ID)

    # --- §Q1 step 2b / Story 59-12: surface→deep handoff. ---
    # THIS PC's region is non-empty but NOT a node of the procedural dungeon
    # graph — it is the surface cartography region the PC was bound to by
    # init_region_location (e.g. beneath_sunden's 'ropefoot' waiting-camp,
    # cartography.starting_region). No prior seam rebinds the PC onto the graph
    # on descent: the dungeon-attach entrance-bind only fires when
    # current_region is blank, and the per-turn projection treats a surface
    # region as the surface lane. So a descent crosses surface→deep at the
    # dungeon's threshold — its ``entrance`` node. Bind THIS PC there via the
    # Phase-1 per-PC patch path (which fires the frontier transition for the
    # look-ahead worker) and resolve the crossing. A non-descent intent from
    # the surface fails LOUD (No Silent Fallbacks) — there is no dungeon route
    # to navigate until the PC has actually entered.
    if from_region not in graph.nodes:
        if direction != "deeper":
            return _unresolved(
                snapshot=snapshot,
                player_name=player_name,
                reason="surface_no_route",
                from_region=from_region,
                direction=direction,
                exit_descriptor=exit_descriptor,
                available=[],
                surface=(
                    f"{player_name} stands on the surface; the only way on "
                    f"from here is down, into the dark below."
                ),
            )
        # --- §Q1 step 2b / Story 59-12: surface→deep handoff via shared resolver.
        # One implementation, two doors: the hybrid (region-mode + seam route)
        # door is in the region-mode block above; this door serves room-graph
        # worlds whose PC is still bound to a surface cartography region. Both
        # doors call resolve_deep_descent — the duplicate inline bind is GONE.
        try:
            crossing = resolve_deep_descent(
                snapshot=snapshot,
                player_name=player_name,
                route=Route(
                    name="(synthetic) surface descent",
                    description="room-graph surface→deep handoff (59-12)",
                    from_id=from_region,
                    to_id="deep_descent",
                ),
                resolved_via="surface_descent",
                dungeon_store=dungeon_store,
                direction=direction,
                exit_descriptor=exit_descriptor,
            )
        except SeamCrossingError as err:
            return _unresolved(
                snapshot=snapshot,
                player_name=player_name,
                reason=err.reason,
                from_region=from_region,
                direction=direction,
                exit_descriptor=exit_descriptor,
                available=[],
                surface=err.surface,
            )
        return SubsystemOutput(
            data={
                "to_region": crossing.to_region,
                "from_region": from_region,
                "resolved_via": "surface_descent",
            }
        )

    proj = project_region(graph, from_region, palette)

    # --- §Q1 step 3: filter hidden exits unless the edge is discovered. ---
    discovered_routes = set(snapshot.discovered_routes or [])
    candidates: list[RegionExit] = [
        e for e in proj.exits if (not e.hidden) or (e.to_region_id in discovered_routes)
    ]

    available_ids = [e.to_region_id for e in candidates]

    if not candidates:
        return _unresolved(
            snapshot=snapshot,
            player_name=player_name,
            reason="no_candidate_edges",
            from_region=from_region,
            direction=direction,
            exit_descriptor=exit_descriptor,
            available=available_ids,
            surface=f"{player_name} finds no such way from here.",
        )

    # --- §Q1 step 4: resolve the coarse intent against the candidates. ---
    resolved, resolved_via, ambiguous = _resolve(
        candidates=candidates,
        graph=graph,
        from_region=from_region,
        from_depth=proj.depth_score,
        direction=direction,
        exit_descriptor=exit_descriptor,
        discovered_regions=list(snapshot.discovered_regions or []),
    )

    if ambiguous:
        ways = ", ".join(_way_phrase(e) for e in sorted(candidates, key=_exit_sort_key))
        return _unresolved(
            snapshot=snapshot,
            player_name=player_name,
            reason="ambiguous_descriptor",
            from_region=from_region,
            direction=direction,
            exit_descriptor=exit_descriptor,
            available=available_ids,
            surface=(f"{player_name} could go more than one way from here: {ways}. Which way?"),
        )

    if resolved is None:
        return _unresolved(
            snapshot=snapshot,
            player_name=player_name,
            reason="no_candidate_edges",
            from_region=from_region,
            direction=direction,
            exit_descriptor=exit_descriptor,
            available=available_ids,
            surface=f"{player_name} finds no such way from here.",
        )

    chosen = resolved
    target_id = chosen.to_region_id

    # --- §Q1 step 6 / invariant: resolved region MUST be a real neighbor. ---
    if target_id not in graph.neighbors(from_region):
        # Programmer bug — the resolution escaped the adjacency set. RAISE
        # (the bank records the error span); never move into a phantom.
        raise RuntimeError(
            f"movement resolution escaped neighbors(): resolved {target_id!r} "
            f"is not adjacent to {from_region!r} (neighbors="
            f"{sorted(graph.neighbors(from_region))})"
        )

    # --- §Q3: sync-materialize-then-move for an uncommitted target. ---
    target_pre_materialized = target_id in graph.nodes
    if not target_pre_materialized:
        ok = await _sync_materialize(
            lookahead_handle=lookahead_handle,
            dungeon_store=dungeon_store,
            from_region=from_region,
            target_id=target_id,
            snapshot=snapshot,
        )
        if not ok:
            return _unresolved(
                snapshot=snapshot,
                player_name=player_name,
                reason="no_candidate_edges",
                from_region=from_region,
                direction=direction,
                exit_descriptor=exit_descriptor,
                available=available_ids,
                surface=f"The way to {target_id} has not yet formed.",
            )

    # --- §Q2: advance THIS PC via the Phase-1 per-PC patch path. ---
    snapshot.apply_world_patch(WorldStatePatch(pc_region={player_name: target_id}))

    party_split_after = snapshot.region_for() is None

    with movement_resolved_span(
        pc_name=player_name,
        from_region=from_region,
        to_region=target_id,
    ) as span:
        span.set_attribute("intent.direction", direction)
        span.set_attribute("intent.exit_descriptor", exit_descriptor)
        span.set_attribute("resolved_via", resolved_via)
        span.set_attribute("candidate_exits", available_ids)
        span.set_attribute("edge_kind", chosen.kind)
        span.set_attribute("target_pre_materialized", target_pre_materialized)
        # A move always enqueues next-ring look-ahead around the new region
        # (the §Q3 move-then-materialize ring); the sync-materialize for an
        # uncommitted target is a SEPARATE prior step.
        span.set_attribute("materialize_triggered", True)
        span.set_attribute("party_split_after", party_split_after)

    logger.debug(
        "movement.resolved pc=%s from=%s to=%s via=%s kind=%s pre_materialized=%s",
        player_name,
        from_region,
        target_id,
        resolved_via,
        chosen.kind,
        target_pre_materialized,
    )
    return SubsystemOutput(
        data={
            "to_region": target_id,
            "from_region": from_region,
            "resolved_via": resolved_via,
        }
    )


def _resolve(
    *,
    candidates: list[RegionExit],
    graph: RegionGraph,
    from_region: str,
    from_depth: float | None,
    direction: str,
    exit_descriptor: str,
    discovered_regions: list[str],
) -> tuple[RegionExit | None, str, bool]:
    """Deterministic §Q1 resolution. Returns (chosen, resolved_via, ambiguous)."""
    # Total deterministic baseline ordering: ascending to_region_id.
    ordered = sorted(candidates, key=lambda e: e.to_region_id)

    # --- bearing match (highest priority) → the player named a direction. ---
    # "I go north", "down the stairs", "the eastern passage" — each exit
    # carries a distinct bearing (assign_bearings), so a named bearing
    # resolves to AT MOST one edge: no tie is possible, and the 4-way "the
    # corridor ahead" ambiguity that made movement unresolvable is gone the
    # moment the narrator names the ways out by their bearings. A named
    # bearing that matches nothing falls through to the coarse/descriptor
    # paths (so "north corridor" can still land on the corridor token) rather
    # than hard-refusing on the bearing alone.
    want_bearing = requested_bearing(exit_descriptor) or requested_bearing(direction)
    if want_bearing:
        matched = [e for e in ordered if e.bearing == want_bearing]
        if len(matched) == 1:
            return matched[0], "bearing", False

    # --- exit_descriptor present → token-overlap match. ---
    if exit_descriptor.strip():
        want = _tokens(exit_descriptor)
        scored: list[tuple[int, RegionExit]] = []
        for e in ordered:
            if e.kind == "secret":
                # never auto-matched by descriptor (reverse-Illusionism).
                continue
            surface = _tokens(e.to_region_id) | _KIND_SYNONYMS.get(e.kind, set())
            scored.append((len(want & surface), e))
        scored = [s for s in scored if s[0] > 0]
        if not scored:
            # sq-playtest 2026-06-12 (beneath_sunden-6 t6/t7): the router
            # passes the player's words through verbatim, so the descriptor
            # is often FLAVOR ("the heart of the dungeon"), not a way-name.
            # A descriptor that matches NOTHING must not veto an otherwise
            # unambiguous coarse direction — "I go deeper, into the heart
            # of the dungeon" was refused twice as no_candidate_edges while
            # a real deeper corridor existed. Fall back to the direction
            # resolution (resolved_via carries the fallback for the GM
            # panel). No direction → the honest refusal stands. An
            # AMBIGUOUS descriptor (several real ways tie) still refuses
            # below — "which corridor?" is a fair question; "no such way"
            # for "go deeper" is a stonewall.
            if direction in ("deeper", "back", "toward_exit"):
                chosen, via, ambiguous = _resolve(
                    candidates=candidates,
                    graph=graph,
                    from_region=from_region,
                    from_depth=from_depth,
                    direction=direction,
                    exit_descriptor="",
                    discovered_regions=discovered_regions,
                )
                return chosen, f"descriptor_fallback_{via}", ambiguous
            return None, "descriptor_match", False
        scored.sort(key=lambda s: (-s[0], s[1].to_region_id))
        if len(scored) >= 2 and scored[0][0] == scored[1][0]:
            # Genuine ambiguity — top-2 token scores tied. Fail loud.
            return None, "descriptor_match", True
        return scored[0][1], "descriptor_match", False

    # --- direction="deeper" → strictly-greater depth_score, max delta. ---
    if direction == "deeper":
        cur = from_depth if from_depth is not None else 0.0
        deeper: list[tuple[float, int, str, RegionExit]] = []
        for e in ordered:
            node = graph.nodes.get(e.to_region_id)
            nd = node.depth_score if (node and node.depth_score is not None) else None
            if nd is None or nd <= cur:
                continue
            deeper.append(
                (
                    -(nd - cur),  # max depth delta first
                    _DEEPER_KIND_RANK.get(e.kind, 99),  # kind tie-break
                    e.to_region_id,  # total deterministic tie-break
                    e,
                )
            )
        if not deeper:
            return None, "depth_delta", False
        deeper.sort(key=lambda t: (t[0], t[1], t[2]))
        return deeper[0][3], "depth_delta", False

    # --- direction="back" → smallest depth_score discovered neighbor. ---
    if direction == "back":
        # "way they came" tie-break: most-recently-prior in discovered_regions.
        recency = {rid: i for i, rid in enumerate(discovered_regions)}
        back: list[tuple[float, int, str, RegionExit]] = []
        for e in ordered:
            if e.to_region_id not in recency:
                continue
            node = graph.nodes.get(e.to_region_id)
            nd = node.depth_score if (node and node.depth_score is not None) else 0.0
            back.append(
                (
                    nd,  # smallest depth first (toward surface)
                    -recency[e.to_region_id],  # most-recently-prior first
                    e.to_region_id,
                    e,
                )
            )
        if not back:
            return None, "depth_delta", False
        back.sort(key=lambda t: (t[0], t[1], t[2]))
        return back[0][3], "depth_delta", False

    # --- direction="toward_exit" → BFS step toward the entrance. ---
    if direction == "toward_exit":
        dist = graph.bfs_dist(graph.entrance_id)
        cand_ids = {e.to_region_id for e in ordered}
        stepped: list[tuple[int, str, RegionExit]] = []
        for e in ordered:
            d = dist.get(e.to_region_id)
            if d is None:
                continue
            stepped.append((d, e.to_region_id, e))
        if not stepped:
            return None, "bfs_to_exit", False
        # The neighbor on the shortest path to entrance has the smallest
        # bfs distance; tie-break ascending to_region_id.
        stepped.sort(key=lambda t: (t[0], t[1]))
        _ = cand_ids
        return stepped[0][2], "bfs_to_exit", False

    # Unknown direction with no descriptor → no resolution.
    return None, "depth_delta", False


async def _sync_materialize(
    *,
    lookahead_handle: LookaheadWorkerHandle | None,
    dungeon_store: DungeonStore,
    from_region: str,
    target_id: str,
    snapshot: GameSnapshot,
) -> bool:
    """§Q3: synchronously materialize the single approaching frontier edge
    rooted at ``from_region`` so ``target_id`` becomes a real node BEFORE
    the PC moves. Reuses the worker's own ``_materialize_edge`` path (no
    parallel materialize mechanism). Returns True iff ``target_id`` exists
    in a fresh ``load_map`` afterward; on any failure returns False (the
    caller fails loud and does NOT move).
    """
    if lookahead_handle is None:
        return False
    frontier = dungeon_store.load_frontier()
    rooted = [fe for fe in frontier if fe.from_region_id == from_region]
    if not rooted:
        return False
    rooted.sort(key=lambda fe: (fe.spawn_depth_score, fe.frontier_edge_id))
    edge = rooted[0]
    try:
        await lookahead_handle._materialize_edge(  # noqa: SLF001 — reuse the one worker path
            edge=edge, to_region=from_region, snapshot=snapshot
        )
    except Exception as exc:  # noqa: BLE001 — recoverable: fail loud via caller's unresolved span
        logger.warning(
            "movement.sync_materialize_failed from=%s target=%s exc=%s",
            from_region,
            target_id,
            exc,
        )
        return False
    fresh = dungeon_store.load_map(entrance_id=_ENTRANCE_ID)
    return target_id in fresh.nodes


def _defer_region_mode(
    *,
    snapshot: GameSnapshot,
    player_name: str,
    from_region: str,
    direction: str,
    exit_descriptor: str,
) -> SubsystemOutput:
    """Region-mode defer: the narration_apply heading→region path owns the
    advance for a PC standing on surface cartography. Observable (non-error
    ``movement.region_mode`` span) — NOT a silent fallback; see the
    region-mode block in ``run_movement_dispatch``."""
    with movement_region_mode_span(
        pc_name=player_name,
        from_region=from_region,
    ) as span:
        span.set_attribute("intent.direction", direction)
        span.set_attribute("intent.exit_descriptor", exit_descriptor)
        span.set_attribute("world_slug", snapshot.world_slug)
    logger.debug(
        "movement.region_mode pc=%s world=%s direction=%s descriptor=%r "
        "(deferred to narration_apply heading→region path)",
        player_name,
        snapshot.world_slug,
        direction,
        exit_descriptor,
    )
    # No patch: the heading→region path owns the advance. No directive:
    # the narrator resolves the move in prose. No error: this is the
    # expected navigation mode, not a failure.
    return SubsystemOutput(data={"resolved_via": "region_mode_deferred"})


def _unresolved(
    *,
    snapshot: GameSnapshot,
    player_name: str,
    reason: str,
    from_region: str,
    direction: str,
    exit_descriptor: str,
    available: list[str],
    surface: str,
) -> SubsystemOutput:
    """Emit the ERROR ``movement.unresolved`` span + an honest narrator
    surface directive; apply NO patch (the PC does not move). §Q4."""
    with movement_unresolved_span(
        pc_name=player_name,
        reason=reason,
        from_region=from_region,
    ) as span:
        span.set_attribute("intent.direction", direction)
        span.set_attribute("intent.exit_descriptor", exit_descriptor)
        span.set_attribute("available_exits", available)
    logger.warning(
        "movement.unresolved pc=%s reason=%s from=%s direction=%s descriptor=%r available=%s",
        player_name,
        reason,
        from_region,
        direction,
        exit_descriptor,
        available,
    )
    # The directive is a GM instruction, not just in-fiction prose: the
    # narrator must NOT paper over a refused move with a confabulated room
    # (sq-playtest 2026-06-12 — narrator flipped the title to "First
    # Corridor" and seeded a monster while the PC stayed frozen at the
    # entrance). Make the non-advance explicit and hand it the honest surface
    # to voice.
    payload = (
        f"MOVEMENT REFUSED ({reason}): {player_name} has NOT moved and is still in "
        f"the same region. Do NOT change the location title or scene heading, do NOT "
        f"describe entering/traversing/arriving anywhere, and do NOT introduce a new "
        f"room or its contents. In fiction, surface this honestly and — if the way was "
        f"ambiguous — ask which of the listed exits they take (name them by their "
        f"bearings). Honest text to voice: {surface}"
    )
    directive = NarratorDirective(
        kind="must_narrate",
        payload=payload,
        visibility=VisibilityTag(visible_to="all"),
    )
    return SubsystemOutput(directives=[directive], data={"error": reason})


__all__ = ["run_movement_dispatch"]
