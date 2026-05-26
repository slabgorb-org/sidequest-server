"""Beneath Sünden Plan 7 Task 7 — async look-ahead WORKER.

This is the **consumer side** of Task 6's frontier-approach producer
seam (``frontier_hook.register_frontier_observer`` /
``notify_region_transition``). When the party transitions toward an
unexpanded frontier edge, the pipeline runs *ahead of the party*
asynchronously: the worker materialises the next expansion in the
background so the dungeon has already grown by the time the party
arrives. ``lookahead_breadth`` controls how many edges along the heading
are prefetched (default 1 — only the single approaching edge).

THE CENTRAL DESIGN CONSTRAINT
-----------------------------
``notify_region_transition`` is **synchronous**, called from
``GameSnapshot._apply_world_patch_inner`` AFTER ``snap.current_region``
is already set (the region transition ALREADY SUCCEEDED). The registered
observer here is a **thin sync function** that *schedules* the async
worker as a fire-and-forget background task and returns immediately
WITHOUT raising. The observer MUST NOT let the worker's failure (or its
own scheduling failure) propagate synchronously out of
``notify_region_transition`` → ``_apply_world_patch_inner`` — doing so
would abort the party's region crossing for a mere background-prefetch
problem (core-gameplay fragility). "Loud" therefore means a
GM-panel-visible terminal ``frontier.lookahead`` OTEL span (the
lie-detector), NEVER an exception into the synchronous transition path.
A background failure is never silently swallowed — the done-callback
surfaces it on the terminal span.

Idempotency
-----------
An in-flight ``frontier_edge_id`` registry serialises rapid successive
approach signals: a second signal for an already-in-flight materialisation
is a NO-OP (``deduped=true`` on its span — the lie-detector proof), NOT a
double-materialise. The in-flight marker is cleared when the worker
finishes (success OR failure) so a later genuine re-approach can retry.

Dependency injection
--------------------
``register_lookahead_worker`` closes over an EXPLICIT session context
(persistence/bundle/palette/pack_tropes/claude_client/campaign_seed/
lookahead_breadth) — the snapshot delivered by the producer carries none
of these, so they are passed in, never magically sourced (No Silent
Fallbacks). It returns a :class:`LookaheadWorkerHandle` so the session
lifecycle can ``.unregister()`` at teardown.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from sidequest.dungeon.frontier_hook import (
    register_frontier_observer,
    unregister_frontier_observer,
)
from sidequest.dungeon.materializer import MaterializationRequest, materialize
from sidequest.dungeon.persistence import DungeonStore, FrontierEdge
from sidequest.telemetry.spans.dungeon_materialize import frontier_lookahead_span

__all__ = [
    "LookaheadWorkerHandle",
    "register_lookahead_worker",
]

# The entrance region id is the Seed=Expansion-0 contract's fixed anchor
# (every Plan 5/7 commit persists the surface entrance as expansion 0
# under this id; load_map needs the entrance_id to rebuild the graph).
_ENTRANCE_ID = "entrance"


def _select_frontier_edges(
    frontier: list[FrontierEdge],
    *,
    to_region: str,
    lookahead_breadth: int,
) -> list[FrontierEdge]:
    """Pick the unexpanded frontier edge(s) the party is approaching.

    The party just crossed INTO ``to_region``. The unexpanded frontier
    edges *ahead of the party along the current heading* are exactly the
    edges rooted at ``to_region`` (``from_region_id == to_region``): each
    is a boundary the next expansion would push outward FROM the region
    the party just entered (the Task-6 ``_new_frontier_edges`` contract —
    every derived edge's ``from_region_id`` is a real region node id).

    ``lookahead_breadth == 1`` → ONLY the single nearest approaching edge.
    ``> 1`` → the ``lookahead_breadth`` nearest such edges. "Nearest"
    along the heading = shallowest ``spawn_depth_score`` (the depth
    gradient increases outward from the party — the closest edge spawns
    at the lowest depth). Ties broken by ``frontier_edge_id`` for a
    deterministic, reproducible selection (No Silent Fallbacks: not an
    arbitrary set order).

    No edge rooted at ``to_region`` → empty list (the genuine no-op case:
    not every transition approaches the frontier — the caller records
    this on OTEL so the GM panel can tell "nothing to do" from "broken").
    """
    rooted = [fe for fe in frontier if fe.from_region_id == to_region]
    rooted.sort(key=lambda fe: (fe.spawn_depth_score, fe.frontier_edge_id))
    return rooted[: max(0, lookahead_breadth)]


def _attach_region_ids(graph: Any, from_region_id: str) -> list[str]:
    """The explored regions the look-ahead expansion attaches to.

    ``generate_expansion`` requires a non-seed expansion to attach to
    ``>= 2`` distinct explored regions ("no single chokepoint" — the
    Jaquays loopful invariant). A single approaching frontier edge gives
    ONE push-off region (its ``from_region_id``); the other attach
    points are that region's REAL graph neighbours (already-explored,
    already-committed regions it is connected to). This is not an
    invented set: every id is a real node in the live ``RegionGraph``
    (No Silent Fallbacks). If ``from_region_id`` has no neighbour the
    dungeon genuinely has a single chokepoint there — ``generate_expansion``
    then raises loudly (the worker surfaces it on the terminal span; we
    do NOT paper over a real topology problem with a fake second attach
    point)."""
    attach = [from_region_id]
    for nb in graph.neighbors(from_region_id):
        if nb not in attach:
            attach.append(nb)
    return attach


@dataclass
class LookaheadWorkerHandle:
    """Registration handle for the async look-ahead worker.

    Closes over the explicit session context (DI — never magically
    sourced). The session lifecycle ``.unregister()``s at teardown.
    ``.drain()`` awaits all in-flight look-ahead tasks (deterministic
    test draining + a clean shutdown join; production fire-and-forget
    does not require it but it is harmless to await an empty set)."""

    persistence: DungeonStore
    bundle: Any
    palette: Any
    pack_tropes: Any
    claude_client: Any
    campaign_seed: int
    lookahead_breadth: int = 1
    # Story 55-1 / ADR-109: the session genre + world the materializer's
    # YAML emit needs to resolve ``<pack_root>/worlds/<world>``. Default
    # empty — the materializer's emit branch is a no-op when either is
    # absent (test fixtures keep the older worker shape valid).
    genre_slug: str = ""
    world_slug: str = ""
    _in_flight: dict[str, asyncio.Task[None]] = field(default_factory=dict, init=False, repr=False)

    def _observer(
        self,
        *,
        snapshot: Any,
        pc_name: str,
        from_region: str | None,
        to_region: str,
    ) -> None:
        """The THIN SYNC observer registered on Task 6's producer.

        Movement subsystem §Q2: ``pc_name`` is which PC moved (split-party
        legibility — threaded into the lookahead span for GM visibility). The
        worker still materializes the frontier around ``to_region``; pc_name is
        informational this phase.

        Schedules the async worker fire-and-forget and returns
        immediately WITHOUT raising — the central constraint. The
        region transition already succeeded; a prefetch problem must
        never abort the party's crossing.

        No Silent Fallbacks: if there is no running event loop this is a
        real production-contract violation (the observer is reached only
        from inside the uvicorn/async session loop) — it is surfaced
        loudly on the terminal span, NOT silently no-op'd. It still does
        not re-raise into the synchronous transition.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop — a real contract violation, but the
            # transition already happened: surface loud-on-span, do not
            # propagate into the sync path.
            with frontier_lookahead_span(to_region=to_region, heading=from_region or "") as span:
                span.set_attribute("pc_name", pc_name)
                span.set_attribute("error", "no_running_event_loop")
                span.set_attribute(
                    "reason",
                    "frontier look-ahead observer reached with no running "
                    "asyncio loop — the production contract is that this "
                    "fires from inside the async session loop; the dungeon "
                    "did NOT prefetch (No Silent Fallbacks)",
                )
            return

        # CENTRAL CONSTRAINT: everything below runs SYNCHRONOUSLY inside
        # notify_region_transition (frontier_hook explicitly does NOT
        # swallow observer exceptions). persistence.load_frontier() raises
        # DatabaseError/SerializationError on a bad/corrupt save, and
        # _select_frontier_edges / _schedule could raise too. A
        # background-prefetch failure must NEVER abort the party's region
        # crossing — so the ENTIRE post-get_running_loop sync body is
        # guarded: on any exception, surface it LOUD on a terminal routed
        # span (GM-panel-visible — the dungeon failed to prefetch) and
        # return WITHOUT re-raising (the identical loud-on-span / no-
        # re-raise pattern as the no-running-loop branch above).
        try:
            frontier = self.persistence.load_frontier()
            targets = _select_frontier_edges(
                frontier,
                to_region=to_region,
                lookahead_breadth=self.lookahead_breadth,
            )

            if not targets:
                # The genuine no-op case (not a silent skip): observable
                # so the GM panel can tell "nothing to do" from
                # "look-ahead broken".
                with frontier_lookahead_span(
                    to_region=to_region, heading=from_region or ""
                ) as span:
                    span.set_attribute("pc_name", pc_name)
                    span.set_attribute("no_frontier_along_heading", True)
                    span.set_attribute("targets", 0)
                return

            self._schedule(loop, edges=targets, to_region=to_region, snapshot=snapshot)
        except Exception as exc:  # noqa: BLE001 — central constraint: never re-raise into the sync transition
            with frontier_lookahead_span(to_region=to_region, heading=from_region or "") as span:
                span.set_attribute("pc_name", pc_name)
                span.set_attribute("error", type(exc).__name__)
                span.set_attribute(
                    "reason",
                    f"frontier look-ahead observer body failed before "
                    f"scheduling ({exc}) — the region transition already "
                    f"succeeded; the dungeon did NOT prefetch but the "
                    f"party's crossing is NOT aborted (central constraint)",
                )
            return

    def _schedule(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        edges: list[FrontierEdge],
        to_region: str,
        snapshot: Any,
    ) -> None:
        """Idempotent fire-and-forget scheduling for ONE approach signal.

        The in-flight check + insert happens SYNCHRONOUSLY before
        ``create_task`` so two near-simultaneous approach signals for the
        same edge dedupe to exactly one materialisation (concurrency-safe:
        ``notify_region_transition`` runs on the single event-loop thread,
        and the markers are set before the await point — no task can
        observe the gap). A per-edge dedupe is GM-panel-visible: an
        already-in-flight edge emits its own ``frontier.lookahead`` span
        with ``deduped=true`` (the lie-detector proof, NOT a
        double-materialise).

        When a single signal targets N edges (``lookahead_breadth>1``)
        the non-deduped edges are materialised SERIALLY inside ONE
        background task (``_materialize_edges``): the party is not
        waiting on prefetch, so serial-in-background is faithful to
        "ahead of the party asynchronously" AND removes the
        expansion_id race — each edge's ``materialize`` (incl. its
        commit) completes before the next reads ``max(expansion_id)+1``.
        """
        fresh: list[FrontierEdge] = []
        for edge in edges:
            feid = edge.frontier_edge_id
            if feid in self._in_flight:
                with frontier_lookahead_span(to_region=to_region, heading=edge.heading) as span:
                    span.set_attribute("frontier_edge_id", feid)
                    span.set_attribute("deduped", True)
                    span.set_attribute("targets", 1)
                continue
            fresh.append(edge)

        if not fresh:
            return

        task = loop.create_task(
            self._materialize_edges(edges=fresh, to_region=to_region, snapshot=snapshot)
        )
        # One task processes all `fresh` edges serially; every edge's
        # marker holds the SAME task so a same-edge re-approach dedupes
        # while any edge in the batch is still in flight.
        feids = [e.frontier_edge_id for e in fresh]
        for feid in feids:
            self._in_flight[feid] = task

        def _done(t: asyncio.Task[None], _feids: list[str] = feids) -> None:
            # Clear EVERY in-flight marker on finish (success OR failure)
            # so a later genuine re-approach can retry.
            for _feid in _feids:
                self._in_flight.pop(_feid, None)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                # A bare create_task SWALLOWS exceptions — surface the
                # background failure LOUDLY on a terminal routed span so
                # the GM panel sees the dungeon failed to grow. Never
                # propagate into the sync transition (it already
                # returned); never silently swallow.
                with frontier_lookahead_span(to_region=to_region, heading=_feids[0]) as span:
                    span.set_attribute("frontier_edge_id", _feids[0])
                    span.set_attribute("error", type(exc).__name__)
                    span.set_attribute("reason", str(exc))

        task.add_done_callback(_done)

    async def _materialize_edges(
        self, *, edges: list[FrontierEdge], to_region: str, snapshot: Any
    ) -> None:
        """Materialise the batch of approaching edges SERIALLY in one
        background task.

        Serial (not gathered) is deliberate and race-free: each edge's
        ``materialize`` — including Task 6's commit — completes BEFORE
        the next edge reads ``max(expansion_id)+1``, so parallel tasks
        can never read the same max and collide on ``exp00N.rX`` ids.
        All N are prefetch (the party is not waiting), so
        serial-in-background is faithful to "ahead of the party
        asynchronously". An exception on any edge propagates out of the
        task (the done-callback surfaces it loud-on-span); the central
        constraint is unaffected — this runs in the background, never in
        the sync transition."""
        for edge in edges:
            await self._materialize_edge(edge=edge, to_region=to_region, snapshot=snapshot)

    async def _materialize_edge(self, *, edge: FrontierEdge, to_region: str, snapshot: Any) -> None:
        """The async worker body: build the request + run the real
        five-stage pipeline for ONE approaching frontier edge.

        The committed expansion becomes live atomically — Task 6 owns
        the commit transaction. The worker just ``await``s
        ``materialize``; it adds NO commit/rollback of its own and never
        races Plan 5's transaction primitive (serialise through
        ``materialize``/Task-6's one-txn)."""
        with frontier_lookahead_span(to_region=to_region, heading=edge.heading) as span:
            span.set_attribute("frontier_edge_id", edge.frontier_edge_id)
            span.set_attribute("deduped", False)
            span.set_attribute("targets", 1)

            # The next expansion_id = one past the highest committed
            # expansion (the entrance is expansion 0; the gradient
            # continues outward). Read from the live save so a fresh
            # process resumes correctly (save-is-truth). Safe against the
            # breadth>1 race because _materialize_edges runs edges
            # serially — the prior edge's commit lands before this read.
            # v1: full load_map for max expansion_id — a dedicated store
            # max_expansion_id() is a later optimization (no behavior change).
            graph = self.persistence.load_map(entrance_id=_ENTRANCE_ID)
            next_expansion_id = max((n.expansion_id for n in graph.nodes.values()), default=0) + 1
            span.set_attribute("expansion_id", next_expansion_id)

            request = MaterializationRequest.build(
                campaign_seed=self.campaign_seed,
                expansion_id=next_expansion_id,
                frontier_edge=edge,
                attach_region_ids=_attach_region_ids(graph, edge.from_region_id),
                heading=edge.heading,
                burst_magnitude=3,
                lookahead_breadth=self.lookahead_breadth,
                frontier=[edge],
                # Story 55-1: thread the session slugs so the
                # materializer's post-commit YAML emit knows where to
                # write.
                genre_slug=self.genre_slug,
                world_slug=self.world_slug,
            )
            await materialize(
                request,
                graph=graph,
                bundle=self.bundle,
                palette=self.palette,
                persistence=self.persistence,
                snapshot=snapshot,
                pack_tropes=self.pack_tropes,
                claude_client=self.claude_client,
            )

    async def drain(self) -> None:
        """Await all in-flight look-ahead tasks (deterministic test
        draining + a clean shutdown join). Exceptions are NOT re-raised
        here — the done-callback already surfaced them on the terminal
        span (the loud-on-span-only contract); re-raising would defeat
        the fire-and-forget design."""
        while self._in_flight:
            tasks = list(self._in_flight.values())
            await asyncio.gather(*tasks, return_exceptions=True)

    def unregister(self) -> None:
        """Remove the observer from Task 6's producer registry (teardown
        must be safe to call unconditionally)."""
        unregister_frontier_observer(self._observer)


def register_lookahead_worker(
    *,
    persistence: DungeonStore,
    bundle: Any,
    palette: Any,
    pack_tropes: Any,
    claude_client: Any,
    campaign_seed: int,
    lookahead_breadth: int = 1,
    genre_slug: str = "",
    world_slug: str = "",
) -> LookaheadWorkerHandle:
    """Register the async look-ahead worker as Task 6's consuming
    frontier-approach observer.

    Closes over the EXPLICIT session context (DI — the snapshot the
    producer delivers carries none of these). Returns a
    :class:`LookaheadWorkerHandle`; the live session lifecycle calls
    ``.unregister()`` at teardown. ``register_frontier_observer`` is
    idempotent per identity, so a re-entrant session setup cannot
    double-enqueue.

    ``genre_slug`` / ``world_slug`` (Story 55-1): the session's genre +
    world the materializer's post-commit YAML emit resolves into a
    world directory. Default empty for back-compat — the emit is a
    no-op when either is absent."""
    handle = LookaheadWorkerHandle(
        persistence=persistence,
        bundle=bundle,
        palette=palette,
        pack_tropes=pack_tropes,
        claude_client=claude_client,
        campaign_seed=campaign_seed,
        lookahead_breadth=lookahead_breadth,
        genre_slug=genre_slug,
        world_slug=world_slug,
    )
    register_frontier_observer(handle._observer)
    return handle
