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
from sidequest.dungeon.region_projection import RegionExit, project_region
from sidequest.game.session import GameSnapshot, WorldStatePatch
from sidequest.protocol.dispatch import NarratorDirective, SubsystemDispatch, VisibilityTag
from sidequest.telemetry.spans import movement_resolved_span, movement_unresolved_span

if TYPE_CHECKING:
    from sidequest.dungeon.lookahead_worker import LookaheadWorkerHandle
    from sidequest.dungeon.persistence import DungeonStore
    from sidequest.dungeon.themes import ThemePalette

logger = logging.getLogger(__name__)

# Seed=Expansion-0 contract's fixed entrance anchor (load_map needs it to
# rebuild the graph). Mirrors lookahead_worker._ENTRANCE_ID /
# seed_bootstrap.ENTRANCE_ID — kept local so the handler does not reach
# into the worker's private name.
_ENTRANCE_ID = "entrance"

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


async def run_movement_dispatch(
    dispatch: SubsystemDispatch,
    *,
    snapshot: GameSnapshot,
    player_name: str,
    dungeon_store: DungeonStore | None = None,
    palette: ThemePalette | None = None,
    lookahead_handle: LookaheadWorkerHandle | None = None,
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
        return _unresolved(
            snapshot=snapshot,
            player_name=player_name,
            reason="ambiguous_descriptor",
            from_region=from_region,
            direction=direction,
            exit_descriptor=exit_descriptor,
            available=available_ids,
            surface=(
                f"{player_name} could mean any of several ways from here: "
                f"{', '.join(sorted(available_ids))}."
            ),
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
    directive = NarratorDirective(
        kind="must_narrate",
        payload=surface,
        visibility=VisibilityTag(visible_to="all"),
    )
    return SubsystemOutput(directives=[directive], data={"error": reason})


__all__ = ["run_movement_dispatch"]
