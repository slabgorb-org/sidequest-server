"""Beneath Sünden Plan 7 Task 7 — async look-ahead WORKER tests.

Three plan bullets, TDD:

  1. Idempotency: two rapid approach signals for the same frontier edge
     → EXACTLY ONE materialisation (one expansion committed, not two; the
     ``deduped=true`` span attribute proves the dedupe — the lie-detector).
  2. ``lookahead_breadth``: =1 materialises only the heading edge; raising
     it materialises the near-frontier set; default is 1.
  3. Worker exception surfaces LOUD on a terminal ``frontier.lookahead``
     span AND does NOT propagate into the synchronous
     ``apply_world_patch`` region transition (the central constraint).

No mocking of the dungeon/persistence/region_graph layer — real
``DungeonStore`` on a real connection, real ``materialize()`` pipeline
through Tasks 1–6, real ``frontier_hook`` producer. The ONLY mock is the
curation LLM call (an injected ToolingLlmClient-shaped fake — the Task-4
SDK precedent; never a real network call).
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _restore_frontier_observers() -> Any:
    """Belt-and-suspenders: unconditionally restore
    ``frontier_hook._OBSERVERS`` after every test in this module so
    observer registration cannot leak into the ~6500-test suite (the
    Task-6 wiring-test fixture pattern, reused verbatim)."""
    from sidequest.dungeon import frontier_hook

    before = list(frontier_hook._OBSERVERS)
    try:
        yield
    finally:
        frontier_hook._OBSERVERS[:] = before


def _mem_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


async def _seed_expansion_one(store: Any) -> Any:
    """Run the REAL coordinator for expansion 1 so the store carries a
    committed seed + expansion 1 + REAL unexpanded frontier edges rooted
    at exp001.r* (Task 6 derived them). Returns the resolved palette."""
    from tests.dungeon.test_materializer import (
        _commit_palette,
        _materialize_full,
        _seed_graph_themed,
    )

    theme_id = "lookahead_unit_crypt"
    palette = _commit_palette(theme_id)
    graph = _seed_graph_themed(theme_id)
    await _materialize_full(graph=graph, palette=palette, store=store)
    return palette


def _otel_in_memory() -> tuple[Any, Any, Any]:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider, provider.get_tracer("test")


def _register(store: Any, palette: Any, *, lookahead_breadth: int = 1) -> Any:
    from sidequest.dungeon.lookahead_worker import register_lookahead_worker
    from tests.dungeon.test_materializer import (
        _attach_pack,
        _real_cookbook_bundle,
        _reflecting_sdk_client,
    )

    return register_lookahead_worker(
        persistence=store,
        bundle=_real_cookbook_bundle(),
        palette=palette,
        pack_tropes=_attach_pack("cave_in"),
        claude_client=_reflecting_sdk_client(),
        campaign_seed=7,
        lookahead_breadth=lookahead_breadth,
    )


def _fresh_snapshot(region: str) -> Any:
    from sidequest.game.session import GameSnapshot

    snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="beneath_sunden")
    snap.current_region = "entrance"
    return snap


# ---------------------------------------------------------------------------
# Bullet 1: idempotency — two rapid signals → exactly one materialisation
# ---------------------------------------------------------------------------


async def test_two_rapid_signals_same_edge_materialize_once_deduped_span() -> None:
    """Two rapid approach signals for the SAME frontier edge → EXACTLY
    ONE materialisation (one new expansion committed, not two) and the
    second signal emits a ``frontier.lookahead`` span with
    ``deduped=true`` (the lie-detector proof the in-flight dedupe ran).

    Decisive: must FAIL if the in-flight dedupe is removed (two materialise
    runs would commit two expansions / collide on the frozen region ids)."""
    import sidequest.telemetry.spans as _spans_module
    from sidequest.dungeon.persistence import DungeonStore
    from sidequest.game.session import WorldStatePatch
    from sidequest.telemetry.spans.dungeon_materialize import (
        SPAN_FRONTIER_LOOKAHEAD,
    )

    conn = _mem_conn()
    store = DungeonStore(conn)
    store.ensure_schema()
    palette = await _seed_expansion_one(store)

    frontier = store.load_frontier()
    target = frontier[0].from_region_id

    exporter, _provider, real_tracer = _otel_in_memory()
    original_tracer_fn = _spans_module.tracer
    _spans_module.tracer = lambda: real_tracer  # type: ignore[method-assign]

    obs = _register(store, palette)
    try:
        snap = _fresh_snapshot(target)
        # TWO rapid region-transition signals for the SAME edge, before
        # any task gets a chance to run (synchronous back-to-back applies
        # on the single event-loop thread — the rapid-successive case).
        snap.current_region = "entrance"
        snap.apply_world_patch(WorldStatePatch(current_region=target))
        snap.current_region = "entrance"  # simulate a re-approach signal
        snap.apply_world_patch(WorldStatePatch(current_region=target))
        await obs.drain()
    finally:
        obs.unregister()
        _spans_module.tracer = original_tracer_fn  # type: ignore[method-assign]

    # EXACTLY ONE look-ahead expansion committed (id 2), never two.
    nodes = store.load_map(entrance_id="entrance").nodes.values()
    lookahead_expansions = {n.expansion_id for n in nodes if n.expansion_id >= 2}
    assert lookahead_expansions == {2}, (
        f"expected exactly one look-ahead expansion (id 2); got "
        f"{sorted(lookahead_expansions)} — the in-flight dedupe did not "
        f"hold (two rapid signals double-materialised)"
    )

    # The dedupe is GM-panel-visible: a frontier.lookahead span with
    # deduped=true (the lie-detector proof).
    finished = exporter.get_finished_spans()
    la_spans = [s for s in finished if s.name == SPAN_FRONTIER_LOOKAHEAD]
    deduped = [s for s in la_spans if (s.attributes or {}).get("deduped") is True]
    assert deduped, (
        "no frontier.lookahead span with deduped=true — the idempotency "
        "dedupe is not provable on the GM panel (the lie-detector misses "
        "the no-op second signal)"
    )


# ---------------------------------------------------------------------------
# Bullet 2: lookahead_breadth — 1 vs N along the heading; default 1
# ---------------------------------------------------------------------------


async def test_lookahead_breadth_one_materializes_only_heading_edge() -> None:
    """``lookahead_breadth=1`` → only the single approaching (heading)
    edge is materialised. A region-transition into a region with exactly
    one rooted frontier edge commits exactly ONE new expansion."""
    from sidequest.dungeon.persistence import DungeonStore
    from sidequest.game.session import WorldStatePatch

    conn = _mem_conn()
    store = DungeonStore(conn)
    store.ensure_schema()
    palette = await _seed_expansion_one(store)

    frontier = store.load_frontier()
    # Each exp001.r* region roots exactly one frontier edge (Task 6
    # _new_frontier_edges: one edge per new node). Pick one.
    target = frontier[0].from_region_id
    rooted = [fe for fe in frontier if fe.from_region_id == target]
    assert len(rooted) == 1, "test precondition: one rooted edge per region"

    obs = _register(store, palette, lookahead_breadth=1)
    try:
        snap = _fresh_snapshot(target)
        snap.apply_world_patch(WorldStatePatch(current_region=target))
        await obs.drain()
    finally:
        obs.unregister()

    nodes = store.load_map(entrance_id="entrance").nodes.values()
    lookahead = {n.expansion_id for n in nodes if n.expansion_id >= 2}
    assert lookahead == {2}, (
        f"lookahead_breadth=1 must materialise exactly the single heading "
        f"edge → one new expansion (id 2); got {sorted(lookahead)}"
    )


async def test_default_lookahead_breadth_is_one() -> None:
    """``register_lookahead_worker`` default ``lookahead_breadth`` is 1
    (spec §12 knob default). The handle records it; the selection along
    the heading uses it."""
    from sidequest.dungeon.persistence import DungeonStore

    conn = _mem_conn()
    store = DungeonStore(conn)
    store.ensure_schema()
    palette = await _seed_expansion_one(store)

    from sidequest.dungeon.lookahead_worker import register_lookahead_worker
    from tests.dungeon.test_materializer import (
        _attach_pack,
        _real_cookbook_bundle,
        _reflecting_sdk_client,
    )

    handle = register_lookahead_worker(
        persistence=store,
        bundle=_real_cookbook_bundle(),
        palette=palette,
        pack_tropes=_attach_pack("cave_in"),
        claude_client=_reflecting_sdk_client(),
        campaign_seed=7,
    )
    try:
        assert handle.lookahead_breadth == 1, (
            "register_lookahead_worker default lookahead_breadth must be 1 (spec §12 knob default)"
        )
    finally:
        handle.unregister()


def _yielding_concurrency_probe_client(probe: dict[str, int]) -> Any:
    """A ToolingLlmClient-shaped fake whose ``complete_with_tools`` (a)
    reflects the curation verdict (the Task-4 SDK precedent — never a real
    network call) and (b) yields control with ``await asyncio.sleep(0)``
    so that IF two edges' ``materialize`` ran concurrently their curate
    awaits would interleave. It tracks live concurrency: ``probe['max']``
    is the peak number of simultaneously-suspended materialize runs.

    With the breadth>1 serial fix, ``probe['max']`` MUST stay 1 (one task
    materialises all edges serially — the next edge's ``load_map``/build
    only runs after the prior edge's ``materialize`` incl. commit fully
    returns). If the worker reverted to per-edge parallel tasks, the
    sleep(0) yields would let curates overlap → ``probe['max'] >= 2`` AND
    the expansion_id reads would collide on the same ``max+1`` → a
    PersistError re-commit. This asserts REAL serialization, not an
    instant fake resolving before the next task starts."""
    import asyncio as _asyncio
    import json as _json

    from sidequest.agents.tooling_protocol import ToolingResult

    class _ProbeSdk:
        async def complete_with_tools(
            self,
            system_blocks: Any,
            messages: Any,
            tools: Any,
            tool_dispatch: Any = None,
            *,
            model: str,
            max_iterations: int = 8,
            max_tokens: int = 4096,
            on_text_delta: Any = None,
        ) -> ToolingResult:
            probe["live"] = probe.get("live", 0) + 1
            probe["max"] = max(probe.get("max", 0), probe["live"])
            try:
                # Real cooperative yield: if a sibling edge's materialize
                # were running concurrently it would advance here and
                # bump 'live'.
                await _asyncio.sleep(0)
                await _asyncio.sleep(0)
                prompt = messages[0].content
                _, _, input_blob = prompt.partition("INPUT:\n")
                payload = _json.loads(input_blob)
                verdict = {
                    region_id: {
                        "race": region["race"],
                        "cr_band": region["cr_band"],
                        "wandering_table": [
                            {**row, "telegraph": (row.get("telegraph") or "It is here.")}
                            for row in region["wandering_table"]
                        ],
                        "big_bad": region["big_bad"],
                    }
                    for region_id, region in payload.items()
                }
                return ToolingResult(
                    text=_json.dumps(verdict),
                    stop_reason="end_turn",
                    input_tokens=1,
                    output_tokens=7,
                    cached_input_read_tokens=0,
                    cached_input_write_tokens=0,
                    model=model,
                )
            finally:
                probe["live"] -= 1

    return _ProbeSdk()


async def test_lookahead_breadth_greater_than_one_materializes_near_set_serially() -> None:
    """Raising ``lookahead_breadth`` materialises the near-frontier SET
    along the heading: a region rooting multiple unexpanded frontier
    edges with breadth=N commits N new expansions (the nearest N by
    spawn_depth_score).

    IMPORTANT: this asserts the REAL post-fix behaviour — the N edges are
    materialised SERIALLY inside ONE background task, so the
    ``expansion_id`` (``max+1``) reads cannot race even with a genuinely
    suspending (slow) curate LLM call. The deterministic ``[2,3,4]`` is
    a consequence of real serialization, NOT of an instant fake
    resolving before the next task starts. The concurrency probe proves
    only ONE materialize is ever in-flight at a time (peak == 1); a
    parallel-per-edge regression would overlap the suspending curates
    (peak >= 2) and collide the expansion_ids (PersistError)."""
    from sidequest.dungeon.lookahead_worker import register_lookahead_worker
    from sidequest.dungeon.persistence import DungeonStore, FrontierEdge
    from sidequest.game.session import WorldStatePatch
    from tests.dungeon.test_materializer import (
        _attach_pack,
        _real_cookbook_bundle,
    )

    conn = _mem_conn()
    store = DungeonStore(conn)
    store.ensure_schema()
    palette = await _seed_expansion_one(store)

    frontier = store.load_frontier()
    target = frontier[0].from_region_id

    # Real Task-6-shaped extra unexpanded frontier edges rooted at the
    # SAME region (a region the next expansion can push outward from
    # along several headings) — persisted via the real store, not mocked.
    extra = [
        FrontierEdge(
            frontier_edge_id=f"{target}_extra_{i}",
            from_region_id=target,
            heading="north",
            spawn_depth_score=5.0 + i,
        )
        for i in range(2)
    ]
    for fe in extra:
        store.put_frontier(fe)

    rooted = [fe for fe in store.load_frontier() if fe.from_region_id == target]
    assert len(rooted) >= 3, "test precondition: >=3 rooted edges"

    probe: dict[str, int] = {"live": 0, "max": 0}
    obs = register_lookahead_worker(
        persistence=store,
        bundle=_real_cookbook_bundle(),
        palette=palette,
        pack_tropes=_attach_pack("cave_in"),
        claude_client=_yielding_concurrency_probe_client(probe),
        campaign_seed=7,
        lookahead_breadth=3,
    )
    try:
        snap = _fresh_snapshot(target)
        snap.apply_world_patch(WorldStatePatch(current_region=target))
        await obs.drain()
    finally:
        obs.unregister()

    nodes = store.load_map(entrance_id="entrance").nodes.values()
    lookahead = sorted({n.expansion_id for n in nodes if n.expansion_id >= 2})
    assert lookahead == [2, 3, 4], (
        f"lookahead_breadth=3 must materialise the 3 nearest rooted edges "
        f"→ expansions 2,3,4; got {lookahead} (a parallel-per-edge "
        f"regression would collide expansion_ids → PersistError / fewer "
        f"than 3 distinct expansions)"
    )
    # The keystone of IMPORTANT #2/#3: only ONE materialize was ever
    # in-flight (real serialization, race-free regardless of curate
    # suspension). A parallel regression would show peak >= 2.
    assert probe["max"] == 1, (
        f"breadth>1 materialized edges CONCURRENTLY (peak in-flight "
        f"curate = {probe['max']}) — the expansion_id max+1 read races a "
        f"slow curate subprocess (IMPORTANT #2); edges must be serialized "
        f"within one background task"
    )


# ---------------------------------------------------------------------------
# Bullet 3 (THE CENTRAL CONSTRAINT): worker exception is LOUD on a
# terminal span AND does NOT propagate into the sync region transition.
# ---------------------------------------------------------------------------


async def test_worker_failure_loud_on_span_and_does_not_abort_transition() -> None:
    """Force ``materialize()`` to raise via a RETAINED hard curate
    failure — ADR-106 Amendment A (story 50-26) carve-out (ii): a parsed
    curated row missing ``cr`` still raises ``CurationError`` (degrading
    it would corrupt the CR→Edge seam). NOTE: a *generic* curate failure
    (unparseable verdict / ``LlmClientError``) no longer raises under
    Amendment A — it Layer-2-degrades and the expansion commits uncurated,
    so it would NOT exercise this central-constraint test. The carve-out
    is the correct still-aborting failure mode. The worker MUST:

      - emit a LOUD terminal ``frontier.lookahead`` span carrying the
        failure (routed → GM-panel-visible; the dungeon failed to grow);
      - NOT propagate the exception out of the synchronous
        ``apply_world_patch`` region transition (the transition itself
        completes: ``snap.current_region`` is the new region, no
        exception raised to the caller).

    Decisive central-constraint test: must FAIL if the observer re-raises
    synchronously (the region crossing would abort for a mere prefetch
    failure — core-gameplay fragility)."""
    import sidequest.telemetry.spans as _spans_module
    from sidequest.dungeon.lookahead_worker import register_lookahead_worker
    from sidequest.dungeon.persistence import DungeonStore
    from sidequest.game.session import WorldStatePatch
    from sidequest.telemetry.spans import SPAN_ROUTES
    from sidequest.telemetry.spans.dungeon_materialize import (
        SPAN_FRONTIER_LOOKAHEAD,
    )
    from tests.dungeon.test_materializer import (
        _attach_pack,
        _missing_cr_sdk_client,
        _real_cookbook_bundle,
    )

    conn = _mem_conn()
    store = DungeonStore(conn)
    store.ensure_schema()
    palette = await _seed_expansion_one(store)

    frontier = store.load_frontier()
    target = frontier[0].from_region_id

    # A fake SDK client returning a parseable verdict whose kept rows
    # dropped `cr` → ADR-106 Amendment A retained carve-out (ii):
    # _stage_curate STILL raises CurationError (NOT degraded — degrading
    # would corrupt CR→Edge) → materialize raises → the background worker
    # fails. The ONLY mocked seam (Task-4 SDK precedent).
    failing_client = _missing_cr_sdk_client()

    exporter, _provider, real_tracer = _otel_in_memory()
    original_tracer_fn = _spans_module.tracer
    _spans_module.tracer = lambda: real_tracer  # type: ignore[method-assign]

    handle = register_lookahead_worker(
        persistence=store,
        bundle=_real_cookbook_bundle(),
        palette=palette,
        pack_tropes=_attach_pack("cave_in"),
        claude_client=failing_client,
        campaign_seed=7,
        lookahead_breadth=1,
    )
    try:
        snap = _fresh_snapshot(target)
        # THE CENTRAL CONSTRAINT: this synchronous apply must NOT raise
        # even though the scheduled background worker will fail.
        snap.apply_world_patch(WorldStatePatch(current_region=target))
        # The region transition itself completed (not aborted by the
        # prefetch failure).
        assert snap.current_region == target, (
            "the region transition did not complete — a background "
            "look-ahead failure aborted the party's crossing (the central "
            "constraint is violated: the observer must never re-raise "
            "synchronously)"
        )
        await handle.drain()
    finally:
        handle.unregister()
        _spans_module.tracer = original_tracer_fn  # type: ignore[method-assign]

    # No expansion 2 committed (the worker genuinely failed).
    nodes = store.load_map(entrance_id="entrance").nodes.values()
    assert all(n.expansion_id < 2 for n in nodes), (
        "an expansion was committed despite the forced curation failure"
    )

    # The failure is LOUD on a terminal, ROUTED frontier.lookahead span
    # (GM-panel-visible — the dungeon failed to grow, never silently
    # swallowed).
    finished = exporter.get_finished_spans()
    la_spans = [s for s in finished if s.name == SPAN_FRONTIER_LOOKAHEAD]
    failed = [s for s in la_spans if (s.attributes or {}).get("error") is not None]
    assert failed, (
        "no frontier.lookahead span carrying an `error` — the background "
        "worker failure was silently swallowed (the GM panel cannot see "
        "the dungeon failed to grow)"
    )
    route = SPAN_ROUTES[SPAN_FRONTIER_LOOKAHEAD]
    routed = route.extract(failed[0])
    assert routed.get("error") is not None and routed.get("reason"), (
        "the failure marker is set on the span but NOT routed through "
        "SPAN_ROUTES (the Task-2 lesson: set-but-not-routed is the defect)"
    )


async def test_no_frontier_along_heading_is_observable_not_silent() -> None:
    """A region transition into a region with NO rooted unexpanded
    frontier edge is the genuine no-op case (not every transition
    approaches the frontier). It must be OBSERVABLE — a
    ``frontier.lookahead`` span with ``no_frontier_along_heading=true`` —
    so the GM panel tells "nothing to do" from "look-ahead broken" (No
    Silent Fallbacks)."""
    import sidequest.telemetry.spans as _spans_module
    from sidequest.dungeon.persistence import DungeonStore
    from sidequest.game.session import WorldStatePatch
    from sidequest.telemetry.spans.dungeon_materialize import (
        SPAN_FRONTIER_LOOKAHEAD,
    )

    conn = _mem_conn()
    store = DungeonStore(conn)
    store.ensure_schema()
    palette = await _seed_expansion_one(store)

    exporter, _provider, real_tracer = _otel_in_memory()
    original_tracer_fn = _spans_module.tracer
    _spans_module.tracer = lambda: real_tracer  # type: ignore[method-assign]

    obs = _register(store, palette)
    try:
        snap = _fresh_snapshot("entrance")
        # "entrance" roots NO unexpanded frontier edge (Task 6 derives
        # edges only off the NEW expansion's nodes, never the entrance).
        snap.apply_world_patch(WorldStatePatch(current_region="entrance_other"))
        await obs.drain()
    finally:
        obs.unregister()
        _spans_module.tracer = original_tracer_fn  # type: ignore[method-assign]

    finished = exporter.get_finished_spans()
    la_spans = [s for s in finished if s.name == SPAN_FRONTIER_LOOKAHEAD]
    no_frontier = [
        s for s in la_spans if (s.attributes or {}).get("no_frontier_along_heading") is True
    ]
    assert no_frontier, (
        "the no-frontier-along-heading no-op was NOT observable — it must "
        "emit a frontier.lookahead span (No Silent Fallbacks: the GM panel "
        "must tell 'nothing to do' from 'look-ahead broken')"
    )
    # And nothing was materialised (genuine no-op).
    nodes = store.load_map(entrance_id="entrance").nodes.values()
    assert all(n.expansion_id < 2 for n in nodes)


# ---------------------------------------------------------------------------
# CRITICAL #1 (the central-constraint KEYSTONE): a sync-observer-body
# failure (load_frontier raising DatabaseError on a bad/corrupt save)
# MUST NOT abort the party's region crossing. Loud = terminal routed
# span, NEVER exception-into-the-sync-path.
# ---------------------------------------------------------------------------


class _ExplodingFrontierStore:
    """A real-shaped DungeonStore wrapper whose ``load_frontier()`` raises
    the EXACT real exception ``persistence.py:337`` raises on a
    ``sqlite3.Error`` (``DatabaseError``). Everything else delegates to a
    real ``DungeonStore`` on a real connection — the ONLY divergence is
    the realistic load-failure injection (NOT a mock of the dungeon
    layer; it is the genuine corrupt/locked-save error path)."""

    def __init__(self, real: Any) -> None:
        self._real = real

    def load_frontier(self) -> Any:
        from sidequest.dungeon.persistence import DatabaseError

        raise DatabaseError("load_frontier failed: database disk image is malformed")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


async def test_sync_observer_body_failure_does_not_abort_region_crossing() -> None:
    """CRITICAL #1 — the central-constraint keystone, empirically decisive.

    ``persistence.load_frontier()`` runs SYNCHRONOUSLY inside the
    observer, which runs inside ``notify_region_transition`` (frontier_hook
    explicitly does NOT swallow observer exceptions). On a bad/corrupt
    save it raises ``DatabaseError``. A background-prefetch DB error must
    NEVER abort the party's region crossing.

    Drive the REAL production region-transition
    (``snap.apply_world_patch(WorldStatePatch(current_region=...))``) with
    a persistence whose ``load_frontier()`` raises ``DatabaseError``
    exactly as ``persistence.py:337`` does, and assert:
      (a) NO exception propagates to the ``apply_world_patch`` caller,
      (b) ``snap.current_region`` == the new region (the crossing
          completed — it was never aborted),
      (c) the failure IS loud on the routed terminal frontier.lookahead
          span (GM-panel-visible — the dungeon failed to prefetch).

    Decisive: must FAIL without the post-get_running_loop guard (the
    DatabaseError would propagate out of apply_world_patch and abort the
    crossing) and PASS with it (loud-on-span, no re-raise)."""
    import sidequest.telemetry.spans as _spans_module
    from sidequest.dungeon.lookahead_worker import register_lookahead_worker
    from sidequest.dungeon.persistence import DungeonStore
    from sidequest.game.session import GameSnapshot, WorldStatePatch
    from sidequest.telemetry.spans import SPAN_ROUTES
    from sidequest.telemetry.spans.dungeon_materialize import (
        SPAN_FRONTIER_LOOKAHEAD,
    )
    from tests.dungeon.test_materializer import (
        _attach_pack,
        _real_cookbook_bundle,
        _reflecting_sdk_client,
    )

    conn = _mem_conn()
    real_store = DungeonStore(conn)
    real_store.ensure_schema()
    palette = await _seed_expansion_one(real_store)

    # A real exp001.r* region that DOES root a frontier edge — so the
    # only reason the worker can't proceed is the injected load_frontier
    # DatabaseError (not a no-frontier no-op).
    target = real_store.load_frontier()[0].from_region_id

    exploding = _ExplodingFrontierStore(real_store)

    exporter, _provider, real_tracer = _otel_in_memory()
    original_tracer_fn = _spans_module.tracer
    _spans_module.tracer = lambda: real_tracer  # type: ignore[method-assign]

    handle = register_lookahead_worker(
        persistence=exploding,
        bundle=_real_cookbook_bundle(),
        palette=palette,
        pack_tropes=_attach_pack("cave_in"),
        claude_client=_reflecting_sdk_client(),
        campaign_seed=7,
        lookahead_breadth=1,
    )
    try:
        snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="beneath_sunden")
        snap.current_region = "entrance"
        # (a) THE KEYSTONE: this synchronous production apply must NOT
        # raise even though load_frontier() raises DatabaseError inside
        # the observer body. No pytest.raises — a propagated exception
        # here IS the bug.
        snap.apply_world_patch(WorldStatePatch(current_region=target))
        # (b) The region crossing completed — never aborted by the
        # background-prefetch DB error.
        assert snap.current_region == target, (
            "the region transition was ABORTED by a load_frontier "
            "DatabaseError — the central constraint is violated (a "
            "background-prefetch failure must never abort the party's "
            "crossing; the sync observer body is unguarded)"
        )
        await handle.drain()
    finally:
        handle.unregister()
        _spans_module.tracer = original_tracer_fn  # type: ignore[method-assign]

    # (c) The failure is LOUD on a terminal, ROUTED frontier.lookahead
    # span (GM-panel-visible — the dungeon failed to prefetch, never
    # silently swallowed).
    finished = exporter.get_finished_spans()
    la_spans = [s for s in finished if s.name == SPAN_FRONTIER_LOOKAHEAD]
    failed = [s for s in la_spans if (s.attributes or {}).get("error") == "DatabaseError"]
    assert failed, (
        "no frontier.lookahead span carrying error=DatabaseError — the "
        "sync-observer-body load_frontier failure was silently swallowed "
        "(the GM panel cannot see the dungeon failed to prefetch)"
    )
    route = SPAN_ROUTES[SPAN_FRONTIER_LOOKAHEAD]
    routed = route.extract(failed[0])
    assert routed.get("error") == "DatabaseError" and routed.get("reason"), (
        "the failure marker is set on the span but NOT routed through "
        "SPAN_ROUTES (the Task-2 lesson: set-but-not-routed is the defect)"
    )
    # Nothing was materialised (the prefetch genuinely failed).
    nodes = real_store.load_map(entrance_id="entrance").nodes.values()
    assert all(n.expansion_id < 2 for n in nodes)
