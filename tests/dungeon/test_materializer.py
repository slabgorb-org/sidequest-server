"""Beneath Sünden Plan 7 Task 1 — MaterializationRequest + pipeline skeleton tests.

Two test bullets from the plan:
  1. Request validation: expansion_id < 1 raises ValueError, frontier_edge not in
     the supplied frontier raises ValueError, burst_magnitude < 1 raises ValueError
     — loud, no defaults, no silent fallbacks.
  2. materialize() opens a parent dungeon.materialize span; the five child stage
     spans (design, fill, curate, attach, commit) nest under it in order.
     Each stage raises NotImplementedError (skeleton contract).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from sidequest.dungeon.persistence import FrontierEdge  # noqa: E402
from sidequest.dungeon.region_graph import RegionGraph
from sidequest.dungeon.themes import ThemePalette

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mem_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _make_frontier_edge(frontier_edge_id: str = "fe1") -> FrontierEdge:
    return FrontierEdge(
        frontier_edge_id=frontier_edge_id,
        from_region_id="exp001.r0",
        heading="north",
        spawn_depth_score=15.0,
    )


def _otel_in_memory() -> tuple[Any, Any, Any]:
    """Return (exporter, provider, real_tracer) for in-memory OTEL tests."""
    from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: PLC0415
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: PLC0415
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    real_tracer = provider.get_tracer("test")
    return exporter, provider, real_tracer


# ---------------------------------------------------------------------------
# Test 1: Request validation
# ---------------------------------------------------------------------------


class TestMaterializationRequestValidation:
    """Task 1 test bullet 1: the request is a frozen, hashable value object
    that rejects invalid inputs loudly at construction — no silent defaults."""

    def _valid_kwargs(self, frontier_edge: FrontierEdge | None = None) -> dict:
        fe = frontier_edge or _make_frontier_edge("fe1")
        return {
            "campaign_seed": 7,
            "expansion_id": 1,
            "frontier_edge": fe,
            "frontier": [fe],
            "attach_region_ids": ["exp001.r0"],
            "heading": "north",
            "burst_magnitude": 3,
            "lookahead_breadth": 2,
        }

    def test_valid_request_constructs_without_error(self) -> None:
        from sidequest.dungeon.materializer import MaterializationRequest

        req = MaterializationRequest.build(**self._valid_kwargs())
        assert req.expansion_id == 1
        assert req.campaign_seed == 7
        assert req.burst_magnitude == 3
        assert req.lookahead_breadth == 2

    def test_expansion_id_zero_raises_value_error(self) -> None:
        """Expansion 0 is the seed/entrance; it is reserved. expansion_id < 1 is invalid."""
        from sidequest.dungeon.materializer import MaterializationRequest

        kwargs = self._valid_kwargs()
        kwargs["expansion_id"] = 0
        with pytest.raises(ValueError, match="expansion_id"):
            MaterializationRequest.build(**kwargs)

    def test_expansion_id_negative_raises_value_error(self) -> None:
        from sidequest.dungeon.materializer import MaterializationRequest

        kwargs = self._valid_kwargs()
        kwargs["expansion_id"] = -5
        with pytest.raises(ValueError, match="expansion_id"):
            MaterializationRequest.build(**kwargs)

    def test_frontier_edge_not_in_frontier_raises_value_error(self) -> None:
        """frontier_edge must be a member of the supplied frontier list."""
        from sidequest.dungeon.materializer import MaterializationRequest

        fe_in_frontier = _make_frontier_edge("fe1")
        fe_not_in_frontier = _make_frontier_edge("fe_unknown")
        kwargs = self._valid_kwargs(frontier_edge=fe_in_frontier)
        kwargs["frontier_edge"] = fe_not_in_frontier
        kwargs["frontier"] = [fe_in_frontier]  # fe_not_in_frontier not in this list

        with pytest.raises(ValueError, match="frontier"):
            MaterializationRequest.build(**kwargs)

    def test_burst_magnitude_zero_raises_value_error(self) -> None:
        from sidequest.dungeon.materializer import MaterializationRequest

        kwargs = self._valid_kwargs()
        kwargs["burst_magnitude"] = 0
        with pytest.raises(ValueError, match="burst_magnitude"):
            MaterializationRequest.build(**kwargs)

    def test_burst_magnitude_negative_raises_value_error(self) -> None:
        from sidequest.dungeon.materializer import MaterializationRequest

        kwargs = self._valid_kwargs()
        kwargs["burst_magnitude"] = -1
        with pytest.raises(ValueError, match="burst_magnitude"):
            MaterializationRequest.build(**kwargs)

    def test_request_is_frozen(self) -> None:
        """frozen=True dataclass — attribute assignment must raise
        FrozenInstanceError (a subclass of AttributeError)."""
        from sidequest.dungeon.materializer import MaterializationRequest

        req = MaterializationRequest.build(**self._valid_kwargs())
        with pytest.raises(AttributeError):
            req.expansion_id = 99  # type: ignore[misc]

    def test_request_is_hashable(self) -> None:
        """The object must be usable as a dict key or set member."""
        from sidequest.dungeon.materializer import MaterializationRequest

        req = MaterializationRequest.build(**self._valid_kwargs())
        d: dict[MaterializationRequest, str] = {req: "ok"}
        assert d[req] == "ok"
        s: set[MaterializationRequest] = {req}
        assert req in s

    def test_attach_region_ids_stored_as_tuple(self) -> None:
        """list input is frozen to tuple internally so the object stays hashable."""
        from sidequest.dungeon.materializer import MaterializationRequest

        req = MaterializationRequest.build(**self._valid_kwargs())
        # Must be a tuple (not list) — lists are unhashable and would break
        # the hash contract.
        assert isinstance(req.attach_region_ids, tuple)

    def test_two_equal_requests_have_same_hash(self) -> None:
        from sidequest.dungeon.materializer import MaterializationRequest

        fe = _make_frontier_edge("fe1")
        kwargs = {
            "campaign_seed": 7,
            "expansion_id": 1,
            "frontier_edge": fe,
            "frontier": [fe],
            "attach_region_ids": ["exp001.r0"],
            "heading": "north",
            "burst_magnitude": 3,
            "lookahead_breadth": 2,
        }
        r1 = MaterializationRequest.build(**kwargs)
        r2 = MaterializationRequest.build(**kwargs)
        assert r1 == r2
        assert hash(r1) == hash(r2)


# ---------------------------------------------------------------------------
# Test 2: span nesting — parent + five ordered children
# ---------------------------------------------------------------------------


class TestMaterializePipelineSpans:
    """Task 1 test bullet 2: materialize() opens a dungeon.materialize parent
    span; the five stage child spans (design, fill, curate, attach, commit) nest
    under it in order.

    Note: After Task 2, design is implemented so the first NotImplementedError
    comes from _stage_fill (not design). The structural contract — parent span
    emitted, stages run in order, nesting correct — is unchanged.
    """

    def _build_request(self) -> Any:
        from sidequest.dungeon.materializer import MaterializationRequest
        from sidequest.dungeon.persistence import FrontierEdge

        fe = FrontierEdge(
            frontier_edge_id="fe1",
            from_region_id="entrance",
            heading="north",
            spawn_depth_score=15.0,
        )
        return MaterializationRequest.build(
            campaign_seed=7,
            expansion_id=1,
            frontier_edge=fe,
            frontier=[fe],
            attach_region_ids=["entrance"],
            heading="north",
            burst_magnitude=3,
            lookahead_breadth=2,
        )

    async def test_materialize_runs_full_pipeline_to_completion(
        self, monkeypatch: Any, migrated_db: str
    ) -> None:
        """Task 6 landed the final stage: materialize() now runs all five
        stages to completion with NO NotImplementedError (the skeleton
        boundary that moved forward through Tasks 2–5 has now reached the
        end). The structural Task-1 contract — the pipeline runs in order
        through commit — is preserved; the assertion shape moves to the
        new production reality (completes; the expansion is committed
        live). A real PgDungeonRepository is required because Task
        6's commit introspects + writes the real save (the production
        shape)."""
        import sidequest.telemetry.spans as _spans_module
        from sidequest.dungeon.materializer import materialize
        from tests.dungeon.conftest import build_pg_dungeon_repo

        _pool, repo, _sid = build_pg_dungeon_repo(monkeypatch, migrated_db)

        bundle = _real_cookbook_bundle()
        theme_id = "pipeline_crypt"
        palette = _commit_palette(theme_id)
        graph = _seed_graph_themed(theme_id)

        _exporter, _provider, real_tracer = _otel_in_memory()
        original_tracer_fn = _spans_module.tracer
        _spans_module.tracer = lambda: real_tracer  # type: ignore[method-assign]
        try:
            req = self._build_request()
            # No pytest.raises: Task 6 is implemented, the pipeline
            # completes. The expansion is committed live.
            await materialize(
                req,
                graph=graph,
                bundle=bundle,
                palette=palette,
                dungeon_repository=repo,
                snapshot=_fresh_snapshot(),
                pack_tropes=_attach_pack("cave_in"),
                claude_client=_reflecting_sdk_client(),
            )
        finally:
            _spans_module.tracer = original_tracer_fn  # type: ignore[method-assign]

        # Commit is immediately live on success (spec §7).
        assert "entrance" in repo.load_map(entrance_id="entrance").nodes

    async def test_parent_span_opens_and_pipeline_completes(
        self, monkeypatch: Any, migrated_db: str
    ) -> None:
        """dungeon.materialize parent span must be emitted, and (Task 6)
        the full pipeline now completes — the parent span wraps a
        successful five-stage run, not a NotImplementedError abort."""
        import sidequest.telemetry.spans as _spans_module
        from sidequest.dungeon.materializer import materialize
        from sidequest.telemetry.spans.dungeon_materialize import SPAN_DUNGEON_MATERIALIZE
        from tests.dungeon.conftest import build_pg_dungeon_repo

        _pool, repo, _sid = build_pg_dungeon_repo(monkeypatch, migrated_db)

        bundle = _real_cookbook_bundle()
        theme_id = "pipeline_crypt2"
        palette = _commit_palette(theme_id)
        graph = _seed_graph_themed(theme_id)

        exporter, _provider, real_tracer = _otel_in_memory()
        original_tracer_fn = _spans_module.tracer
        _spans_module.tracer = lambda: real_tracer  # type: ignore[method-assign]
        try:
            req = self._build_request()
            await materialize(
                req,
                graph=graph,
                bundle=bundle,
                palette=palette,
                dungeon_repository=repo,
                snapshot=_fresh_snapshot(),
                pack_tropes=_attach_pack("cave_in"),
                claude_client=_reflecting_sdk_client(),
            )
        finally:
            _spans_module.tracer = original_tracer_fn  # type: ignore[method-assign]

        finished = exporter.get_finished_spans()
        parent_spans = [s for s in finished if s.name == SPAN_DUNGEON_MATERIALIZE]
        assert parent_spans, (
            f"dungeon.materialize parent span not emitted — "
            f"got span names: {[s.name for s in finished]}"
        )

    async def test_five_stage_spans_emitted_in_order_nested_under_parent(
        self,
    ) -> None:
        """Run materialize with a patched stage executor so all five stages
        run (no early-exit on NotImplementedError). Assert:
          - all five child spans are emitted
          - each child's parent is the dungeon.materialize parent span
          - spans appear in the order: design, fill, curate, attach, commit
        """
        import sidequest.telemetry.spans as _spans_module
        from sidequest.dungeon.materializer import (
            materialize,
        )
        from sidequest.dungeon.persistence import DungeonStore
        from sidequest.telemetry.spans.dungeon_materialize import (
            SPAN_DUNGEON_MATERIALIZE,
            SPAN_DUNGEON_MATERIALIZE_ATTACH,
            SPAN_DUNGEON_MATERIALIZE_COMMIT,
            SPAN_DUNGEON_MATERIALIZE_CURATE,
            SPAN_DUNGEON_MATERIALIZE_DESIGN,
            SPAN_DUNGEON_MATERIALIZE_FILL,
        )

        conn = _mem_conn()
        store = DungeonStore(conn)

        exporter, _provider, real_tracer = _otel_in_memory()
        original_tracer_fn = _spans_module.tracer
        _spans_module.tracer = lambda: real_tracer  # type: ignore[method-assign]

        # Monkey-patch all five stages to no-ops so all five spans fire
        import sidequest.dungeon.materializer as _mat_module  # noqa: PLC0415

        original_design = _mat_module._stage_design
        original_fill = _mat_module._stage_fill
        original_curate = _mat_module._stage_curate
        original_attach = _mat_module._stage_attach
        original_commit = _mat_module._stage_commit

        def _noop(*args: object, **kwargs: object) -> None:
            pass

        async def _async_noop(*args: object, **kwargs: object) -> None:
            # _stage_curate is `async def` (it awaits ClaudeClient.send);
            # the coordinator `await`s it. The no-op stub must therefore be
            # a coroutine function so `await _stage_curate(...)` is valid.
            # design/fill/attach/commit stay synchronous no-ops.
            return None

        def _design_noop(*args: object, **kwargs: object) -> tuple[object, object]:
            # The real _stage_design ALWAYS returns (Expansion,
            # GenerationReport) (Task 2 hard contract); the coordinator
            # unconditionally unpacks it. This stub honors that contract so
            # the test exercises the real unpack path while still no-op'ing
            # all stage logic — it only asserts span nesting/order.
            return (object(), object())

        _mat_module._stage_design = _design_noop  # type: ignore[assignment]
        _mat_module._stage_fill = _noop  # type: ignore[assignment]
        _mat_module._stage_curate = _async_noop  # type: ignore[assignment]
        _mat_module._stage_attach = _noop  # type: ignore[assignment]
        _mat_module._stage_commit = _noop  # type: ignore[assignment]
        try:
            req = self._build_request()
            # snapshot/pack_tropes/claude_client are required materialize()
            # params (Task 5 / Task 4 SDK); the curate+attach stages are
            # monkeypatched to no-ops here so the values are never read —
            # passing real-shaped placeholders keeps the signature
            # satisfied while the test asserts ONLY span nesting/order
            # (unchanged Task-1 contract).
            await materialize(
                req,
                graph=None,
                bundle=None,
                palette=None,
                dungeon_repository=store,
                snapshot=_fresh_snapshot(),
                pack_tropes=_attach_pack("cave_in"),
                claude_client=_reflecting_sdk_client(),
            )
        finally:
            _mat_module._stage_design = original_design  # type: ignore[assignment]
            _mat_module._stage_fill = original_fill  # type: ignore[assignment]
            _mat_module._stage_curate = original_curate  # type: ignore[assignment]
            _mat_module._stage_attach = original_attach  # type: ignore[assignment]
            _mat_module._stage_commit = original_commit  # type: ignore[assignment]
            _spans_module.tracer = original_tracer_fn  # type: ignore[method-assign]

        finished = exporter.get_finished_spans()
        span_names = [s.name for s in finished]

        # Parent span must be present
        parent_spans = [s for s in finished if s.name == SPAN_DUNGEON_MATERIALIZE]
        assert parent_spans, f"dungeon.materialize parent span missing; got: {span_names}"
        parent_span = parent_spans[0]

        # All five child spans must be present
        expected_children = [
            SPAN_DUNGEON_MATERIALIZE_DESIGN,
            SPAN_DUNGEON_MATERIALIZE_FILL,
            SPAN_DUNGEON_MATERIALIZE_CURATE,
            SPAN_DUNGEON_MATERIALIZE_ATTACH,
            SPAN_DUNGEON_MATERIALIZE_COMMIT,
        ]
        for child_name in expected_children:
            child_spans = [s for s in finished if s.name == child_name]
            assert child_spans, (
                f"Stage span {child_name!r} not emitted. All span names: {span_names}"
            )

        # Each child's parent must be the dungeon.materialize parent span
        parent_context_span_id = parent_span.context.span_id
        for child_name in expected_children:
            child_span = next(s for s in finished if s.name == child_name)
            assert child_span.parent is not None, (
                f"{child_name!r} has no parent — it must be nested under dungeon.materialize"
            )
            assert child_span.parent.span_id == parent_context_span_id, (
                f"{child_name!r}.parent.span_id != dungeon.materialize span_id — "
                f"the stage span is not nested under the parent"
            )

        # Children must appear in order: design, fill, curate, attach, commit
        # OTEL SimpleSpanProcessor exports spans as they END (LIFO inside
        # nested with-blocks). For sequential (non-nested) child spans, the
        # first child to start is the first to end.
        # We assert ordering by start time (monotonic).
        child_spans_in_order = [
            next(s for s in finished if s.name == cn) for cn in expected_children
        ]
        start_times = [s.start_time for s in child_spans_in_order]
        assert start_times == sorted(start_times), (
            "Stage spans must start in order: design, fill, curate, attach, commit. "
            f"Start times: {list(zip(expected_children, start_times, strict=True))}"
        )


# ---------------------------------------------------------------------------
# Test 3: wiring — public exports reachable from sidequest.dungeon
# ---------------------------------------------------------------------------


def test_materializer_exports_reachable_from_dungeon_package() -> None:
    """MaterializationRequest and materialize must be importable from
    sidequest.dungeon (the package __init__.py must export them)."""
    from sidequest.dungeon import MaterializationRequest, materialize  # type: ignore[attr-defined]

    assert callable(materialize)
    assert MaterializationRequest is not None


# ---------------------------------------------------------------------------
# Test 4: routing completeness gate (no new unrouted constants)
# ---------------------------------------------------------------------------


def test_dungeon_materialize_spans_registered_and_routed() -> None:
    """Every dungeon.materialize.*, dungeon.curate.* (ADR-106 Amendment A),
    and frontier.expand constant must be in SPAN_ROUTES — the
    routing-completeness gate must not fail on our additions."""
    from sidequest.telemetry.spans import SPAN_ROUTES
    from sidequest.telemetry.spans.dungeon_materialize import (
        SPAN_DUNGEON_CURATE_DEGRADED,
        SPAN_DUNGEON_CURATE_PARSE_FAILED,
        SPAN_DUNGEON_MATERIALIZE,
        SPAN_DUNGEON_MATERIALIZE_ATTACH,
        SPAN_DUNGEON_MATERIALIZE_COMMIT,
        SPAN_DUNGEON_MATERIALIZE_CURATE,
        SPAN_DUNGEON_MATERIALIZE_DESIGN,
        SPAN_DUNGEON_MATERIALIZE_FILL,
        SPAN_FRONTIER_EXPAND,
    )

    for name in (
        SPAN_DUNGEON_MATERIALIZE,
        SPAN_DUNGEON_MATERIALIZE_DESIGN,
        SPAN_DUNGEON_MATERIALIZE_FILL,
        SPAN_DUNGEON_MATERIALIZE_CURATE,
        SPAN_DUNGEON_MATERIALIZE_ATTACH,
        SPAN_DUNGEON_MATERIALIZE_COMMIT,
        SPAN_FRONTIER_EXPAND,
        # ADR-106 Amendment A (story 50-26) — clause-12 routed lie-detector
        # spans for the curate-stage robustness contract.
        SPAN_DUNGEON_CURATE_PARSE_FAILED,
        SPAN_DUNGEON_CURATE_DEGRADED,
    ):
        assert name in SPAN_ROUTES, (
            f"{name!r} not in SPAN_ROUTES — routing-completeness gate will fail"
        )


# ---------------------------------------------------------------------------
# Task 2: Stage 1 design — generate_expansion + depth-filtered theme_pool
# ---------------------------------------------------------------------------


def _make_seed_graph(entrance_id: str = "entrance") -> RegionGraph:
    """A seed graph (only the entrance node) suitable for generate_expansion
    in seed-mode (is_seed=True path in the generator)."""
    from sidequest.dungeon.region_graph import RegionGraph, RegionNode

    g = RegionGraph(entrance_id=entrance_id)
    g.add_node(RegionNode(id=entrance_id, expansion_id=0, theme="tomb"))
    return g


def _make_theme_palette_two_themes(
    *,
    deep_theme_id: str = "deep_crypt",
    shallow_theme_id: str = "entry_hall",
    depth_score_cutoff: float = 20.0,
) -> ThemePalette:
    """Build a minimal ThemePalette with two themes:
    - shallow_theme_id: eligible at depth_score < depth_score_cutoff (max=cutoff-1)
    - deep_theme_id:    eligible at all depths (min=0, max=None)
    Used to verify that themes_for_depth correctly filters by band.
    """
    from sidequest.dungeon.themes import (
        Adjacency,
        DepthBand,
        DungeonTheme,
        InteriorSpec,
        NarratorFlavor,
        ThemePalette,
    )

    def _make_theme(tid: str, min_d: float, max_d: float | None) -> DungeonTheme:
        return DungeonTheme(
            id=tid,
            display_name=tid.replace("_", " ").title(),
            generator_class="organic",
            interior=InteriorSpec(algorithm="cellular", braid_ratio=0.0),
            depth_band=DepthBand(min=min_d, max=max_d),
            narrator=NarratorFlavor(register="grave", flavor="dread whispers"),
            adjacency=Adjacency(),
        )

    # shallow theme: max = depth_score_cutoff - 1 (so depth_score >= cutoff is excluded)
    shallow = _make_theme(shallow_theme_id, 0.0, depth_score_cutoff - 1.0)
    # deep theme: unbounded (eligible at ALL depths)
    deep = _make_theme(deep_theme_id, 0.0, None)
    return ThemePalette(themes={shallow_theme_id: shallow, deep_theme_id: deep})


class TestStageDesign:
    """Task 2 tests:
    1. design returns (Expansion, GenerationReport); span attrs = report.as_dict() exactly.
    2. ExpansionGenerationError propagates loudly + span carries failure.
    3. theme_pool is depth-filtered — excluded theme absent from pool.
    """

    # ---- helpers ----

    def _make_request(
        self,
        *,
        expansion_id: int = 1,
        burst_magnitude: int = 3,
        depth_score: float = 15.0,
        attach_region_ids: list[str] | None = None,
    ) -> Any:
        from sidequest.dungeon.materializer import MaterializationRequest
        from sidequest.dungeon.persistence import FrontierEdge

        fe = FrontierEdge(
            frontier_edge_id="fe1",
            from_region_id="entrance",
            heading="north",
            spawn_depth_score=depth_score,
        )
        return MaterializationRequest.build(
            campaign_seed=42,
            expansion_id=expansion_id,
            frontier_edge=fe,
            frontier=[fe],
            attach_region_ids=attach_region_ids or ["entrance"],
            heading="north",
            burst_magnitude=burst_magnitude,
            lookahead_breadth=2,
        )

    def _setup_otel(self):
        import sidequest.telemetry.spans as _spans_module

        exporter, provider, real_tracer = _otel_in_memory()
        original_tracer_fn = _spans_module.tracer
        _spans_module.tracer = lambda: real_tracer  # type: ignore[method-assign]
        return exporter, original_tracer_fn, _spans_module

    def _teardown_otel(self, _spans_module: Any, original_tracer_fn: Any) -> None:
        _spans_module.tracer = original_tracer_fn  # type: ignore[method-assign]

    # ---- Test 1: design returns (Expansion, GenerationReport); span attrs = report.as_dict() ----

    def test_design_stage_returns_expansion_and_report(self) -> None:
        """_stage_design returns (Expansion, GenerationReport); the design span's
        attributes equal report.as_dict() exactly (key-set pinned)."""

        import sidequest.dungeon.materializer as _mat_module
        from sidequest.dungeon.region_graph.invariants import GenerationReport
        from sidequest.telemetry.spans import SPAN_ROUTES
        from sidequest.telemetry.spans.dungeon_materialize import (
            SPAN_DUNGEON_MATERIALIZE_DESIGN,
            dungeon_materialize_design_span,
        )

        palette = _make_theme_palette_two_themes(depth_score_cutoff=20.0)
        graph = _make_seed_graph()
        request = self._make_request(depth_score=15.0)

        exporter, original_tracer_fn, _spans_mod = self._setup_otel()
        try:
            with dungeon_materialize_design_span(expansion_id=request.expansion_id) as span:
                result = _mat_module._stage_design(request, graph=graph, palette=palette, span=span)
        finally:
            self._teardown_otel(_spans_mod, original_tracer_fn)

        expansion, report = result
        assert isinstance(report, GenerationReport), (
            f"second element must be GenerationReport; got {type(report)}"
        )

        # Span attribute key-set must equal report.as_dict() keys exactly
        finished = exporter.get_finished_spans()
        design_spans = [s for s in finished if s.name == SPAN_DUNGEON_MATERIALIZE_DESIGN]
        assert design_spans, "dungeon.materialize.design span not emitted"
        span_attrs = dict(design_spans[0].attributes or {})
        report_dict = report.as_dict()

        assert set(span_attrs.keys()) == set(report_dict.keys()), (
            f"Span attribute key-set mismatch.\n"
            f"  Span keys: {sorted(span_attrs)}\n"
            f"  report.as_dict() keys: {sorted(report_dict)}"
        )
        # Check each value (invariants_passed is stored as JSON string on the span)
        for k, v in report_dict.items():
            if k == "invariants_passed":
                assert span_attrs[k] == json.dumps(v, sort_keys=True), (
                    f"span[{k!r}] = {span_attrs[k]!r}, expected JSON of {v!r}"
                )
            else:
                assert span_attrs[k] == v, f"span[{k!r}] = {span_attrs[k]!r}, expected {v!r}"

        # Success path: the routed extract's failure markers read None
        # (graceful-get idiom — harmless; no error/failing attrs were set).
        route = SPAN_ROUTES[SPAN_DUNGEON_MATERIALIZE_DESIGN]
        fields = route.extract(design_spans[0])  # type: ignore[arg-type]
        assert fields.get("error") is None
        assert fields.get("failing") is None

    # ---- Test 2: ExpansionGenerationError propagates + span carries failure ----

    def test_design_stage_propagates_expansion_generation_error_with_span(self) -> None:
        """When generate_expansion raises ExpansionGenerationError, _stage_design
        must NOT swallow it — it sets a failure attribute on the span and re-raises.

        Also asserts the *routed* SPAN_ROUTES extract surfaces error/failing — the
        raw span attributes are not what the GM panel renders; the typed
        state_transition event built by the route's extract is. The lie-detector
        is only honest if the route propagates the failure markers."""

        import sidequest.dungeon.materializer as _mat_module
        from sidequest.dungeon.region_graph import ExpansionGenerationError
        from sidequest.telemetry.spans import SPAN_ROUTES
        from sidequest.telemetry.spans.dungeon_materialize import (
            SPAN_DUNGEON_MATERIALIZE_DESIGN,
            dungeon_materialize_design_span,
        )

        palette = _make_theme_palette_two_themes(depth_score_cutoff=20.0)
        graph = _make_seed_graph()
        request = self._make_request(depth_score=15.0)

        forced_error = ExpansionGenerationError(
            expansion_id=request.expansion_id,
            attempts=64,
            failing=["two_independent_entries"],
        )

        exporter, original_tracer_fn, _spans_mod = self._setup_otel()
        # Patch at the materializer's imported name (not the origin module).
        original_gen = _mat_module.generate_expansion

        def _always_fail(**kwargs: Any) -> Any:
            raise forced_error

        _mat_module.generate_expansion = _always_fail  # type: ignore[assignment]
        try:
            with (
                pytest.raises(ExpansionGenerationError) as exc_info,
                dungeon_materialize_design_span(expansion_id=request.expansion_id) as span,
            ):
                _mat_module._stage_design(request, graph=graph, palette=palette, span=span)
        finally:
            _mat_module.generate_expansion = original_gen  # type: ignore[assignment]
            self._teardown_otel(_spans_mod, original_tracer_fn)

        # The error must be the exact instance (not a wrapped/swallowed one)
        assert exc_info.value is forced_error, (
            "ExpansionGenerationError must propagate unchanged — not swallowed or re-wrapped"
        )

        # The design span must carry a failure attribute (lie-detector visibility)
        finished = exporter.get_finished_spans()
        design_spans = [s for s in finished if s.name == SPAN_DUNGEON_MATERIALIZE_DESIGN]
        assert design_spans, "dungeon.materialize.design span must be emitted even on failure"
        finished_span = design_spans[0]
        span_attrs = dict(finished_span.attributes or {})
        assert "error" in span_attrs and "failing" in span_attrs, (
            f"Design span must carry both error and failing attributes on failure; "
            f"got attrs: {span_attrs}"
        )

        # Decisive: the ROUTED extract (what the GM panel actually renders) must
        # surface the failure markers, not just the raw span attributes.
        route = SPAN_ROUTES[SPAN_DUNGEON_MATERIALIZE_DESIGN]
        fields = route.extract(finished_span)  # type: ignore[arg-type]
        assert fields.get("error") == str(forced_error), (
            f"Routed extract must surface 'error' on the failure path "
            f"(GM-panel lie-detector); got fields: {fields}"
        )
        assert fields.get("failing") == json.dumps(["two_independent_entries"], sort_keys=True), (
            f"Routed extract must surface 'failing' on the failure path; got fields: {fields}"
        )

    # ---- Test 3: theme_pool is depth-filtered ----

    def test_design_stage_depth_filters_theme_pool(self) -> None:
        """Themes whose depth_band excludes depth_score are absent from the
        theme_pool passed to generate_expansion."""
        import sidequest.dungeon.materializer as _mat_module
        from sidequest.telemetry.spans.dungeon_materialize import dungeon_materialize_design_span

        # depth_score = 25.0; shallow theme max = 19.0 (< 25.0) → excluded
        # deep theme max = None → always eligible
        DEPTH_SCORE = 25.0
        CUTOFF = 20.0
        SHALLOW_ID = "entry_hall"
        DEEP_ID = "deep_crypt"
        palette = _make_theme_palette_two_themes(
            deep_theme_id=DEEP_ID,
            shallow_theme_id=SHALLOW_ID,
            depth_score_cutoff=CUTOFF,
        )
        graph = _make_seed_graph()
        request = self._make_request(depth_score=DEPTH_SCORE)

        captured_theme_pool: list[list[str]] = []
        # Patch at the materializer's imported name so the spy intercepts the call.
        original_gen = _mat_module.generate_expansion

        def _capture_pool(**kwargs: Any) -> Any:
            captured_theme_pool.append(list(kwargs["theme_pool"]))
            return original_gen(**kwargs)

        exporter, original_tracer_fn, _spans_mod = self._setup_otel()
        _mat_module.generate_expansion = _capture_pool  # type: ignore[assignment]
        try:
            with dungeon_materialize_design_span(expansion_id=request.expansion_id) as span:
                _mat_module._stage_design(request, graph=graph, palette=palette, span=span)
        finally:
            _mat_module.generate_expansion = original_gen  # type: ignore[assignment]
            self._teardown_otel(_spans_mod, original_tracer_fn)

        assert captured_theme_pool, "generate_expansion was not called"
        pool = captured_theme_pool[0]
        assert SHALLOW_ID not in pool, (
            f"Shallow theme {SHALLOW_ID!r} (max depth {CUTOFF - 1}) must be "
            f"excluded from theme_pool at depth_score {DEPTH_SCORE}; "
            f"got pool: {pool}"
        )
        assert DEEP_ID in pool, (
            f"Deep theme {DEEP_ID!r} (unbounded) must be in theme_pool; got pool: {pool}"
        )

    # ---- Test 4: empty theme_pool raises loudly ----

    def test_design_stage_raises_if_no_themes_eligible_at_depth(self) -> None:
        """If themes_for_depth returns an empty list, _stage_design must raise
        loudly — not silently pass an empty pool to generate_expansion.
        Must raise ValueError specifically (not NotImplementedError)."""
        import sidequest.dungeon.materializer as _mat_module
        from sidequest.dungeon.themes import (
            Adjacency,
            DepthBand,
            DungeonTheme,
            InteriorSpec,
            NarratorFlavor,
            ThemePalette,
        )
        from sidequest.telemetry.spans.dungeon_materialize import dungeon_materialize_design_span

        # Build a palette where the ONLY theme has max=5.0
        theme = DungeonTheme(
            id="shallow_only",
            display_name="Shallow Only",
            generator_class="organic",
            interior=InteriorSpec(algorithm="cellular"),
            depth_band=DepthBand(min=0.0, max=5.0),
            narrator=NarratorFlavor(register="grave", flavor="whispers"),
            adjacency=Adjacency(),
        )
        palette = ThemePalette(themes={"shallow_only": theme})
        graph = _make_seed_graph()
        # depth_score=50.0 — far beyond the theme's band
        request = self._make_request(depth_score=50.0)

        with (
            pytest.raises(ValueError, match="[Tt]heme|depth|empty"),
            dungeon_materialize_design_span(expansion_id=request.expansion_id) as span,
        ):
            _mat_module._stage_design(request, graph=graph, palette=palette, span=span)


# ---------------------------------------------------------------------------
# Task 3: Stage 2 fill — generate_interior per region, theme-keyed, span
# ---------------------------------------------------------------------------


def _theme_for_class(
    tid: str,
    generator_class: str,
    *,
    braid_ratio: float = 0.0,
) -> Any:
    """Build a real DungeonTheme of the given generator_class with the
    spec §5.2 algorithm enforced by the model_validator. No mocking — real
    value objects only."""
    from sidequest.dungeon.themes import (
        Adjacency,
        DepthBand,
        DungeonTheme,
        InteriorSpec,
        NarratorFlavor,
    )

    class_algorithm = {
        "organic": "cellular",
        "labyrinthine": "depthfirst",
        "structured": "prim",
        "built": "roomcorridor",
    }
    return DungeonTheme(
        id=tid,
        display_name=tid.replace("_", " ").title(),
        generator_class=generator_class,
        interior=InteriorSpec(
            algorithm=class_algorithm[generator_class],
            braid_ratio=braid_ratio,
        ),
        depth_band=DepthBand(min=0.0, max=None),
        narrator=NarratorFlavor(register="grave", flavor="dread whispers"),
        adjacency=Adjacency(),
    )


def _make_request_task3(
    *,
    campaign_seed: int = 42,
    expansion_id: int = 1,
) -> Any:
    from sidequest.dungeon.materializer import MaterializationRequest
    from sidequest.dungeon.persistence import FrontierEdge

    fe = FrontierEdge(
        frontier_edge_id="fe1",
        from_region_id="entrance",
        heading="north",
        spawn_depth_score=15.0,
    )
    return MaterializationRequest.build(
        campaign_seed=campaign_seed,
        expansion_id=expansion_id,
        frontier_edge=fe,
        frontier=[fe],
        attach_region_ids=["entrance"],
        heading="north",
        burst_magnitude=3,
        lookahead_breadth=2,
    )


def _expansion_with_themes(*theme_ids: str, expansion_id: int = 1) -> Any:
    from sidequest.dungeon.region_graph import Expansion
    from sidequest.dungeon.region_graph.model import RegionNode

    nodes = [
        RegionNode(id=f"exp{expansion_id:03d}.r{i}", expansion_id=expansion_id, theme=tid)
        for i, tid in enumerate(theme_ids)
    ]
    return Expansion(expansion_id=expansion_id, new_nodes=nodes, new_edges=[])


def _setup_otel_task3() -> tuple[Any, Any, Any]:
    import sidequest.telemetry.spans as _spans_module

    exporter, _provider, real_tracer = _otel_in_memory()
    original_tracer_fn = _spans_module.tracer
    _spans_module.tracer = lambda: real_tracer  # type: ignore[method-assign]
    return exporter, original_tracer_fn, _spans_module


class TestStageFill:
    """Task 3 tests:
    1. Every generator_class in the expansion maps to its spec §5.2 generator
       (cellular/depthfirst/prim/roomcorridor); an unknown algorithm raises
       loudly via generate_interior's OWN guard (not a re-implemented check).
    2. A labyrinth-trap theme fills with braid_ratio=0.0 (pristine); a non-trap
       maze theme with its palette braid_ratio. The span records the
       actually-applied ratio (lie detector: prove no silent default).
    """

    def test_each_generator_class_maps_to_its_spec_generator(self) -> None:
        """A region per generator_class fills with its §5.2 algorithm; the
        routed span payload records the algorithm actually used per region."""

        import sidequest.dungeon.materializer as _mat_module
        from sidequest.dungeon.themes import ThemePalette
        from sidequest.telemetry.spans import SPAN_ROUTES
        from sidequest.telemetry.spans.dungeon_materialize import (
            SPAN_DUNGEON_MATERIALIZE_FILL,
            dungeon_materialize_fill_span,
        )

        themes = {
            "t_organic": _theme_for_class("t_organic", "organic"),
            "t_laby": _theme_for_class("t_laby", "labyrinthine"),
            "t_struct": _theme_for_class("t_struct", "structured"),
            "t_built": _theme_for_class("t_built", "built"),
        }
        palette = ThemePalette(themes=themes)
        expansion = _expansion_with_themes("t_organic", "t_laby", "t_struct", "t_built")
        request = _make_request_task3()

        exporter, original_tracer_fn, _spans_mod = _setup_otel_task3()
        try:
            with dungeon_materialize_fill_span(expansion_id=request.expansion_id) as span:
                result = _mat_module._stage_fill(
                    request, expansion=expansion, palette=palette, span=span
                )
        finally:
            _spans_mod.tracer = original_tracer_fn

        # Result is an ordered mapping region_id -> RegionFill
        by_region = {rf.region_id: rf for rf in result.values()}
        assert by_region["exp001.r0"].algorithm == "cellular"
        assert by_region["exp001.r1"].algorithm == "depthfirst"
        assert by_region["exp001.r2"].algorithm == "prim"
        assert by_region["exp001.r3"].algorithm == "roomcorridor"

        # The routed span payload (what the GM panel renders) must surface
        # the per-region algorithm — not just the in-memory result.
        finished = exporter.get_finished_spans()
        fill_spans = [s for s in finished if s.name == SPAN_DUNGEON_MATERIALIZE_FILL]
        assert fill_spans, "dungeon.materialize.fill span not emitted"
        route = SPAN_ROUTES[SPAN_DUNGEON_MATERIALIZE_FILL]
        fields = route.extract(fill_spans[0])  # type: ignore[arg-type]
        regions_payload = json.loads(fields["regions"])
        algo_by_region = {r["region_id"]: r["algorithm"] for r in regions_payload}
        assert algo_by_region == {
            "exp001.r0": "cellular",
            "exp001.r1": "depthfirst",
            "exp001.r2": "prim",
            "exp001.r3": "roomcorridor",
        }

    def test_unknown_algorithm_raises_loudly_via_generators_own_guard(
        self,
    ) -> None:
        """Defense-in-depth: prove the materializer does NOT re-implement the
        algorithm check — it relies on generate_interior's OWN guard.

        In production this path is UNREACHABLE: InteriorSpec.algorithm's
        field_validator is the real gate at palette load, so a bad algorithm
        can never reach _stage_fill. This test deliberately bypasses that
        load-time gate (mutating the already-validated model in place) to
        force the materializer to dispatch a bad algorithm, then asserts
        generate_interior's own ValueError fires and the span carries the
        failure. It is not testing a real production failure mode — it pins
        the "reuse, don't re-implement the guard" contract."""
        import sidequest.dungeon.materializer as _mat_module
        from sidequest.dungeon.themes import ThemePalette
        from sidequest.telemetry.spans import SPAN_ROUTES
        from sidequest.telemetry.spans.dungeon_materialize import (
            SPAN_DUNGEON_MATERIALIZE_FILL,
            dungeon_materialize_fill_span,
        )

        theme = _theme_for_class("t_organic", "organic")
        palette = ThemePalette(themes={"t_organic": theme})
        # The real production gate is InteriorSpec.algorithm's field_validator,
        # which runs at palette LOAD (not on assignment — Pydantic v2 does not
        # validate-on-assign by default, so a plain attribute set would equally
        # bypass it). Mutating the already-validated model here simulates an
        # algorithm that escaped that load-time gate, forcing the materializer
        # to dispatch it so generate_interior's OWN guard is the thing that
        # rejects it (we never re-implement that check).
        object.__setattr__(theme.interior, "algorithm", "no_such_algo")
        expansion = _expansion_with_themes("t_organic")
        request = _make_request_task3()

        exporter, original_tracer_fn, _spans_mod = _setup_otel_task3()
        try:
            with (
                pytest.raises(ValueError, match="unknown interior algorithm"),
                dungeon_materialize_fill_span(expansion_id=request.expansion_id) as span,
            ):
                _mat_module._stage_fill(request, expansion=expansion, palette=palette, span=span)
        finally:
            _spans_mod.tracer = original_tracer_fn

        # The fill span must carry the failure marker (lie-detector visibility)
        finished = exporter.get_finished_spans()
        fill_spans = [s for s in finished if s.name == SPAN_DUNGEON_MATERIALIZE_FILL]
        assert fill_spans, "fill span must be emitted even on failure"
        route = SPAN_ROUTES[SPAN_DUNGEON_MATERIALIZE_FILL]
        fields = route.extract(fill_spans[0])  # type: ignore[arg-type]
        assert fields.get("error") is not None
        assert "no_such_algo" in fields["error"]

    def test_missing_theme_raises_loudly_with_span_failure(self) -> None:
        """A region whose theme is absent from the palette is a loud
        ValueError; the span carries the failure."""
        import sidequest.dungeon.materializer as _mat_module
        from sidequest.dungeon.themes import ThemePalette
        from sidequest.telemetry.spans.dungeon_materialize import (
            SPAN_DUNGEON_MATERIALIZE_FILL,
            dungeon_materialize_fill_span,
        )

        palette = ThemePalette(themes={"t_organic": _theme_for_class("t_organic", "organic")})
        expansion = _expansion_with_themes("t_organic", "t_absent")
        request = _make_request_task3()

        exporter, original_tracer_fn, _spans_mod = _setup_otel_task3()
        try:
            with (
                pytest.raises(ValueError, match="t_absent"),
                dungeon_materialize_fill_span(expansion_id=request.expansion_id) as span,
            ):
                _mat_module._stage_fill(request, expansion=expansion, palette=palette, span=span)
        finally:
            _spans_mod.tracer = original_tracer_fn

        finished = exporter.get_finished_spans()
        fill_spans = [s for s in finished if s.name == SPAN_DUNGEON_MATERIALIZE_FILL]
        assert fill_spans
        attrs = dict(fill_spans[0].attributes or {})
        assert "error" in attrs

    def test_braid_ratio_applied_and_recorded_per_region(self) -> None:
        """A labyrinth-trap theme (braid_ratio=0.0) fills pristine; a non-trap
        maze theme fills with its palette braid_ratio (0.3). The span records
        the ACTUALLY-applied ratio per region (lie detector: not a default)."""

        import sidequest.dungeon.materializer as _mat_module
        from sidequest.dungeon.themes import ThemePalette
        from sidequest.telemetry.spans import SPAN_ROUTES
        from sidequest.telemetry.spans.dungeon_materialize import (
            SPAN_DUNGEON_MATERIALIZE_FILL,
            dungeon_materialize_fill_span,
        )

        trap = _theme_for_class("t_trap", "labyrinthine", braid_ratio=0.0)
        maze = _theme_for_class("t_maze", "labyrinthine", braid_ratio=0.3)
        palette = ThemePalette(themes={"t_trap": trap, "t_maze": maze})
        expansion = _expansion_with_themes("t_trap", "t_maze")
        request = _make_request_task3()

        exporter, original_tracer_fn, _spans_mod = _setup_otel_task3()
        try:
            with dungeon_materialize_fill_span(expansion_id=request.expansion_id) as span:
                result = _mat_module._stage_fill(
                    request, expansion=expansion, palette=palette, span=span
                )
        finally:
            _spans_mod.tracer = original_tracer_fn

        by_region = {rf.region_id: rf for rf in result.values()}
        assert by_region["exp001.r0"].braid_ratio == 0.0
        assert by_region["exp001.r1"].braid_ratio == 0.3

        finished = exporter.get_finished_spans()
        fill_spans = [s for s in finished if s.name == SPAN_DUNGEON_MATERIALIZE_FILL]
        route = SPAN_ROUTES[SPAN_DUNGEON_MATERIALIZE_FILL]
        fields = route.extract(fill_spans[0])  # type: ignore[arg-type]
        ratio_by_region = {r["region_id"]: r["braid_ratio"] for r in json.loads(fields["regions"])}
        assert ratio_by_region == {"exp001.r0": 0.0, "exp001.r1": 0.3}, (
            "span must record the actually-applied braid_ratio per region "
            "(0.0 trap, 0.3 maze) — proving no silent default"
        )

    def test_fill_is_deterministic_for_identical_inputs(self) -> None:
        """Identical (campaign_seed, expansion_id, region.id) ⇒ identical grid
        — the determinism contract for raw seed-reproducible fill."""
        import sidequest.dungeon.materializer as _mat_module
        from sidequest.dungeon.themes import ThemePalette
        from sidequest.telemetry.spans.dungeon_materialize import (
            dungeon_materialize_fill_span,
        )

        palette = ThemePalette(themes={"t_organic": _theme_for_class("t_organic", "organic")})

        def _run() -> Any:
            expansion = _expansion_with_themes("t_organic")
            request = _make_request_task3(campaign_seed=99, expansion_id=2)
            _exp, orig, mod = _setup_otel_task3()
            try:
                with dungeon_materialize_fill_span(expansion_id=request.expansion_id) as span:
                    return _mat_module._stage_fill(
                        request, expansion=expansion, palette=palette, span=span
                    )
            finally:
                mod.tracer = orig

        r1 = _run()
        r2 = _run()
        g1 = next(iter(r1.values())).grid
        g2 = next(iter(r2.values())).grid
        assert g1 == g2, "identical inputs must yield byte-identical grids"

    def test_roomcorridor_below_min_dim_raises_loudly(self) -> None:
        """A roomcorridor region below ROOMCORRIDOR_MIN_DIM is a loud
        ValueError naming the region, algorithm, dims and floor — no silent
        shrink/grow/skip (No Silent Fallbacks)."""
        import sidequest.dungeon.materializer as _mat_module
        from sidequest.dungeon.themes import ThemePalette
        from sidequest.telemetry.spans.dungeon_materialize import (
            dungeon_materialize_fill_span,
        )

        # Force the default dims below the roomcorridor floor for this test.
        orig_w = _mat_module.DEFAULT_INTERIOR_WIDTH
        orig_h = _mat_module.DEFAULT_INTERIOR_HEIGHT
        _mat_module.DEFAULT_INTERIOR_WIDTH = 7  # type: ignore[misc]
        _mat_module.DEFAULT_INTERIOR_HEIGHT = 7  # type: ignore[misc]
        palette = ThemePalette(themes={"t_built": _theme_for_class("t_built", "built")})
        expansion = _expansion_with_themes("t_built")
        request = _make_request_task3()
        try:
            with (
                pytest.raises(
                    ValueError,
                    match="roomcorridor.*floor|ROOMCORRIDOR_MIN_DIM|7",
                ),
                dungeon_materialize_fill_span(expansion_id=request.expansion_id) as span,
            ):
                _mat_module._stage_fill(request, expansion=expansion, palette=palette, span=span)
        finally:
            _mat_module.DEFAULT_INTERIOR_WIDTH = orig_w  # type: ignore[misc]
            _mat_module.DEFAULT_INTERIOR_HEIGHT = orig_h  # type: ignore[misc]

    def test_degenerate_braid_fixed_point_seed_raises_loudly(self) -> None:
        """If a derived per-region interior seed equals the braid fixed point
        (0x5EED == 24301), fill must fail loudly rather than feed the braid
        sub-seed its degenerate fixed point."""
        import sidequest.dungeon.materializer as _mat_module
        from sidequest.dungeon.themes import ThemePalette
        from sidequest.telemetry.spans.dungeon_materialize import (
            dungeon_materialize_fill_span,
        )

        palette = ThemePalette(themes={"t_organic": _theme_for_class("t_organic", "organic")})
        expansion = _expansion_with_themes("t_organic")
        request = _make_request_task3()

        # Force the seed mixer to return the fixed point for this region.
        orig_mixer = _mat_module._region_interior_seed
        _mat_module._region_interior_seed = (  # type: ignore[assignment]
            lambda *a, **k: _mat_module._BRAID_FIXED_POINT
        )
        try:
            with (
                pytest.raises(ValueError, match="24301|fixed point|0x5EED"),
                dungeon_materialize_fill_span(expansion_id=request.expansion_id) as span,
            ):
                _mat_module._stage_fill(request, expansion=expansion, palette=palette, span=span)
        finally:
            _mat_module._region_interior_seed = orig_mixer  # type: ignore[assignment]

    async def test_fill_wired_into_coordinator(self, monkeypatch: Any, migrated_db: str) -> None:
        """Wiring test: materialize() reaches _stage_fill with real
        expansion+palette threaded from _stage_design, and (Task 6 landed)
        the pipeline now runs all the way through fill → curate → attach →
        commit to completion. Proving the run completes proves fill was
        reached and its result threaded forward (commit needs the attach
        result, which needs curate, which needs fill). A schema-ready
        PgDungeonRepository is required because Task 6's commit introspects
        + writes the real save (the production shape)."""
        import sidequest.telemetry.spans as _spans_module
        from sidequest.dungeon.materializer import materialize
        from tests.dungeon.conftest import build_pg_dungeon_repo

        _pool, repo, _sid = build_pg_dungeon_repo(monkeypatch, migrated_db)

        bundle = _real_cookbook_bundle()
        theme_id = "fill_wired_crypt"
        palette = _commit_palette(theme_id)
        graph = _seed_graph_themed(theme_id)

        _exporter, _provider, real_tracer = _otel_in_memory()
        original_tracer_fn = _spans_module.tracer
        _spans_module.tracer = lambda: real_tracer  # type: ignore[method-assign]
        try:
            req = _make_request_task3()
            # design + fill + curate + attach + commit all run (Tasks 2–6).
            await materialize(
                req,
                graph=graph,
                bundle=bundle,
                palette=palette,
                dungeon_repository=repo,
                snapshot=_fresh_snapshot(),
                pack_tropes=_attach_pack("cave_in"),
                claude_client=_reflecting_sdk_client(),
            )
        finally:
            _spans_module.tracer = original_tracer_fn  # type: ignore[method-assign]

        # Commit is immediately live: fill's result reached commit through
        # the whole chain.
        assert "entrance" in repo.load_map(entrance_id="entrance").nodes


# ---------------------------------------------------------------------------
# Task 4: Stage 3 curate — assemble_region + one-shot claude -p + CR→Edge
# ---------------------------------------------------------------------------

# The real shipped beneath_sunden world dir (Plan 5/8). Discovery mirrors
# tests/genre/test_beneath_sunden_world_load.py — parents[3] is the
# orchestrator root; the world dir is the load_cookbook input.
_BENEATH_SUNDEN_WORLD = (
    Path(__file__).resolve().parents[3]
    / "sidequest-content/genre_packs/caverns_and_claudes/worlds/beneath_sunden"
)


def _real_cookbook_bundle() -> Any:
    """Load the REAL beneath_sunden cookbook (no mocking the content)."""
    from sidequest.game.cookbook.loader import load_cookbook

    return load_cookbook(_BENEATH_SUNDEN_WORLD)


def _reflecting_sdk_client(**_: Any) -> Any:
    """ToolingLlmClient-shaped fake: parses the curation prompt's
    ``INPUT:\\n<json>`` and echoes a well-formed per-region verdict as
    ToolingResult.text. The shared Plan-7 curate-success fake — NEVER a
    real network call (the only mocked seam).

    Accepts (and ignores) arbitrary kwargs so it can stand in for the
    real ``build_llm_client(purpose=...)`` factory via ``monkeypatch``.
    """
    import json as _json

    from sidequest.agents.tooling_protocol import ToolingResult

    class _ReflectingSdk:
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

    return _ReflectingSdk()


def _failing_sdk_client() -> Any:
    """ToolingLlmClient-shaped fake whose call fails with an
    LlmClientError subclass (SDK analog of a curation subprocess
    failure)."""
    from sidequest.agents.anthropic_sdk_client import AnthropicSdkClientError

    class _FailingSdk:
        async def complete_with_tools(self, *a: Any, **k: Any) -> Any:
            raise AnthropicSdkClientError("forced curation failure (test)")

    return _FailingSdk()


# ---------------------------------------------------------------------------
# Story 50-26 — curate-stage robustness fakes (ADR-106 Amendment A).
# The pingpong failure shape: the SDK verdict is cut mid-`wandering_table`
# string (the 4096-token-default deterministic truncation OQ-1 captured:
# "Unterminated string starting at: line 452 column 17").
# ---------------------------------------------------------------------------

# Unterminated JSON — `json.loads` raises json.JSONDecodeError on this exact
# shape (open string after "Skeleton", no closing quote/brace). This is the
# verbatim head OQ-1 recorded in sq-playtest-pingpong.md.
_PINGPONG_TRUNCATED_VERDICT = (
    '{"exp001.r0": {"race": "undead", "cr_band": "shallow", '
    '"wandering_table": [{"name": "Skeleton",'
)


def _tooling_result(text: str, model: str) -> Any:
    from sidequest.agents.tooling_protocol import ToolingResult

    return ToolingResult(
        text=text,
        stop_reason="end_turn",
        input_tokens=1,
        output_tokens=7,
        cached_input_read_tokens=0,
        cached_input_write_tokens=0,
        model=model,
    )


def _well_formed_verdict_text(messages: Any) -> str:
    """Echo the curate prompt's INPUT as a well-formed per-region verdict
    (same logic as _reflecting_sdk_client — a SUCCESS curation)."""
    import json as _json

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
    return _json.dumps(verdict)


def _truncating_sdk_client(call_log: list[int] | None = None) -> Any:
    """ALWAYS returns the pingpong unterminated-JSON verdict — every
    attempt. Drives Layer-1 retry-exhaustion → Layer-2 loud degrade.
    `call_log` (if given) gets one entry appended per curate call so the
    test can assert the attempt count is bounded (AC-4)."""

    class _Truncating:
        async def complete_with_tools(self, *a: Any, model: str, **k: Any) -> Any:
            if call_log is not None:
                call_log.append(1)
            return _tooling_result(_PINGPONG_TRUNCATED_VERDICT, model)

    return _Truncating()


def _truncated_then_valid_sdk_client(call_log: list[int] | None = None) -> Any:
    """Attempt 1 → unterminated JSON; attempt 2 → a well-formed verdict.
    Drives the Layer-1 retry-RECOVERS path (no degrade, curated=True,
    exactly one parse_failed span for attempt 1)."""

    state = {"n": 0}

    class _TruncatedThenValid:
        async def complete_with_tools(self, *a: Any, model: str, messages: Any, **k: Any) -> Any:
            state["n"] += 1
            if call_log is not None:
                call_log.append(state["n"])
            if state["n"] == 1:
                return _tooling_result(_PINGPONG_TRUNCATED_VERDICT, model)
            return _tooling_result(_well_formed_verdict_text(messages), model)

    return _TruncatedThenValid()


def _slow_then_valid_sdk_client(delay_s: float) -> Any:
    """Sleeps `delay_s` then returns a well-formed verdict. With an
    injected tiny curate deadline this drives the Layer-1 wall-clock-cap
    → Layer-2 degrade (failure_kind='deadline')."""
    import asyncio as _asyncio

    class _Slow:
        async def complete_with_tools(self, *a: Any, model: str, messages: Any, **k: Any) -> Any:
            await _asyncio.sleep(delay_s)
            return _tooling_result(_well_formed_verdict_text(messages), model)

    return _Slow()


def _per_region_partial_sdk_client() -> Any:
    """Top-level JSON PARSES, but one region's value is structurally
    broken (a string, not an object). Drives per-region isolation: the
    broken region degrades loudly; sibling regions stay curated; the
    whole expansion is NOT aborted (ADR-106 Amendment A — per-region)."""
    import json as _json

    class _PartialVerdict:
        async def complete_with_tools(self, *a: Any, model: str, messages: Any, **k: Any) -> Any:
            prompt = messages[0].content
            _, _, input_blob = prompt.partition("INPUT:\n")
            payload = _json.loads(input_blob)
            region_ids = list(payload)
            verdict: dict[str, Any] = {}
            for i, region_id in enumerate(region_ids):
                region = payload[region_id]
                if i == len(region_ids) - 1 and len(region_ids) > 1:
                    # Last region: structurally broken (degradable).
                    verdict[region_id] = "THE_CURATOR_RETURNED_PROSE_HERE"
                else:
                    verdict[region_id] = {
                        "race": region["race"],
                        "cr_band": region["cr_band"],
                        "wandering_table": [
                            {**row, "telegraph": (row.get("telegraph") or "It is here.")}
                            for row in region["wandering_table"]
                        ],
                        "big_bad": region["big_bad"],
                    }
            return _tooling_result(_json.dumps(verdict), model)

    return _PartialVerdict()


def _missing_cr_sdk_client() -> Any:
    """Parseable verdict whose kept wandering row DROPPED `cr`. Per
    Amendment A this is the RETAINED `CurationError` carve-out (ii):
    degrading would corrupt the CR→Edge seam, so it MUST still raise —
    NOT degrade. This fake guards against over-degradation."""
    import json as _json

    class _MissingCr:
        async def complete_with_tools(self, *a: Any, model: str, messages: Any, **k: Any) -> Any:
            prompt = messages[0].content
            _, _, input_blob = prompt.partition("INPUT:\n")
            payload = _json.loads(input_blob)
            verdict = {
                region_id: {
                    "race": region["race"],
                    "cr_band": region["cr_band"],
                    "wandering_table": [
                        {k: v for k, v in row.items() if k != "cr"}
                        for row in region["wandering_table"]
                    ],
                    "big_bad": region["big_bad"],
                }
                for region_id, region in payload.items()
            }
            return _tooling_result(_json.dumps(verdict), model)

    return _MissingCr()


def _curate_inputs_two_regions(
    *, algorithm: str = "prim", expansion_id: int = 9, depth_score: float = 0.5
) -> tuple[Any, Any, Any, Any]:
    """Like `_curate_inputs` but a TWO-region expansion (r0, r1) so
    per-region isolation is testable. Returns (request, palette,
    expansion, fill_result)."""
    from sidequest.dungeon.materializer import MaterializationRequest, RegionFill
    from sidequest.dungeon.persistence import FrontierEdge
    from sidequest.dungeon.region_graph import Expansion
    from sidequest.dungeon.region_graph.model import RegionNode
    from sidequest.dungeon.themes import ThemePalette

    theme_id = f"t_{algorithm}"
    palette = ThemePalette(themes={theme_id: _theme_bound_to_look(theme_id, algorithm)})
    r0 = f"exp{expansion_id:03d}.r0"
    r1 = f"exp{expansion_id:03d}.r1"
    nodes = [
        RegionNode(id=r0, expansion_id=expansion_id, theme=theme_id),
        RegionNode(id=r1, expansion_id=expansion_id, theme=theme_id),
    ]
    expansion = Expansion(expansion_id=expansion_id, new_nodes=nodes, new_edges=[])
    fe = FrontierEdge(
        frontier_edge_id="fe1",
        from_region_id="entrance",
        heading="north",
        spawn_depth_score=depth_score,
    )
    request = MaterializationRequest.build(
        campaign_seed=7,
        expansion_id=expansion_id,
        frontier_edge=fe,
        frontier=[fe],
        attach_region_ids=["entrance"],
        heading="north",
        burst_magnitude=3,
        lookahead_breadth=2,
    )
    fill_result = {
        rid: RegionFill(
            region_id=rid,
            algorithm=algorithm,
            width=49,
            height=49,
            braid_ratio=0.0,
            grid=[[0]],
        )
        for rid in (r0, r1)
    }
    return request, palette, expansion, fill_result


def _theme_bound_to_look(theme_id: str, algorithm: str) -> Any:
    """A real DungeonTheme whose interior.algorithm is the join key onto
    a LookDef.generator_binding (the resolved look→theme seam)."""
    from sidequest.dungeon.themes import (
        Adjacency,
        DepthBand,
        DungeonTheme,
        InteriorSpec,
        NarratorFlavor,
    )

    algo_class = {
        "cellular": "organic",
        "depthfirst": "labyrinthine",
        "prim": "structured",
        "roomcorridor": "built",
    }
    return DungeonTheme(
        id=theme_id,
        display_name=theme_id.replace("_", " ").title(),
        generator_class=algo_class[algorithm],
        interior=InteriorSpec(algorithm=algorithm, braid_ratio=0.0),
        depth_band=DepthBand(min=0.0, max=None),
        narrator=NarratorFlavor(register="grave", flavor="dread"),
        adjacency=Adjacency(),
    )


def _curate_inputs(
    *,
    algorithm: str = "prim",
    expansion_id: int = 1,
    depth_score: float = 0.5,
) -> tuple[Any, Any, Any, Any, str]:
    """Build (request, palette, expansion, fill_result, look) bound to a
    real look. `prim` → look `delvehold` (race dwarf at depth 0.5 → band
    `mid`, which has a big_bad gate — every creature must Edge-translate).
    """
    from sidequest.dungeon.materializer import MaterializationRequest, RegionFill
    from sidequest.dungeon.persistence import FrontierEdge
    from sidequest.dungeon.region_graph import Expansion
    from sidequest.dungeon.region_graph.model import RegionNode
    from sidequest.dungeon.themes import ThemePalette

    theme_id = f"t_{algorithm}"
    palette = ThemePalette(themes={theme_id: _theme_bound_to_look(theme_id, algorithm)})
    rid = f"exp{expansion_id:03d}.r0"
    nodes = [RegionNode(id=rid, expansion_id=expansion_id, theme=theme_id)]
    expansion = Expansion(expansion_id=expansion_id, new_nodes=nodes, new_edges=[])
    fe = FrontierEdge(
        frontier_edge_id="fe1",
        from_region_id="entrance",
        heading="north",
        spawn_depth_score=depth_score,
    )
    request = MaterializationRequest.build(
        campaign_seed=7,
        expansion_id=expansion_id,
        frontier_edge=fe,
        frontier=[fe],
        attach_region_ids=["entrance"],
        heading="north",
        burst_magnitude=3,
        lookahead_breadth=2,
    )
    fill_result = {
        rid: RegionFill(
            region_id=rid,
            algorithm=algorithm,
            width=49,
            height=49,
            braid_ratio=0.0,
            grid=[[0]],
        )
    }
    # `prim` is the generator_binding of look `delvehold` in the real
    # beneath_sunden cookbook (looks.yaml).
    look = "delvehold"
    return request, palette, expansion, fill_result, look


class TestStageCurate:
    """Task 4 tests (the 3 plan bullets):
    1. assemble_region called with EXACTLY the named signal kwargs (int→str
       at the seam); manifest deterministic for identical inputs (up to the
       curation seam).
    2. A curation subprocess failure raises loudly + aborts; the span
       records curated=false + a reason; raw manifest is NOT shipped
       stamped curated.
    3. Every corpus creature crossing the seam emerges with an HpPool —
       no raw cr leaks into the curate-stage output (ADR-114).
    """

    async def test_assemble_region_called_with_exact_signal_kwargs_and_is_deterministic(
        self,
    ) -> None:
        import sidequest.dungeon.materializer as _mat
        from sidequest.telemetry.spans.dungeon_materialize import (
            dungeon_materialize_curate_span,
        )

        bundle = _real_cookbook_bundle()
        request, palette, expansion, fill_result, look = _curate_inputs(
            algorithm="prim", expansion_id=4, depth_score=0.5
        )
        rid0 = expansion.new_nodes[0].id

        # Spy on assemble_region at the materializer's imported name.
        calls: list[dict] = []
        original = _mat.assemble_region

        def _spy(bundle_arg: Any, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return original(bundle_arg, **kwargs)

        # A success curation that echoes the manifest back unchanged.
        exporter, original_tracer_fn, _spans_mod = _setup_otel_task3()
        _mat.assemble_region = _spy  # type: ignore[assignment]
        try:
            with dungeon_materialize_curate_span(expansion_id=request.expansion_id) as span:
                # _reflecting_sdk_client echoes the stage's actual curation
                # prompt INPUT (== the manifest _spy/assemble_region built)
                # — a success curation that returns the manifest unchanged.
                result = await _mat._stage_curate(
                    request,
                    bundle=bundle,
                    palette=palette,
                    expansion=expansion,
                    fill_result=fill_result,
                    is_first_band_entry=True,
                    claude_client=_reflecting_sdk_client(),
                    span=span,
                )
        finally:
            _mat.assemble_region = original  # type: ignore[assignment]
            _spans_mod.tracer = original_tracer_fn

        assert calls, "assemble_region was not called by _stage_curate"
        kw = calls[0]
        # DIVERGENCE 1: campaign_seed / expansion_id are str at this seam.
        assert kw["campaign_seed"] == str(request.campaign_seed)
        assert kw["expansion_id"] == str(request.expansion_id)
        assert isinstance(kw["campaign_seed"], str)
        assert isinstance(kw["expansion_id"], str)
        # depth_score from the frontier edge; burst from the request.
        assert kw["depth_score"] == request.frontier_edge.spawn_depth_score
        assert kw["burst_magnitude"] == request.burst_magnitude
        # The look the stage passed to assemble_region is the one it
        # derived loudly from the region theme (theme=t_prim →
        # generator_binding 'prim' → cookbook look 'delvehold'). No
        # caller override exists; assert the derived value end-to-end.
        assert kw["look"] == "delvehold"
        assert result.region_look[rid0] == "delvehold"
        assert kw["is_first_band_entry"] is True

        # Pre-curation determinism: assemble_region is pure — identical
        # inputs ⇒ identical manifest (the contract holds UP TO the
        # curation seam).
        m_a = original(
            bundle,
            campaign_seed=str(request.campaign_seed),
            expansion_id=str(request.expansion_id),
            depth_score=request.frontier_edge.spawn_depth_score,
            burst_magnitude=request.burst_magnitude,
            look=look,
            is_first_band_entry=True,
            room_id=rid0,
        )
        m_b = original(
            bundle,
            campaign_seed=str(request.campaign_seed),
            expansion_id=str(request.expansion_id),
            depth_score=request.frontier_edge.spawn_depth_score,
            burst_magnitude=request.burst_magnitude,
            look=look,
            is_first_band_entry=True,
            room_id=rid0,
        )
        assert m_a.model_dump() == m_b.model_dump(), (
            "assemble_region must be deterministic for identical inputs "
            "(pre-curation determinism contract, up to the curation seam)"
        )

    async def test_curation_llm_failure_degrades_loudly_not_raises_amendment_a(
        self,
    ) -> None:
        """SUPERSEDED CONTRACT (story 50-26 / ADR-106 Amendment A).

        This test previously asserted an `LlmClientError` curate failure
        RAISES `CurationError` and aborts. Amendment A makes that the
        frozen/aborted turn the contract exists to eliminate: `llm_error`
        is an enumerated degrade `failure_kind`, and it is NOT one of the
        two retained `CurationError` carve-outs ((i) invalid assembled
        manifest, (ii) post-parse mechanical corruption). So a persistent
        LLM-call failure must Layer-1-retry (2 attempts) then Layer-2
        LOUD-degrade — NOT raise. RED on develop (current code raises on
        the first LlmClientError). The retained lie-detector assertion
        (`dungeon.materialize.curate` stage span records curated=False)
        stays — only the raise becomes a loud degrade.
        """
        import sidequest.dungeon.materializer as _mat
        from sidequest.dungeon.materializer import CurationError
        from sidequest.telemetry.spans import SPAN_ROUTES
        from sidequest.telemetry.spans.dungeon_materialize import (
            SPAN_DUNGEON_MATERIALIZE_CURATE,
            dungeon_materialize_curate_span,
        )

        bundle = _real_cookbook_bundle()
        request, palette, expansion, fill_result, _look = _curate_inputs()
        rid = expansion.new_nodes[0].id

        exporter, original_tracer_fn, _spans_mod = _setup_otel_task3()
        try:
            with dungeon_materialize_curate_span(expansion_id=request.expansion_id) as span:
                try:
                    result = await _mat._stage_curate(
                        request,
                        bundle=bundle,
                        palette=palette,
                        expansion=expansion,
                        fill_result=fill_result,
                        is_first_band_entry=True,
                        claude_client=_failing_sdk_client(),
                        span=span,
                    )
                except CurationError as exc:  # pragma: no cover - RED proof
                    pytest.fail(
                        "Amendment A: a persistent LLM-call failure is "
                        "failure_kind='llm_error' — it must Layer-2 "
                        f"degrade, not raise CurationError. Got: {exc}"
                    )
        finally:
            _spans_mod.tracer = original_tracer_fn

        # New contract: loud degrade, turn proceeds.
        assert result.curated is False
        assert result.curated is not True  # forbidden silent fallback
        assert rid in result.uncurated_regions
        assert rid in result.region_manifests  # honest coal shipped
        degraded = self._spans_named(exporter, "dungeon.curate.degraded")
        assert degraded, "llm_error must Layer-2 degrade loudly"
        assert dict(degraded[0].attributes or {}).get("failure_kind") == "llm_error"
        # RETAINED: the stage span still surfaces curated=False to the GM
        # panel (set-but-not-routed was the Task-2 defect lesson).
        curate_spans = [
            s for s in exporter.get_finished_spans() if s.name == SPAN_DUNGEON_MATERIALIZE_CURATE
        ]
        assert curate_spans, "stage curate span must be emitted even on degrade"
        fields = SPAN_ROUTES[SPAN_DUNGEON_MATERIALIZE_CURATE].extract(
            curate_spans[0]  # type: ignore[arg-type]
        )
        assert fields.get("curated") is False
        assert fields.get("curated") is not True

    def _spans_named(self, exporter: Any, name: str) -> list[Any]:
        return [s for s in exporter.get_finished_spans() if s.name == name]

    async def test_every_corpus_creature_emerges_with_edge_no_raw_cr_hp(
        self,
    ) -> None:
        import sidequest.dungeon.materializer as _mat
        from sidequest.game.creature_core import HpPool
        from sidequest.telemetry.spans.dungeon_materialize import (
            dungeon_materialize_curate_span,
        )

        bundle = _real_cookbook_bundle()
        # depth 0.5 → band `mid`; `mid` is in
        # affinities.big_bad_gate.on_first_band_entry, and the fixture
        # passes is_first_band_entry=True, so the manifest is GUARANTEED a
        # big_bad — both the wandering table AND a big_bad cross the
        # CR→Edge seam (verified: race=dwarf yields big_bad 'Wight').
        request, palette, expansion, fill_result, look = _curate_inputs(depth_score=0.5)
        rid0 = expansion.new_nodes[0].id

        from sidequest.dungeon.materializer import assemble_region

        m0 = assemble_region(
            bundle,
            campaign_seed=str(request.campaign_seed),
            expansion_id=str(request.expansion_id),
            depth_score=0.5,
            burst_magnitude=request.burst_magnitude,
            look=look,
            is_first_band_entry=True,
            room_id=rid0,
        )
        assert m0.wandering_table, "fixture must exercise a non-empty wandering table"
        assert m0.big_bad is not None, (
            "band 'mid' + is_first_band_entry=True must yield a big_bad in "
            "the real cookbook so the big_bad CR→Edge path is provably "
            "exercised (fixture/affinities contract)"
        )

        _exporter, original_tracer_fn, _spans_mod = _setup_otel_task3()
        try:
            with dungeon_materialize_curate_span(expansion_id=request.expansion_id) as span:
                # _reflecting_sdk_client echoes the curation prompt INPUT
                # (== the m0 manifest the stage assembles) unchanged — a
                # success curation exercising the full CR→Edge seam.
                result = await _mat._stage_curate(
                    request,
                    bundle=bundle,
                    palette=palette,
                    expansion=expansion,
                    fill_result=fill_result,
                    is_first_band_entry=True,
                    claude_client=_reflecting_sdk_client(),
                    span=span,
                )
        finally:
            _spans_mod.tracer = original_tracer_fn

        # I2: the look is derived from the theme inside the stage — assert
        # the resolved value end-to-end (no caller override exists).
        assert result.region_look[rid0] == "delvehold"

        creatures = result.creatures_for_region(rid0)
        assert creatures, "curated region must carry its creatures"
        for c in creatures:
            assert isinstance(c.hp, HpPool), (
                f"every corpus creature must emerge with an HpPool; {c.name!r} has {type(c.hp)}"
            )
            assert c.hp.max >= 1 and c.hp.current == c.hp.max
            # No raw cr may leak onto the curated creature object.
            assert not hasattr(c, "cr"), f"{c.name!r} leaked raw cr"

        # The big_bad (band `mid` gate, GUARANTEED above) must ALSO be
        # HP-translated — decisively, not conditionally.
        bb = result.big_bad_for_region(rid0)
        assert bb is not None, (
            "band 'mid' fixture must yield a big_bad so the big_bad "
            "CR→HP path is provably exercised"
        )
        assert isinstance(bb.hp, HpPool)
        assert bb.hp.max >= 1 and bb.hp.current == bb.hp.max
        assert not hasattr(bb, "cr")

    async def test_curate_wired_into_coordinator(self, monkeypatch: Any, migrated_db: str) -> None:
        """Wiring: materialize() reaches _stage_curate with real
        expansion+palette+bundle threaded from design/fill, runs the
        injected fake curation through the real look-resolution
        (prim→delvehold), and (Task 6 landed) the pipeline now runs PAST
        curate through attach + commit to completion — proving curate was
        reached and its RegionCuration threaded forward (commit needs the
        attach result, which needs curate's output)."""
        import sidequest.telemetry.spans as _spans_module
        from sidequest.dungeon.materializer import materialize
        from sidequest.dungeon.region_graph import RegionNode
        from tests.dungeon.conftest import build_pg_dungeon_repo

        bundle = _real_cookbook_bundle()
        # A palette whose single theme binds to look `delvehold` (prim) —
        # the curate look-resolution focus of this wiring test.
        request, palette, _expansion, _fill, _look = _curate_inputs(
            algorithm="prim", expansion_id=1, depth_score=0.5
        )
        theme_id = next(iter(palette.themes))
        # The coordinator runs the REAL _stage_design/_stage_fill; re-theme
        # the seed entrance to the palette's theme so it resolves for the
        # pre-existing node too (mirrors the Task-5 coordinator-threads
        # precedent).
        graph = _make_seed_graph("entrance")
        graph.nodes["entrance"] = RegionNode(id="entrance", expansion_id=0, theme=theme_id)

        # The REAL _stage_design generates region ids dynamically, so the
        # curation verdict cannot be precomputed. The reflecting SDK client
        # parses the curate stage's actual prompt and echoes a well-formed
        # per-region verdict — still never a real network call.
        reflecting = _reflecting_sdk_client()

        _pool, repo, _sid = build_pg_dungeon_repo(monkeypatch, migrated_db)
        _exporter, _provider, real_tracer = _otel_in_memory()
        original_tracer_fn = _spans_module.tracer
        _spans_module.tracer = lambda: real_tracer  # type: ignore[method-assign]
        try:
            # design + fill + curate + attach + commit all run (Tasks 2–6).
            await materialize(
                request,
                graph=graph,
                bundle=bundle,
                palette=palette,
                dungeon_repository=repo,
                snapshot=_fresh_snapshot(),
                pack_tropes=_attach_pack("cave_in"),
                claude_client=reflecting,
            )
        finally:
            _spans_module.tracer = original_tracer_fn  # type: ignore[method-assign]

        # Commit is immediately live: curate's RegionCuration reached
        # commit through attach.
        assert "entrance" in repo.load_map(entrance_id="entrance").nodes


class TestStageCurateRobustness:
    """Story 50-26 — RED against ADR-106 Amendment A (Curate-stage
    robustness contract). Every test here MUST FAIL on current `develop`
    (strict `json.loads` → fatal `CurationError` → frozen turn, no retry,
    no loud degrade, no `dungeon.curate.*` spans, no per-region
    `uncurated` marker). They pin the layered bounded contract: Layer 1
    one bounded whole-call retry → Layer 2 LOUD degrade-to-uncurated;
    `CurationError` retained only for the two carve-outs; per-region
    isolation; clause-12 routed spans.

    Test-pinned API (the observable contract RED proposes; Architect
    spec-check / Dev may rename at GREEN but the behaviour is fixed):
      * `RegionCuration.uncurated_regions: frozenset[str]` — region ids
        that Layer-2-degraded; empty on a fully-curated expansion.
      * `RegionCuration.curated` is the expansion-level rollup: False iff
        any region degraded (a degraded region must NOT be stamped True —
        the forbidden silent fallback).
      * Span names `dungeon.curate.parse_failed` (per attempt) and
        `dungeon.curate.degraded` (Layer 2 fired), both registered in
        `SPAN_ROUTES` (clause-12 GM-panel-visible / routed).
      * An injectable curate wall-clock cap so AC-4's deadline is
        verifiable without a 25 s test (proposed
        `materializer.CURATE_DEADLINE_S`).
    """

    def _spans_named(self, exporter: Any, name: str) -> list[Any]:
        return [s for s in exporter.get_finished_spans() if s.name == name]

    async def test_truncated_verdict_does_not_escape_stage_curate(self) -> None:
        """AC-2 / target (a): the pingpong unterminated-JSON verdict no
        longer propagates an unhandled `CurationError` that freezes the
        turn — `_stage_curate` RETURNS a `RegionCuration`."""
        import sidequest.dungeon.materializer as _mat
        from sidequest.dungeon.materializer import CurationError, RegionCuration
        from sidequest.telemetry.spans.dungeon_materialize import (
            dungeon_materialize_curate_span,
        )

        bundle = _real_cookbook_bundle()
        request, palette, expansion, fill_result, _look = _curate_inputs(
            algorithm="prim", expansion_id=1, depth_score=0.5
        )
        _exporter, original_tracer_fn, _spans_mod = _setup_otel_task3()
        try:
            with dungeon_materialize_curate_span(expansion_id=request.expansion_id) as span:
                try:
                    result = await _mat._stage_curate(
                        request,
                        bundle=bundle,
                        palette=palette,
                        expansion=expansion,
                        fill_result=fill_result,
                        is_first_band_entry=True,
                        claude_client=_truncating_sdk_client(),
                        span=span,
                    )
                except CurationError as exc:  # pragma: no cover - RED proof
                    pytest.fail(
                        "ADR-106 Amendment A: a truncated verdict must "
                        "Layer-2 degrade, not raise CurationError and "
                        f"freeze the turn. Got: {exc}"
                    )
        finally:
            _spans_mod.tracer = original_tracer_fn
        assert isinstance(result, RegionCuration)

    async def test_truncated_verdict_degrades_loud_curated_false_with_content(
        self,
    ) -> None:
        """target (b) + AC-3 + Forbidden invariant: the degraded region
        ships the deterministic assemble_region manifest, stamped
        `curated=False` and marked uncurated — NEVER stamped curated=True
        (the prior architect's correctly-rejected silent fallback)."""
        import sidequest.dungeon.materializer as _mat
        from sidequest.telemetry.spans.dungeon_materialize import (
            dungeon_materialize_curate_span,
        )

        bundle = _real_cookbook_bundle()
        request, palette, expansion, fill_result, _look = _curate_inputs(
            algorithm="prim", expansion_id=1, depth_score=0.5
        )
        rid = expansion.new_nodes[0].id
        _exporter, original_tracer_fn, _spans_mod = _setup_otel_task3()
        try:
            with dungeon_materialize_curate_span(expansion_id=request.expansion_id) as span:
                result = await _mat._stage_curate(
                    request,
                    bundle=bundle,
                    palette=palette,
                    expansion=expansion,
                    fill_result=fill_result,
                    is_first_band_entry=True,
                    claude_client=_truncating_sdk_client(),
                    span=span,
                )
        finally:
            _spans_mod.tracer = original_tracer_fn

        # Forbidden invariant: a degraded region is NEVER curated=True.
        assert result.curated is False
        assert result.curated is not True
        # Per-region uncurated marker (test-pinned API).
        assert rid in result.uncurated_regions
        # Layer 2 ships the deterministic assemble_region manifest as
        # content — the region is honest coal, not empty/garbage.
        assert rid in result.region_manifests
        assert result.region_manifests[rid].wandering_table, (
            "Layer-2 degrade must ship the pre-curation assemble_region "
            "manifest as content (ADR-106 clause 9), not an empty region"
        )

    async def test_degrade_emits_routed_dungeon_curate_degraded_span(self) -> None:
        """target (c) + AC-3 + clause 12: Layer 2 emits a routed
        `dungeon.curate.degraded` span carrying region_id, failure_kind,
        attempts, elapsed_ms — GM-panel-visible (the lie detector)."""
        import sidequest.dungeon.materializer as _mat
        from sidequest.telemetry.spans import SPAN_ROUTES
        from sidequest.telemetry.spans.dungeon_materialize import (
            dungeon_materialize_curate_span,
        )

        bundle = _real_cookbook_bundle()
        request, palette, expansion, fill_result, _look = _curate_inputs(
            algorithm="prim", expansion_id=1, depth_score=0.5
        )
        rid = expansion.new_nodes[0].id
        exporter, original_tracer_fn, _spans_mod = _setup_otel_task3()
        try:
            with dungeon_materialize_curate_span(expansion_id=request.expansion_id) as span:
                await _mat._stage_curate(
                    request,
                    bundle=bundle,
                    palette=palette,
                    expansion=expansion,
                    fill_result=fill_result,
                    is_first_band_entry=True,
                    claude_client=_truncating_sdk_client(),
                    span=span,
                )
        finally:
            _spans_mod.tracer = original_tracer_fn

        degraded = self._spans_named(exporter, "dungeon.curate.degraded")
        assert degraded, (
            "Layer 2 must emit a `dungeon.curate.degraded` span (clause-12 OTEL mandate)"
        )
        attrs = dict(degraded[0].attributes or {})
        assert attrs.get("region_id") == rid
        assert attrs.get("failure_kind") in {"truncated", "malformed"}
        assert int(attrs.get("attempts", 0)) >= 1
        assert "elapsed_ms" in attrs
        # Clause 12: the span must be ROUTED (GM-panel visible), not just
        # emitted into the void.
        assert "dungeon.curate.degraded" in SPAN_ROUTES, (
            "`dungeon.curate.degraded` must be registered in SPAN_ROUTES "
            "so the GM panel can render it (ADR-106 clause 12)"
        )

    async def test_retry_is_bounded_exactly_one_retry_then_degrade(self) -> None:
        """AC-4 + target (d): exactly 1 retry (2 attempts total), then
        deterministic Layer-2 degrade — provably non-looping. A
        `dungeon.curate.parse_failed` span fires per attempt."""
        import sidequest.dungeon.materializer as _mat
        from sidequest.telemetry.spans.dungeon_materialize import (
            dungeon_materialize_curate_span,
        )

        bundle = _real_cookbook_bundle()
        request, palette, expansion, fill_result, _look = _curate_inputs(
            algorithm="prim", expansion_id=1, depth_score=0.5
        )
        call_log: list[int] = []
        exporter, original_tracer_fn, _spans_mod = _setup_otel_task3()
        try:
            with dungeon_materialize_curate_span(expansion_id=request.expansion_id) as span:
                await _mat._stage_curate(
                    request,
                    bundle=bundle,
                    palette=palette,
                    expansion=expansion,
                    fill_result=fill_result,
                    is_first_band_entry=True,
                    claude_client=_truncating_sdk_client(call_log=call_log),
                    span=span,
                )
        finally:
            _spans_mod.tracer = original_tracer_fn

        assert len(call_log) == 2, (
            "Layer 1 budget is EXACTLY 1 retry (2 attempts total) — "
            f"got {len(call_log)} curate calls (no loop, no single-shot)"
        )
        parse_failed = self._spans_named(exporter, "dungeon.curate.parse_failed")
        assert len(parse_failed) == 2, (
            "one `dungeon.curate.parse_failed` span per attempt "
            f"(expected 2, got {len(parse_failed)})"
        )
        attempts = sorted(int(dict(s.attributes or {}).get("attempt", -1)) for s in parse_failed)
        assert attempts == [1, 2], f"parse_failed spans must tag attempt 1,2; got {attempts}"

    async def test_retry_recovers_no_degrade_when_second_attempt_valid(
        self,
    ) -> None:
        """Layer-1 RECOVERS: attempt 1 truncated, attempt 2 valid →
        curated=True, region NOT degraded, NO degraded span, exactly one
        parse_failed (attempt 1). Proves retry actually retries — not a
        vacuous always-degrade."""
        import sidequest.dungeon.materializer as _mat
        from sidequest.telemetry.spans.dungeon_materialize import (
            dungeon_materialize_curate_span,
        )

        bundle = _real_cookbook_bundle()
        request, palette, expansion, fill_result, _look = _curate_inputs(
            algorithm="prim", expansion_id=1, depth_score=0.5
        )
        rid = expansion.new_nodes[0].id
        call_log: list[int] = []
        exporter, original_tracer_fn, _spans_mod = _setup_otel_task3()
        try:
            with dungeon_materialize_curate_span(expansion_id=request.expansion_id) as span:
                result = await _mat._stage_curate(
                    request,
                    bundle=bundle,
                    palette=palette,
                    expansion=expansion,
                    fill_result=fill_result,
                    is_first_band_entry=True,
                    claude_client=_truncated_then_valid_sdk_client(call_log=call_log),
                    span=span,
                )
        finally:
            _spans_mod.tracer = original_tracer_fn

        assert len(call_log) == 2, "retry must fire the second attempt"
        assert result.curated is True, "a recovered retry is fully curated"
        assert rid not in result.uncurated_regions
        assert not self._spans_named(exporter, "dungeon.curate.degraded"), (
            "no degrade when the retry recovered"
        )
        assert len(self._spans_named(exporter, "dungeon.curate.parse_failed")) == 1, (
            "exactly one parse_failed (attempt 1) — attempt 2 succeeded"
        )

    async def test_per_region_isolation_one_bad_region_siblings_curated(
        self,
    ) -> None:
        """target (f): a verdict that parses but has ONE structurally
        broken region degrades only that region; the sibling stays
        curated; `_stage_curate` does NOT raise and returns content for
        BOTH (one bad region never aborts the expansion)."""
        import sidequest.dungeon.materializer as _mat
        from sidequest.telemetry.spans.dungeon_materialize import (
            dungeon_materialize_curate_span,
        )

        bundle = _real_cookbook_bundle()
        request, palette, expansion, fill_result = _curate_inputs_two_regions(
            algorithm="prim", expansion_id=9, depth_score=0.5
        )
        r0, r1 = (n.id for n in expansion.new_nodes)
        _exporter, original_tracer_fn, _spans_mod = _setup_otel_task3()
        try:
            with dungeon_materialize_curate_span(expansion_id=request.expansion_id) as span:
                result = await _mat._stage_curate(
                    request,
                    bundle=bundle,
                    palette=palette,
                    expansion=expansion,
                    fill_result=fill_result,
                    is_first_band_entry=True,
                    claude_client=_per_region_partial_sdk_client(),
                    span=span,
                )
        finally:
            _spans_mod.tracer = original_tracer_fn

        # r1 (last) is the broken one → degraded; r0 stays curated.
        assert r1 in result.uncurated_regions
        assert r0 not in result.uncurated_regions
        assert result.curated is False  # rollup: any degrade ⇒ False
        # BOTH regions still have content — the expansion was not aborted.
        assert r0 in result.region_manifests
        assert r1 in result.region_manifests
        assert result.region_manifests[r1].wandering_table, (
            "the degraded region still ships its assemble_region manifest"
        )

    async def test_wall_clock_cap_degrades_with_deadline_failure_kind(
        self,
    ) -> None:
        """AC-4 (wall-clock half): a curate call slower than the injected
        deadline degrades (failure_kind='deadline') and RETURNS quickly —
        the load-bearing no-multi-minute-freeze guarantee. Pins an
        injectable cap (proposed `materializer.CURATE_DEADLINE_S`)."""
        import time as _time

        import sidequest.dungeon.materializer as _mat
        from sidequest.telemetry.spans.dungeon_materialize import (
            dungeon_materialize_curate_span,
        )

        if not hasattr(_mat, "CURATE_DEADLINE_S"):
            pytest.fail(
                "AC-4 requires an injectable curate wall-clock cap "
                "(proposed `materializer.CURATE_DEADLINE_S`) so the "
                "deadline path is verifiable without a 25 s test"
            )
        bundle = _real_cookbook_bundle()
        request, palette, expansion, fill_result, _look = _curate_inputs(
            algorithm="prim", expansion_id=1, depth_score=0.5
        )
        rid = expansion.new_nodes[0].id
        exporter, original_tracer_fn, _spans_mod = _setup_otel_task3()
        original_deadline = _mat.CURATE_DEADLINE_S
        _mat.CURATE_DEADLINE_S = 0.05  # type: ignore[attr-defined]
        started = _time.monotonic()
        try:
            with dungeon_materialize_curate_span(expansion_id=request.expansion_id) as span:
                result = await _mat._stage_curate(
                    request,
                    bundle=bundle,
                    palette=palette,
                    expansion=expansion,
                    fill_result=fill_result,
                    is_first_band_entry=True,
                    claude_client=_slow_then_valid_sdk_client(2.0),
                    span=span,
                )
        finally:
            _mat.CURATE_DEADLINE_S = original_deadline  # type: ignore[attr-defined]
            _spans_mod.tracer = original_tracer_fn

        elapsed = _time.monotonic() - started
        assert elapsed < 1.5, (
            f"the wall-clock cap must abort the slow curate fast "
            f"(no multi-minute freeze); took {elapsed:.2f}s"
        )
        assert result.curated is False
        assert rid in result.uncurated_regions
        degraded = self._spans_named(exporter, "dungeon.curate.degraded")
        assert degraded, "deadline path must Layer-2 degrade loudly"
        assert dict(degraded[0].attributes or {}).get("failure_kind") == "deadline"

    async def test_missing_cr_still_raises_curation_error_retained_carveout(
        self,
    ) -> None:
        """RETAINED `CurationError` carve-out (ii) — REGRESSION GUARD
        (may already be green on develop; MUST stay green after GREEN):
        a parseable verdict whose kept row dropped `cr` would corrupt the
        CR→Edge seam, so it MUST still raise — degrading here is
        forbidden over-degradation."""
        import sidequest.dungeon.materializer as _mat
        from sidequest.dungeon.materializer import CurationError
        from sidequest.telemetry.spans.dungeon_materialize import (
            dungeon_materialize_curate_span,
        )

        bundle = _real_cookbook_bundle()
        request, palette, expansion, fill_result, _look = _curate_inputs(
            algorithm="prim", expansion_id=1, depth_score=0.5
        )
        _exporter, original_tracer_fn, _spans_mod = _setup_otel_task3()
        try:
            with (
                pytest.raises(CurationError),
                dungeon_materialize_curate_span(expansion_id=request.expansion_id) as span,
            ):
                await _mat._stage_curate(
                    request,
                    bundle=bundle,
                    palette=palette,
                    expansion=expansion,
                    fill_result=fill_result,
                    is_first_band_entry=True,
                    claude_client=_missing_cr_sdk_client(),
                    span=span,
                )
        finally:
            _spans_mod.tracer = original_tracer_fn

    async def test_degrade_logs_at_error_level_not_swallowed_silently(self, caplog: Any) -> None:
        """RULE ENFORCEMENT — lang-review/python.md #1 (silent exception
        swallowing) + #42 (error paths MUST log error/warning) + CLAUDE.md
        No-Silent-Fallbacks. Layer 2 is an error path: it MUST emit an
        ERROR-level log, not a silent `except: pass`. Catches a Dev who
        degrades quietly."""
        import logging

        import sidequest.dungeon.materializer as _mat
        from sidequest.telemetry.spans.dungeon_materialize import (
            dungeon_materialize_curate_span,
        )

        bundle = _real_cookbook_bundle()
        request, palette, expansion, fill_result, _look = _curate_inputs(
            algorithm="prim", expansion_id=1, depth_score=0.5
        )
        _exporter, original_tracer_fn, _spans_mod = _setup_otel_task3()
        try:
            with (
                caplog.at_level(logging.ERROR),
                dungeon_materialize_curate_span(expansion_id=request.expansion_id) as span,
            ):
                await _mat._stage_curate(
                    request,
                    bundle=bundle,
                    palette=palette,
                    expansion=expansion,
                    fill_result=fill_result,
                    is_first_band_entry=True,
                    claude_client=_truncating_sdk_client(),
                    span=span,
                )
        finally:
            _spans_mod.tracer = original_tracer_fn

        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert error_records, (
            "Layer-2 degrade MUST log at ERROR (No-Silent-Fallbacks / "
            "lang-review #1+#42) — a silent degrade is the exact trap "
            "this story exists to close"
        )
        assert any(
            "curat" in r.getMessage().lower() or "degrad" in r.getMessage().lower()
            for r in error_records
        ), "the ERROR log must name the curate degrade, not be generic noise"

    async def test_truncated_verdict_completes_through_real_materialize_chain(
        self, monkeypatch: Any, migrated_db: str
    ) -> None:
        """AC-5 + target (e) — MANDATORY WIRING TEST (CLAUDE.md): the
        robustness path is reachable from the real
        `materialize → _stage_curate → _parse_curation_verdict` chain. A
        truncating curator no longer aborts the turn — `materialize`
        completes and the expansion commits (the turn proceeds)."""
        import sidequest.telemetry.spans as _spans_module
        from sidequest.dungeon.materializer import materialize
        from sidequest.dungeon.region_graph import RegionNode
        from tests.dungeon.conftest import build_pg_dungeon_repo

        bundle = _real_cookbook_bundle()
        request, palette, _expansion, _fill, _look = _curate_inputs(
            algorithm="prim", expansion_id=1, depth_score=0.5
        )
        theme_id = next(iter(palette.themes))
        graph = _make_seed_graph("entrance")
        graph.nodes["entrance"] = RegionNode(id="entrance", expansion_id=0, theme=theme_id)

        _pool, repo, _sid = build_pg_dungeon_repo(monkeypatch, migrated_db)
        exporter, _provider, real_tracer = _otel_in_memory()
        original_tracer_fn = _spans_module.tracer
        _spans_module.tracer = lambda: real_tracer  # type: ignore[method-assign]
        try:
            await materialize(
                request,
                graph=graph,
                bundle=bundle,
                palette=palette,
                dungeon_repository=repo,
                snapshot=_fresh_snapshot(),
                pack_tropes=_attach_pack("cave_in"),
                claude_client=_truncating_sdk_client(),
            )
        finally:
            _spans_module.tracer = original_tracer_fn  # type: ignore[method-assign]

        # The turn proceeded: the expansion committed despite the
        # truncating curator (no frozen turn, no aborted materialize).
        assert "entrance" in repo.load_map(entrance_id="entrance").nodes
        assert [s for s in exporter.get_finished_spans() if s.name == "dungeon.curate.degraded"], (
            "the degrade must be observable through the real chain too"
        )


# ===========================================================================
# Task 5: Stage 4 attach — attach_expansion + assign_depth_scores + Plan 6
#         set-piece/trope/quest seam
# ===========================================================================
#
# The 3 plan bullets:
#  1. the `attach` span attributes equal DepthReport.as_dict() exactly;
#     entrance region depth is 0.0; a pre-scored region is NOT recomputed.
#  2. (binds to Plan 6's merged API) started trope/quest threads land in the
#     open-complication ledger with origin region + status; thread count
#     scales with burst_magnitude.
#  3. attach_expansion's loud global-invariant failure aborts the whole
#     materialization (no partial commit).


def _theme_with_set_piece(
    theme_id: str,
    *,
    trope_ids: tuple[str, ...] = ("cave_in",),
    quest_ids: tuple[str, ...] = ("deny_the_altar",),
) -> Any:
    """A real DungeonTheme (cellular/organic) carrying ONE real SetPiece
    with trope + quest components. No mocking of the dungeon layer — real
    pydantic value objects only."""
    from sidequest.dungeon.setpieces import SetPiece
    from sidequest.dungeon.themes import (
        Adjacency,
        DepthBand,
        DungeonTheme,
        InteriorSpec,
        NarratorFlavor,
    )

    sp = SetPiece.model_validate(
        {
            "id": f"{theme_id}_altar",
            "name": "The Sünden Altar",
            "telegraph": "A black altar slick with old blood.",
            "outcome": "The ceiling groans and the dark answers.",
            "slots": [{"name": "layout", "options": [{"value": "pit", "weight": 1.0}]}],
            "trope_components": [{"trope_id": t, "params": {}} for t in trope_ids],
            "quest_components": [{"quest_id": q, "params": {}} for q in quest_ids],
        }
    )
    return DungeonTheme(
        id=theme_id,
        display_name=theme_id.replace("_", " ").title(),
        generator_class="organic",
        interior=InteriorSpec(algorithm="cellular", braid_ratio=0.0),
        depth_band=DepthBand(min=0.0, max=None),
        narrator=NarratorFlavor(register="grave", flavor="dread whispers"),
        adjacency=Adjacency(),
        set_pieces=[sp],
    )


def _expansion_off_seed(
    *, theme_id: str, expansion_id: int = 1, entrance_id: str = "entrance"
) -> Any:
    """Two new regions hung off the entrance with a loop edge (so the
    attached graph is connected AND loopful — attach_expansion's two
    global invariants both hold)."""
    from sidequest.dungeon.region_graph import Expansion, RegionEdge, RegionNode

    r0 = f"exp{expansion_id:03d}.r0"
    r1 = f"exp{expansion_id:03d}.r1"
    nodes = [
        RegionNode(id=r0, expansion_id=expansion_id, theme=theme_id),
        RegionNode(id=r1, expansion_id=expansion_id, theme=theme_id),
    ]
    edges = [
        RegionEdge(a=entrance_id, b=r0, kind="corridor"),
        RegionEdge(a=r0, b=r1, kind="corridor"),
        RegionEdge(a=r1, b=entrance_id, kind="stairs"),  # closes a loop
    ]
    return Expansion(expansion_id=expansion_id, new_nodes=nodes, new_edges=edges)


def _manifest_for(region_id: str) -> Any:
    """A real RegionContentManifest (reduced Task 3 does not resolve refs
    against it; attach_set_piece accepts it unchanged)."""
    from sidequest.game.cookbook.models import RegionContentManifest

    return RegionContentManifest(
        race="dwarf",
        cr_band="mid",
        size_budget={"rooms": 6},
        wandering_table=[{"name": "Zombie", "cr": 0.25, "weight": 3, "count": "1d4"}],
        loot_table=[{"name": "Grave Silver", "item_type": "treasure"}],
        special_rooms=[],
        big_bad=None,
    )


def _curation_for(expansion: Any) -> Any:
    """A real RegionCuration whose region_manifests cover every region in
    the expansion (the curate→attach thread the coordinator carries)."""
    from sidequest.dungeon.materializer import RegionCuration

    rids = [n.id for n in expansion.new_nodes]
    return RegionCuration(
        region_manifests={rid: _manifest_for(rid) for rid in rids},
        region_creatures={rid: [] for rid in rids},
        region_big_bad={rid: None for rid in rids},
        region_look={rid: "delvehold" for rid in rids},
        curated=True,
        raw_seed_reproducible=False,
    )


def _attach_pack(*trope_ids: str) -> Any:
    """Pack-shaped object carrying .tropes (duck type attach_set_piece /
    start_trope_components use)."""
    from types import SimpleNamespace

    return SimpleNamespace(tropes=[SimpleNamespace(id=t) for t in trope_ids])


def _fresh_snapshot() -> Any:
    """Minimal GameSnapshot with empty active_tropes (mirrors
    tests/dungeon/test_setpiece_attach.py::_fresh_snapshot)."""
    from sidequest.game.session import GameSnapshot

    return GameSnapshot(genre_slug="caverns_and_claudes", world_slug="test_world")


# ---------------------------------------------------------------------------
# Task 5 Test 1: attach span == DepthReport.as_dict() exactly; entrance 0.0;
#                pre-scored region NOT recomputed (freeze).
# ---------------------------------------------------------------------------


class TestStageAttach:
    async def test_attach_span_equals_depth_report_and_freeze_holds(self) -> None:
        import dataclasses

        import sidequest.dungeon.materializer as _mat
        import sidequest.telemetry.spans as _spans_module
        from sidequest.dungeon.region_graph import RegionGraph, RegionNode
        from sidequest.dungeon.themes import ThemePalette
        from sidequest.telemetry.spans import SPAN_ROUTES
        from sidequest.telemetry.spans.dungeon_materialize import (
            SPAN_DUNGEON_MATERIALIZE_ATTACH,
            dungeon_materialize_attach_span,
        )

        theme_id = "sunken_crypt"
        palette = ThemePalette(themes={theme_id: _theme_with_set_piece(theme_id)})

        # Seed graph: the entrance is UNSCORED (depth_score=None) so
        # assign_depth_scores will score it to exactly 0.0; plus a
        # pre-existing PROVISIONED region with a frozen depth_score that
        # must NOT be recomputed.
        graph = RegionGraph(entrance_id="entrance")
        graph.add_node(RegionNode(id="entrance", expansion_id=0, theme=theme_id))
        graph.add_node(
            RegionNode(
                id="frozen_old",
                expansion_id=0,
                theme=theme_id,
                depth_score=999.0,
            )
        )
        # frozen_old must be reachable on the ordinary route, else
        # assign_depth_scores raises before it can freeze — connect it.
        from sidequest.dungeon.region_graph import RegionEdge

        graph.add_edge(RegionEdge(a="entrance", b="frozen_old", kind="corridor"))

        expansion = _expansion_off_seed(theme_id=theme_id, expansion_id=1)
        curation = _curation_for(expansion)
        snapshot = _fresh_snapshot()
        pack = _attach_pack("cave_in")

        conn = _mem_conn()
        from sidequest.dungeon.persistence import DungeonStore

        store = DungeonStore(conn)
        store.ensure_schema()

        request = MaterializationRequest_build(
            campaign_seed=7, expansion_id=1, spawn_depth_score=0.0
        )

        exporter, _provider, real_tracer = _otel_in_memory()
        original_tracer_fn = _spans_module.tracer
        _spans_module.tracer = lambda: real_tracer  # type: ignore[method-assign]
        try:
            with dungeon_materialize_attach_span(expansion_id=request.expansion_id) as span:
                result = _mat._stage_attach(
                    request,
                    graph=graph,
                    expansion=expansion,
                    palette=palette,
                    curation=curation,
                    snapshot=snapshot,
                    pack_tropes=pack,
                    dungeon_repository=store,
                    span=span,
                )
        finally:
            _spans_module.tracer = original_tracer_fn  # type: ignore[method-assign]

        # The DepthReport carried out for Task 6.
        from sidequest.dungeon.region_graph import DepthReport

        assert isinstance(result.depth_report, DepthReport)
        report_dict = result.depth_report.as_dict()
        assert set(report_dict.keys()) == {
            "regions_scored",
            "depth_min",
            "depth_max",
            "depth_mean",
        }

        # The attach span's STAGE-WRITTEN attributes == DepthReport.as_dict()
        # EXACTLY (byte-pinned). The attach span helper pre-bakes only the
        # `expansion_id` pipeline scaffold (same deliberate choice as the
        # design span helper, which omits `stage` so the stage owns the
        # exact attribute set); the stage writes EXACTLY the 4 DepthReport
        # keys on success — no `stage` attr, no extra keys.
        finished = exporter.get_finished_spans()
        attach_spans = [s for s in finished if s.name == SPAN_DUNGEON_MATERIALIZE_ATTACH]
        assert attach_spans, "dungeon.materialize.attach span not emitted"
        span_attrs = dict(attach_spans[0].attributes or {})
        stage_written = {k: v for k, v in span_attrs.items() if k != "expansion_id"}
        assert set(stage_written.keys()) == set(report_dict.keys()), (
            f"attach span stage-written key-set mismatch (must be EXACTLY "
            f"DepthReport.as_dict()).\n"
            f"  Stage-written keys: {sorted(stage_written)}\n"
            f"  DepthReport keys: {sorted(report_dict)}"
        )
        for k, v in report_dict.items():
            assert span_attrs[k] == v, f"span[{k!r}]={span_attrs[k]!r}, expected {v!r}"

        # The routed extract surfaces the 4 DepthReport keys + a null
        # failure marker on the success path (graceful-get idiom).
        route = SPAN_ROUTES[SPAN_DUNGEON_MATERIALIZE_ATTACH]
        fields = route.extract(attach_spans[0])  # type: ignore[arg-type]
        for k in report_dict:
            assert fields[k] == report_dict[k]
        assert fields.get("error") is None
        assert fields.get("reason") is None

        # Entrance region depth is EXACTLY 0.0 (no jitter at the origin).
        assert graph.nodes["entrance"].depth_score == 0.0

        # FREEZE: the pre-scored region was NOT recomputed.
        assert graph.nodes["frozen_old"].depth_score == 999.0

        # Sanity: only the two new regions + entrance were scored
        # (frozen_old was already scored, so it is excluded).
        assert result.depth_report.regions_scored == 3

        # report.rolled carried for Task 6 (freeze target — never recomputed).
        assert result.attach_reports
        for ar in result.attach_reports:
            assert ar.rolled is not None
            assert isinstance(dataclasses.asdict(ar.rolled), dict)

    # -----------------------------------------------------------------------
    # Task 5 Test 2: threads land in the REAL DungeonStore with origin
    #                region + open status; count scales with burst_magnitude.
    # -----------------------------------------------------------------------

    def _run_attach(
        self,
        *,
        burst_magnitude: int,
        trope_ids: tuple[str, ...],
        quest_ids: tuple[str, ...],
    ) -> tuple[Any, Any]:
        """Run _stage_attach end-to-end against a real DungeonStore and
        return (store, AttachResult). burst_magnitude == the
        threads_lit_per_expansion budget the attach stage passes through."""
        import sidequest.dungeon.materializer as _mat
        import sidequest.telemetry.spans as _spans_module
        from sidequest.dungeon.persistence import DungeonStore
        from sidequest.dungeon.themes import ThemePalette
        from sidequest.telemetry.spans.dungeon_materialize import (
            dungeon_materialize_attach_span,
        )

        theme_id = "deep_ossuary"
        palette = ThemePalette(
            themes={
                theme_id: _theme_with_set_piece(theme_id, trope_ids=trope_ids, quest_ids=quest_ids)
            }
        )
        graph = _make_seed_graph("entrance")
        # Re-theme the entrance so palette.themes[node.theme] resolves for
        # any pre-existing node (the seed entrance theme is "tomb").
        from sidequest.dungeon.region_graph import RegionNode

        graph.nodes["entrance"] = RegionNode(id="entrance", expansion_id=0, theme=theme_id)
        expansion = _expansion_off_seed(theme_id=theme_id, expansion_id=1)
        curation = _curation_for(expansion)
        snapshot = _fresh_snapshot()
        pack = _attach_pack(*trope_ids)

        conn = _mem_conn()
        store = DungeonStore(conn)
        store.ensure_schema()

        request = MaterializationRequest_build(
            campaign_seed=7,
            expansion_id=1,
            spawn_depth_score=0.0,
        )
        # Override burst_magnitude (the attach stage passes it through as
        # threads_lit_per_expansion). Rebuild via dataclasses.replace since
        # MaterializationRequest is frozen.
        import dataclasses

        request = dataclasses.replace(request, burst_magnitude=burst_magnitude)

        exporter, _provider, real_tracer = _otel_in_memory()
        original_tracer_fn = _spans_module.tracer
        _spans_module.tracer = lambda: real_tracer  # type: ignore[method-assign]
        try:
            with dungeon_materialize_attach_span(expansion_id=request.expansion_id) as span:
                result = _mat._stage_attach(
                    request,
                    graph=graph,
                    expansion=expansion,
                    palette=palette,
                    curation=curation,
                    snapshot=snapshot,
                    pack_tropes=pack,
                    dungeon_repository=store,
                    span=span,
                )
        finally:
            _spans_module.tracer = original_tracer_fn  # type: ignore[method-assign]
        return store, result

    async def test_threads_in_real_store_with_origin_and_status(self) -> None:
        store, result = self._run_attach(
            burst_magnitude=10,
            trope_ids=("cave_in", "dripping_water"),
            quest_ids=("deny_the_altar",),
        )

        open_threads = store.open_threads()
        assert open_threads, "no open complication threads written to the store"
        new_region_ids = {"exp001.r0", "exp001.r1"}
        for thread in open_threads:
            assert thread.status == "open"
            assert thread.origin_region_id in new_region_ids
            assert thread.kind in ("trope", "quest")
            # started_at_depth_score frozen from the post-depth-scoring graph
            # (REQUIRED, no default).
            assert thread.started_at_depth_score is not None

        # report.threads_written sums to the persisted open-thread count
        # (lie detector: spans/reports vs ledger rows).
        total_written = sum(r.threads_written for r in result.attach_reports)
        assert total_written == len(open_threads)

    async def test_thread_count_scales_with_burst_magnitude(self) -> None:
        """Raising burst_magnitude (→ threads_lit_per_expansion) yields MORE
        started threads, up to the available components. Binds the real
        Plan 6 attach_set_piece budget API."""
        # 3 trope + 3 quest components per set-piece, 2 regions → up to 12
        # threads available; budget 1 caps low, budget 100 lets them all in.
        tropes = ("cave_in", "dripping_water", "ghost_light")
        quests = ("deny_the_altar", "find_the_relic", "free_the_captive")

        store_low, _ = self._run_attach(burst_magnitude=1, trope_ids=tropes, quest_ids=quests)
        store_high, _ = self._run_attach(burst_magnitude=100, trope_ids=tropes, quest_ids=quests)

        low_count = len(store_low.open_threads())
        high_count = len(store_high.open_threads())
        assert high_count > low_count, (
            f"thread count must scale with burst_magnitude: "
            f"burst=1 → {low_count} threads, burst=100 → {high_count}"
        )

    async def test_thread_budget_caps_cumulatively_across_regions(self) -> None:
        """The threads_lit_per_expansion budget is EXPANSION-level, not
        per-region: it accumulates across ALL regions/set-pieces in the
        whole expansion (threads_already_lit initialised ONCE before the
        region loop, never reset per region).

        2 regions, each set-piece has tropes=("a","b") quests=() → 2
        components/set-piece × 2 regions = 4 components available; budget 3
        (burst_magnitude=3 → threads_lit_per_expansion=3). Correct
        cumulative accumulation caps at EXACTLY 3 (region0 consumes 2 of 3,
        region1 gets only the remaining 1). A per-region budget reset (the
        exact silent budget violation) would wrongly yield 4 (region1
        restarts the full budget). The `== 3` is the load-bearing pin."""
        store, _result = self._run_attach(
            burst_magnitude=3,
            trope_ids=("a", "b"),
            quest_ids=(),
        )

        open_threads = store.open_threads()
        assert len(open_threads) == 3, (
            f"expansion-level budget must cap CUMULATIVELY across regions: "
            f"4 components available, budget 3 → EXACTLY 3 threads "
            f"(region0 takes 2, region1 takes the remaining 1). Got "
            f"{len(open_threads)} — a count of 4 means the accumulator was "
            f"reset per-region (silent budget violation)."
        )

    # -----------------------------------------------------------------------
    # Task 5 Test 3: attach_expansion's loud global-invariant failure aborts
    #                the WHOLE materialization — no depth scoring, no
    #                set-piece attach, span carries the routed failure
    #                marker, NO partial state.
    # -----------------------------------------------------------------------

    async def test_attach_expansion_invariant_failure_aborts_with_no_partial_state(
        self,
    ) -> None:
        import sidequest.dungeon.materializer as _mat
        import sidequest.telemetry.spans as _spans_module
        from sidequest.dungeon.persistence import DungeonStore
        from sidequest.dungeon.region_graph import (
            Expansion,
            RegionGraph,
            RegionNode,
        )
        from sidequest.dungeon.themes import ThemePalette
        from sidequest.telemetry.spans import SPAN_ROUTES
        from sidequest.telemetry.spans.dungeon_materialize import (
            SPAN_DUNGEON_MATERIALIZE_ATTACH,
            dungeon_materialize_attach_span,
        )

        theme_id = "broken_vault"
        palette = ThemePalette(themes={theme_id: _theme_with_set_piece(theme_id)})

        # Seed graph: just the entrance (UNSCORED).
        graph = RegionGraph(entrance_id="entrance")
        graph.add_node(RegionNode(id="entrance", expansion_id=0, theme=theme_id))

        # A degenerate expansion: a new region with NO edge connecting it
        # to the explored graph → attach_expansion's global "connected"
        # invariant raises loudly (No Silent Fallbacks).
        orphan = RegionNode(id="exp001.r0", expansion_id=1, theme=theme_id)
        bad_expansion = Expansion(expansion_id=1, new_nodes=[orphan], new_edges=[])
        curation = _curation_for(bad_expansion)
        snapshot = _fresh_snapshot()
        pack = _attach_pack("cave_in")

        conn = _mem_conn()
        store = DungeonStore(conn)
        store.ensure_schema()

        request = MaterializationRequest_build(
            campaign_seed=7, expansion_id=1, spawn_depth_score=0.0
        )

        exporter, _provider, real_tracer = _otel_in_memory()
        original_tracer_fn = _spans_module.tracer
        _spans_module.tracer = lambda: real_tracer  # type: ignore[method-assign]
        try:
            # Loud global-invariant raise — NOT swallowed, NOT
            # NotImplementedError. (span + pytest.raises combined into one
            # `with` — the Task-2 precedent idiom at
            # test_design_stage_propagates_expansion_generation_error_with_span.)
            with (
                pytest.raises(ValueError, match="disconnected") as exc_info,
                dungeon_materialize_attach_span(expansion_id=request.expansion_id) as span,
            ):
                _mat._stage_attach(
                    request,
                    graph=graph,
                    expansion=bad_expansion,
                    palette=palette,
                    curation=curation,
                    snapshot=snapshot,
                    pack_tropes=pack,
                    dungeon_repository=store,
                    span=span,
                )
        finally:
            _spans_module.tracer = original_tracer_fn  # type: ignore[method-assign]

        assert not isinstance(exc_info.value, NotImplementedError)

        # NO depth scoring ran: the orphan node (even though add_node ran
        # inside attach_expansion before the connected check) has NO
        # depth_score, and the entrance is still UNSCORED.
        assert graph.nodes["entrance"].depth_score is None
        assert graph.nodes["exp001.r0"].depth_score is None

        # NO set-piece attach ran: zero ledger rows written (No partial
        # state — abort BEFORE any thread write).
        assert store.open_threads() == []

        # The span carries the ROUTED failure marker (lie-detector
        # visibility — the GM panel must see the abort, the Task-2 lesson).
        finished = exporter.get_finished_spans()
        attach_spans = [s for s in finished if s.name == SPAN_DUNGEON_MATERIALIZE_ATTACH]
        assert attach_spans, "attach span not emitted on the failure path"
        route = SPAN_ROUTES[SPAN_DUNGEON_MATERIALIZE_ATTACH]
        fields = route.extract(attach_spans[0])  # type: ignore[arg-type]
        assert fields.get("error") is not None
        assert fields.get("reason") is not None
        assert "attach_expansion" in str(fields["reason"])
        # The byte-pinned DepthReport keys were NEVER written (abort before
        # assign_depth_scores) — they read None via the graceful-get idiom.
        assert fields.get("regions_scored") is None
        assert fields.get("depth_min") is None

    # -----------------------------------------------------------------------
    # Task 5 wiring: the coordinator threads _stage_attach's AttachResult
    # into _stage_commit (spec §7 freeze target — Task 6 must persist
    # attach_reports[].rolled and NEVER recompute it). FAILS if the
    # coordinator discards the AttachResult (the pre-fix state).
    # -----------------------------------------------------------------------

    async def test_coordinator_threads_attach_result_into_commit(self) -> None:
        """materialize() must pass the SAME AttachResult instance
        _stage_attach returned into _stage_commit as ``attach_result`` —
        carrying attach_reports whose entries have ``.rolled`` (the spec §7
        freeze target Task 6 persists, never recomputed). Decisive: this
        fails if the coordinator drops the attach return on the floor.

        Non-brittle for Task 6: asserts ONLY the threaded object identity +
        ``.rolled`` presence, never commit internals (which don't exist
        until Task 6)."""
        import sidequest.dungeon.materializer as _mat_module
        import sidequest.telemetry.spans as _spans_module
        from sidequest.dungeon.materializer import AttachResult, materialize
        from sidequest.dungeon.persistence import DungeonStore
        from sidequest.dungeon.region_graph import RegionNode
        from sidequest.dungeon.themes import ThemePalette

        # A palette whose single set-piece-bearing theme is eligible at the
        # frontier depth, bound to a real cookbook look so curate's
        # look-resolution + assemble_region pass. `cellular` → look
        # `hollowing` in the real beneath_sunden looks.yaml.
        theme_id = "wired_crypt"
        base_theme = _theme_with_set_piece(theme_id)
        palette = ThemePalette(themes={theme_id: base_theme})

        graph = _make_seed_graph("entrance")
        # Re-theme the seed entrance so palette.themes[node.theme] resolves
        # for the pre-existing node too (seed entrance theme is "tomb").
        graph.nodes["entrance"] = RegionNode(id="entrance", expansion_id=0, theme=theme_id)

        bundle = _real_cookbook_bundle()
        snapshot = _fresh_snapshot()
        pack = _attach_pack("cave_in")
        request = MaterializationRequest_build(
            campaign_seed=7, expansion_id=1, spawn_depth_score=0.0
        )

        conn = _mem_conn()
        store = DungeonStore(conn)
        store.ensure_schema()

        # Capture the REAL AttachResult _stage_attach produces (wrap the
        # real stage — do NOT stub it; we want the genuine freeze targets).
        real_stage_attach = _mat_module._stage_attach
        original_commit = _mat_module._stage_commit
        captured: dict[str, Any] = {}

        def _wrapped_attach(*args: Any, **kwargs: Any) -> Any:
            result = real_stage_attach(*args, **kwargs)
            captured["attach_result"] = result
            return result

        # Capture _stage_commit's kwargs and DO NOT raise (so the
        # coordinator runs to completion and we can assert the thread).
        def _capturing_commit(*args: Any, **kwargs: Any) -> None:
            captured["commit_kwargs"] = kwargs

        _mat_module._stage_attach = _wrapped_attach  # type: ignore[assignment]
        _mat_module._stage_commit = _capturing_commit  # type: ignore[assignment]

        _exporter, _provider, real_tracer = _otel_in_memory()
        original_tracer_fn = _spans_module.tracer
        _spans_module.tracer = lambda: real_tracer  # type: ignore[method-assign]
        try:
            await materialize(
                request,
                graph=graph,
                bundle=bundle,
                palette=palette,
                dungeon_repository=store,
                snapshot=snapshot,
                pack_tropes=pack,
                claude_client=_reflecting_sdk_client(),
            )
        finally:
            _mat_module._stage_attach = real_stage_attach  # type: ignore[assignment]
            _mat_module._stage_commit = original_commit  # type: ignore[assignment]
            _spans_module.tracer = original_tracer_fn  # type: ignore[method-assign]

        # _stage_attach genuinely ran and produced a real AttachResult with
        # the spec §7 freeze targets.
        assert "attach_result" in captured, "_stage_attach was never called"
        produced = captured["attach_result"]
        assert isinstance(produced, AttachResult)
        assert produced.attach_reports, (
            "attach produced no AttachReports — the set-piece-bearing theme "
            "must yield at least one (rolled freeze target)"
        )
        for ar in produced.attach_reports:
            assert ar.rolled is not None

        # THE WIRING ASSERTION: _stage_commit received the SAME AttachResult
        # instance (identity) as ``attach_result``. Fails if the coordinator
        # discards _stage_attach's return (pre-fix state).
        assert "commit_kwargs" in captured, "_stage_commit was never reached"
        commit_kwargs = captured["commit_kwargs"]
        assert "attach_result" in commit_kwargs, (
            "_stage_commit did NOT receive attach_result — the coordinator "
            "discarded the AttachResult (spec §7 freeze target lost; Task 6 "
            "would be forced to re-roll, violating save-is-truth)"
        )
        assert commit_kwargs["attach_result"] is produced, (
            "_stage_commit received a DIFFERENT object than _stage_attach "
            "produced — the freeze targets must be threaded by identity"
        )


def MaterializationRequest_build(
    *, campaign_seed: int, expansion_id: int, spawn_depth_score: float
) -> Any:
    """Build a MaterializationRequest whose frontier_edge.spawn_depth_score
    is `spawn_depth_score` (entrance-at-0.0 Seed=Expansion-0 contract)."""
    from sidequest.dungeon.materializer import MaterializationRequest
    from sidequest.dungeon.persistence import FrontierEdge

    fe = FrontierEdge(
        frontier_edge_id="fe1",
        from_region_id="entrance",
        heading="north",
        spawn_depth_score=spawn_depth_score,
    )
    return MaterializationRequest.build(
        campaign_seed=campaign_seed,
        expansion_id=expansion_id,
        frontier_edge=fe,
        frontier=[fe],
        attach_region_ids=["entrance"],
        heading="north",
        burst_magnitude=3,
        lookahead_breadth=2,
    )


# ---------------------------------------------------------------------------
# Task 6: Stage 5 commit — one-txn seed+expansion+frontier; atomic rollback;
#         generator-version freeze. Real DungeonStore on a real connection;
#         real design/fill/curate/attach upstream (the production coordinator).
# ---------------------------------------------------------------------------


def _commit_palette(theme_id: str) -> Any:
    """A real ThemePalette whose single set-piece-bearing theme is eligible
    at depth 0.0 (cellular → real beneath_sunden look). Reuses Task 5's
    real DungeonTheme builder — no mocking of the dungeon layer."""
    return ThemePalette(themes={theme_id: _theme_with_set_piece(theme_id)})


def _seed_graph_themed(theme_id: str) -> Any:
    """A seed graph (entrance only) re-themed so palette.themes[node.theme]
    resolves for the pre-existing entrance node too."""
    from sidequest.dungeon.region_graph import RegionNode

    graph = _make_seed_graph("entrance")
    graph.nodes["entrance"] = RegionNode(id="entrance", expansion_id=0, theme=theme_id)
    return graph


async def _materialize_full(
    *,
    graph: Any,
    palette: Any,
    dungeon_repository: Any,
    campaign_seed: int = 7,
    expansion_id: int = 1,
) -> Any:
    """Drive the REAL five-stage coordinator (design->fill->curate->attach->
    commit) against a real DungeonRepository. Returns the GameSnapshot used
    (so callers can inspect promote-to-active state)."""
    import sidequest.telemetry.spans as _spans_module
    from sidequest.dungeon.materializer import materialize

    bundle = _real_cookbook_bundle()
    snapshot = _fresh_snapshot()
    pack = _attach_pack("cave_in")
    request = MaterializationRequest_build(
        campaign_seed=campaign_seed,
        expansion_id=expansion_id,
        spawn_depth_score=0.0,
    )

    _exporter, _provider, real_tracer = _otel_in_memory()
    original_tracer_fn = _spans_module.tracer
    _spans_module.tracer = lambda: real_tracer  # type: ignore[method-assign]
    try:
        await materialize(
            request,
            graph=graph,
            bundle=bundle,
            palette=palette,
            dungeon_repository=dungeon_repository,
            snapshot=snapshot,
            pack_tropes=pack,
            claude_client=_reflecting_sdk_client(),
        )
    finally:
        _spans_module.tracer = original_tracer_fn  # type: ignore[method-assign]
    return snapshot


class TestStageCommit:
    async def test_fresh_save_seeds_expansion_zero_then_commits_expansion(
        self, monkeypatch: Any, migrated_db: str
    ) -> None:
        """A fresh save -> the commit stage persists the surface entrance as
        Expansion 0 (entrance belongs to no Expansion.new_nodes -- the
        Seed=Expansion-0 contract) THEN the generated expansion, in ONE
        transaction. load_map round-trips entrance + expansion; the new
        unexpanded frontier edges are persisted."""
        from tests.dungeon.conftest import build_pg_dungeon_repo

        theme_id = "commit_crypt"
        palette = _commit_palette(theme_id)
        graph = _seed_graph_themed(theme_id)

        _pool, repo, _sid = build_pg_dungeon_repo(monkeypatch, migrated_db)

        # Fresh save: nothing committed yet.
        assert repo.load_map(entrance_id="entrance").nodes == {}
        assert repo.load_frontier() == []

        await _materialize_full(graph=graph, palette=palette, dungeon_repository=repo)

        # The entrance (expansion_id=0) AND the generated expansion's
        # regions are now live (commit is immediately live on success).
        reloaded = repo.load_map(entrance_id="entrance")
        assert "entrance" in reloaded.nodes, (
            "entrance not persisted -- the Seed=Expansion-0 commit did not "
            "run (commit_expansion only persists expansion.new_nodes; the "
            "entrance must be committed as Expansion 0)"
        )
        assert reloaded.nodes["entrance"].expansion_id == 0
        assert reloaded.nodes["entrance"].depth_score == 0.0
        # Generated expansion regions present (expansion_id == 1).
        gen_regions = [n for n in reloaded.nodes.values() if n.expansion_id == 1]
        assert gen_regions, "generated expansion regions not persisted"

        # New unexpanded frontier edges were derived from the attached
        # expansion and persisted within the same txn.
        frontier = repo.load_frontier()
        assert frontier, (
            "no new unexpanded frontier edges persisted -- the commit stage "
            "must derive + put_frontier the edges leading out of the "
            "just-materialized expansion"
        )
        for fe in frontier:
            assert fe.from_region_id in reloaded.nodes

    async def test_commit_is_atomic_injected_midwrite_failure_rolls_back(
        self, monkeypatch: Any, migrated_db: str
    ) -> None:
        """Task 6 bullet 1: an injected failure mid-write leaves the save
        unchanged — NO half-attached expansion, NO orphan ledger rows, NO
        orphan frontier rows, NO orphan mutation rows. Binds Plan 5's real
        txn primitive: _stage_commit wraps the commit in
        dungeon_repository.transaction() which rolls back on PersistError."""
        import sidequest.telemetry.spans as _spans_module
        from sidequest.dungeon.materializer import materialize
        from sidequest.dungeon.persistence import PersistError
        from tests.dungeon.conftest import build_pg_dungeon_repo

        theme_id = "atomic_crypt"
        palette = _commit_palette(theme_id)
        graph = _seed_graph_themed(theme_id)

        _pool, repo, _sid = build_pg_dungeon_repo(monkeypatch, migrated_db)

        # Inject a mid-write PersistError AFTER commit_expansion (regions +
        # edges) have written into the uncommitted txn, but BEFORE the
        # transaction commits. The rollback contract must discard ALL of it.
        #
        # _stage_commit calls tx.put_frontier (on the PgDungeonTransaction
        # yielded by dungeon_repository.transaction()), so we patch the
        # class-level method. The test-scope monkeypatch resets it after.
        from sidequest.game.pg.dungeon import PgDungeonTransaction

        calls = {"n": 0}

        def _boom_put_frontier(self: Any, fe: Any) -> None:
            calls["n"] += 1
            raise PersistError(
                "injected mid-write failure (simulating commit_expansion "
                "IntegrityError after a partial row write)"
            )

        monkeypatch.setattr(PgDungeonTransaction, "put_frontier", _boom_put_frontier)

        bundle = _real_cookbook_bundle()
        snapshot = _fresh_snapshot()
        pack = _attach_pack("cave_in")
        request = MaterializationRequest_build(
            campaign_seed=7, expansion_id=1, spawn_depth_score=0.0
        )

        _exporter, _provider, real_tracer = _otel_in_memory()
        original_tracer_fn = _spans_module.tracer
        _spans_module.tracer = lambda: real_tracer  # type: ignore[method-assign]
        try:
            with pytest.raises(PersistError, match="injected mid-write"):
                await materialize(
                    request,
                    graph=graph,
                    bundle=bundle,
                    palette=palette,
                    dungeon_repository=repo,
                    snapshot=snapshot,
                    pack_tropes=pack,
                    claude_client=_reflecting_sdk_client(),
                )
        finally:
            _spans_module.tracer = original_tracer_fn  # type: ignore[method-assign]

        assert calls["n"] >= 1, "the injected put_frontier was never reached"

        # The commit-stage writes (inside transaction()) are ROLLED BACK on
        # PersistError — expansion rows, frontier edges, and mutations all
        # discard. The attach-stage thread writes (open_thread via
        # PgDungeonRepository.open_thread, which uses its own session_tx)
        # are committed independently BEFORE the commit transaction opens;
        # they survive the rollback by design — the PG implementation does
        # not re-wrap attach writes inside the commit transaction boundary.
        assert repo.load_map(entrance_id="entrance").nodes == {}, (
            "half-attached expansion survived — _stage_commit did not "
            "roll back on PersistError (the transaction() boundary must "
            "discard the half-written txn)"
        )
        assert repo.load_frontier() == [], "orphan frontier rows survived"
        assert repo.load_mutations() == [], "orphan setpiece-state mutation rows survived rollback"

    async def test_generator_version_bump_does_not_regenerate_frozen_region(
        self, monkeypatch: Any, migrated_db: str
    ) -> None:
        """Task 6 bullet 2 (spec §7 freeze): once an expansion is committed
        it is FROZEN. Re-committing the same expansion_id raises
        PersistError (the region is NOT regenerated; the save is truth).
        And a generator-version bump leaves the frozen region's stamped
        bytes UNCHANGED — while a genuinely never-materialized expansion
        DOES pick up the new version (proving _stage_commit resolves
        GENERATOR_VERSION at commit time, so only new code touches new
        expansions)."""
        import sidequest.dungeon.persistence as _persistence_mod
        import sidequest.telemetry.spans as _spans_module
        from sidequest.dungeon.materializer import (
            AttachResult,
            RegionCuration,
            _stage_commit,
        )
        from sidequest.dungeon.persistence import PersistError
        from sidequest.dungeon.region_graph import (
            Expansion,
            RegionEdge,
            RegionNode,
        )
        from sidequest.dungeon.region_graph.depth import (
            DepthReport,
            assign_depth_scores,
        )
        from sidequest.telemetry.spans.dungeon_materialize import (
            dungeon_materialize_commit_span,
        )
        from tests.dungeon.conftest import build_pg_dungeon_repo

        theme_id = "freeze_crypt"
        palette = _commit_palette(theme_id)
        graph = _seed_graph_themed(theme_id)

        _pool, repo, sid = build_pg_dungeon_repo(monkeypatch, migrated_db)

        # Commit expansion 1 under the current generator version.
        await _materialize_full(
            graph=graph, palette=palette, dungeon_repository=repo, expansion_id=1
        )
        # Query PG for committed versions (session-scoped by sid).
        with _pool.connection() as pgconn:
            committed_rows = pgconn.execute(
                "SELECT region_id, generator_version FROM dungeon_map WHERE session_id = %s",
                (sid,),
            ).fetchall()
        committed_versions = {r[0]: r[1] for r in committed_rows}
        assert committed_versions, "no generator_version stamped"
        assert set(committed_versions.values()) == {_persistence_mod.GENERATOR_VERSION}

        # FREEZE: re-committing the SAME expansion_id (same region ids on a
        # fresh graph) must raise PersistError — the region is on disk and
        # is NEVER rewritten (spec §7 frozen-regions-never-regenerated).
        graph_again = _seed_graph_themed(theme_id)
        with pytest.raises(PersistError, match="freeze|re-commit"):
            await _materialize_full(
                graph=graph_again,
                palette=palette,
                dungeon_repository=repo,
                expansion_id=1,
            )
        with _pool.connection() as pgconn:
            after_rows = pgconn.execute(
                "SELECT region_id, generator_version FROM dungeon_map WHERE session_id = %s",
                (sid,),
            ).fetchall()
        after_refreeze = {r[0]: r[1] for r in after_rows}
        assert after_refreeze == committed_versions, (
            "a frozen region's bytes changed on a refused re-commit — the "
            "freeze + rollback contract was violated (spec §7)"
        )

        # Bump GENERATOR_VERSION mid-campaign, then commit a genuinely NEW
        # expansion (id 2) attached to the loaded map via the real
        # region-graph APIs (test_persistence.py::_generate_and_attach
        # precedent — no mocking of the dungeon layer). Decisive: the new
        # expansion's regions are stamped with the BUMPED version, proving
        # _stage_commit reads GENERATOR_VERSION at commit time so only
        # never-materialized expansions use new code.
        original_version = _persistence_mod.GENERATOR_VERSION
        _persistence_mod.GENERATOR_VERSION = "plan5.v999-BUMPED"
        try:
            live = repo.load_map(entrance_id="entrance")
            exp1_ids = [n.id for n in live.nodes.values() if n.expansion_id == 1]
            assert len(exp1_ids) >= 1
            # Two new regions wired to >=2 explored regions (entrance + an
            # expansion-1 region) so attach_expansion's connected+loopful
            # invariants hold and there is no single chokepoint.
            anchor = exp1_ids[0]
            r0 = RegionNode(id="exp002.r0", expansion_id=2, theme=theme_id)
            r1 = RegionNode(id="exp002.r1", expansion_id=2, theme=theme_id)
            exp2 = Expansion(
                expansion_id=2,
                new_nodes=[r0, r1],
                new_edges=[
                    RegionEdge(a="entrance", b="exp002.r0", kind="corridor"),
                    RegionEdge(a="exp002.r0", b="exp002.r1", kind="stairs"),
                    RegionEdge(a="exp002.r1", b=anchor, kind="secret", hidden=True),
                ],
            )
            live.add_node(r0)
            live.add_node(r1)
            for e in exp2.new_edges:
                live.add_edge(e)
            assign_depth_scores(live, campaign_seed=7)

            request2 = MaterializationRequest_build(
                campaign_seed=7, expansion_id=2, spawn_depth_score=0.0
            )
            # A real AttachResult with no set-piece reports (this focused
            # commit-level check exercises the version-stamp path, not the
            # freeze-target path — that is covered elsewhere). NOT a stub:
            # the genuine frozen value object with an empty report list.
            attach_result2 = AttachResult(
                depth_report=DepthReport(regions_scored=0),
                attach_reports=[],
            )

            # A minimal RegionCuration with empty manifests for this focused test
            # (commit doesn't read the manifests when there are no new_nodes with manifests)
            curation2 = RegionCuration(
                region_manifests={},
                region_creatures={},
                region_big_bad={},
                region_look={},
            )

            _exporter, _provider, real_tracer = _otel_in_memory()
            original_tracer_fn = _spans_module.tracer
            _spans_module.tracer = lambda: real_tracer  # type: ignore[method-assign]
            try:
                with dungeon_materialize_commit_span(expansion_id=2) as span2:
                    _stage_commit(
                        request2,
                        curation=curation2,
                        graph=live,
                        expansion=exp2,
                        attach_result=attach_result2,
                        dungeon_repository=repo,
                        span=span2,
                    )
            finally:
                _spans_module.tracer = original_tracer_fn  # type: ignore[method-assign]

            with _pool.connection() as pgconn:
                exp2_rows = pgconn.execute(
                    "SELECT generator_version FROM dungeon_map "
                    "WHERE session_id = %s AND expansion_id = 2",
                    (sid,),
                ).fetchall()
            exp2_versions = {r[0] for r in exp2_rows}
            assert exp2_versions == {"plan5.v999-BUMPED"}, (
                "a never-materialized expansion did not use the new "
                f"generator version; got {exp2_versions} — _stage_commit "
                "must resolve GENERATOR_VERSION at commit time"
            )
            # The frozen expansion-1 regions are STILL the original version.
            with _pool.connection() as pgconn:
                exp1_rows = pgconn.execute(
                    "SELECT generator_version FROM dungeon_map "
                    "WHERE session_id = %s AND expansion_id IN (0, 1)",
                    (sid,),
                ).fetchall()
            exp1_versions = {r[0] for r in exp1_rows}
            assert exp1_versions == {original_version}, (
                "a frozen region's generator_version changed after a "
                "mid-campaign bump — spec §7 freeze violated"
            )
        finally:
            _persistence_mod.GENERATOR_VERSION = original_version

    async def test_commit_emits_commit_and_frontier_expand_spans_routed(
        self,
        monkeypatch: Any,
        migrated_db: str,
    ) -> None:
        """OTEL Observability Principle / spec §8: the commit stage emits
        ``dungeon.materialize.commit`` (real success summary, not the
        Task-1 placeholder) AND one ``frontier.expand`` per new unexpanded
        frontier edge — both routed so the GM panel (lie detector) sees
        the dungeon's frontier actually grew, not narration claiming it."""
        from sidequest.telemetry.spans import SPAN_ROUTES
        from sidequest.telemetry.spans.dungeon_materialize import (
            SPAN_DUNGEON_MATERIALIZE_COMMIT,
            SPAN_FRONTIER_EXPAND,
        )
        from tests.dungeon.conftest import build_pg_dungeon_repo

        _pool, repo, _sid = build_pg_dungeon_repo(monkeypatch, migrated_db)

        theme_id = "span_crypt"
        palette = _commit_palette(theme_id)
        graph = _seed_graph_themed(theme_id)

        import sidequest.telemetry.spans as _spans_module
        from sidequest.dungeon.materializer import materialize

        exporter, _provider, real_tracer = _otel_in_memory()
        original_tracer_fn = _spans_module.tracer
        _spans_module.tracer = lambda: real_tracer  # type: ignore[method-assign]
        try:
            request = MaterializationRequest_build(
                campaign_seed=7, expansion_id=1, spawn_depth_score=0.0
            )
            await materialize(
                request,
                graph=graph,
                bundle=_real_cookbook_bundle(),
                palette=palette,
                dungeon_repository=repo,
                snapshot=_fresh_snapshot(),
                pack_tropes=_attach_pack("cave_in"),
                claude_client=_reflecting_sdk_client(),
            )
        finally:
            _spans_module.tracer = original_tracer_fn  # type: ignore[method-assign]

        finished = exporter.get_finished_spans()

        commit_spans = [s for s in finished if s.name == SPAN_DUNGEON_MATERIALIZE_COMMIT]
        assert commit_spans, "dungeon.materialize.commit span not emitted"
        crow = SPAN_ROUTES[SPAN_DUNGEON_MATERIALIZE_COMMIT].extract(
            commit_spans[0]  # type: ignore[arg-type]
        )
        # Real success summary (not the Task-1 placeholder): seeded the
        # entrance (fresh save), committed regions, no failure marker.
        assert crow["seeded_entrance"] is True
        assert crow["regions_committed"] >= 1
        assert crow["generator_version"] == "plan5.v1"
        assert crow.get("error") is None
        assert crow.get("reason") is None

        expand_spans = [s for s in finished if s.name == SPAN_FRONTIER_EXPAND]
        assert expand_spans, (
            "frontier.expand not emitted — the commit stage must emit one "
            "per new unexpanded frontier edge (spec §8)"
        )
        live = repo.load_map(entrance_id="entrance")
        for s in expand_spans:
            erow = SPAN_ROUTES[SPAN_FRONTIER_EXPAND].extract(s)  # type: ignore[arg-type]
            assert erow["from_region_id"] in live.nodes
            assert erow["heading"] == "north"
            assert erow["frontier_edge_id"]
        # Span count matches persisted frontier rows (lie detector:
        # spans vs the real save, not narration).
        assert len(expand_spans) == len(repo.load_frontier())


# ---------------------------------------------------------------------------
# Story 52-2: Stage 2 mask emit — ADR-096 mask + derived block per region
# ---------------------------------------------------------------------------
#
# These tests pin the contract for the materializer's mask-emit seam (Plan 7
# Task 3 follow-on, ADR-106 §10 spec item 6 + ADR-096 format contract). The
# fill stage builds the grid; the emit step turns that grid into:
#
#   - ``RegionMask.mask_bytes`` — ASCII wall/floor mask (the truth ADR-096 §2)
#   - ``RegionMask.mask_sha``   — SHA-256 hex of mask_bytes (dedupe + lie-detector)
#   - ``RegionMask.block``      — ``BlockInfo`` with cell_width=28 (ADR-096),
#                                  grid_width/grid_height matching the grid
#
# OTEL: a ``dungeon.materialize.mask`` child span nests under the active
# ``dungeon.materialize.fill`` span (which itself nests under the parent
# ``dungeon.materialize`` span — so the new span is a grandchild). The span's
# attributes carry ``grid_width``, ``grid_height``, ``cell_width``, ``mask_sha``
# — the GM-panel lie-detector for mask emission.
#
# No silent fallbacks: an empty grid is a loud ValueError, not an empty mask.


class TestStageEmitMask:
    """Story 52-2 tests:

    1. Data model: ``RegionMask`` and ``BlockInfo`` exist with the spec shape.
    2. Mask emit per region: every ``RegionFill`` returned by ``_stage_fill``
       carries a populated ``mask`` field.
    3. Mask bytes are ASCII (``#`` walls + ``.`` floors + optional ``\\n``); no
       other bytes leak.
    4. SHA-256 is 64-char lowercase hex AND deterministic for identical grids.
    5. ``BlockInfo.cell_width == 28`` (ADR-096 canonical) — locked, not derived
       from data.
    6. ``BlockInfo.grid_width`` / ``grid_height`` match the source grid shape.
    7. ``dungeon.materialize.mask`` OTEL span is emitted per region with the
       routed lie-detector attributes (``grid_width``, ``grid_height``,
       ``cell_width``, ``mask_sha``).
    8. The mask span nests under ``dungeon.materialize.fill`` (the live parent
       context during fill stage execution).
    9. The span is registered in ``SPAN_ROUTES`` so the GM panel actually
       renders it (set-but-not-routed is a Plan-7-Task-2 defect class).
    10. Empty/zero-cell grid raises ``ValueError`` loudly — No Silent Fallbacks.
    """

    def test_region_mask_dataclass_shape_exists(self) -> None:
        """``RegionMask`` and ``BlockInfo`` are real, importable dataclasses
        with the spec-pinned field set (story 52-2 Data Model Shape)."""
        from sidequest.dungeon.materializer import BlockInfo, RegionMask

        # RegionMask carries grid + mask_bytes + mask_sha + block (4 fields).
        rm_fields = {f.name for f in RegionMask.__dataclass_fields__.values()}
        assert {"grid", "mask_bytes", "mask_sha", "block"} <= rm_fields, (
            f"RegionMask must expose grid/mask_bytes/mask_sha/block; got {rm_fields}"
        )
        # BlockInfo carries cell_width + grid_width + grid_height (+ origins).
        bi_fields = {f.name for f in BlockInfo.__dataclass_fields__.values()}
        assert {"cell_width", "grid_width", "grid_height"} <= bi_fields, (
            f"BlockInfo must expose cell_width/grid_width/grid_height; got {bi_fields}"
        )

    def test_emit_mask_attaches_region_mask_to_each_fill(self) -> None:
        """Every ``RegionFill`` returned by ``_stage_fill`` carries a populated
        ``mask: RegionMask``. The contract: fill stage produces masks for every
        generated region — no None, no skip."""
        import sidequest.dungeon.materializer as _mat_module
        from sidequest.dungeon.materializer import RegionMask
        from sidequest.dungeon.themes import ThemePalette
        from sidequest.telemetry.spans.dungeon_materialize import (
            dungeon_materialize_fill_span,
        )

        palette = ThemePalette(
            themes={
                "t_organic": _theme_for_class("t_organic", "organic"),
                "t_laby": _theme_for_class("t_laby", "labyrinthine"),
            }
        )
        expansion = _expansion_with_themes("t_organic", "t_laby")
        request = _make_request_task3()

        exporter, original_tracer_fn, _spans_mod = _setup_otel_task3()
        try:
            with dungeon_materialize_fill_span(expansion_id=request.expansion_id) as span:
                result = _mat_module._stage_fill(
                    request, expansion=expansion, palette=palette, span=span
                )
        finally:
            _spans_mod.tracer = original_tracer_fn

        assert len(result) == 2
        for region_id, fill in result.items():
            assert fill.mask is not None, f"region {region_id!r} missing mask after fill"
            assert isinstance(fill.mask, RegionMask), (
                f"region {region_id!r} mask is {type(fill.mask).__name__}, not RegionMask"
            )
            # Same grid identity — fill's grid IS the mask's source grid
            # (no recomputation, no copy that drifts).
            assert fill.mask.grid is fill.grid, (
                "RegionMask.grid must be the same Grid object the fill was built "
                "from — no silent re-derive, no copy that can drift"
            )

    def test_emit_mask_bytes_are_ascii_walls_and_floors_only(self) -> None:
        """``mask_bytes`` contains only the wall char (``#``), floor char
        (``.``), and an optional row separator (``\\n``). No other bytes leak —
        downstream PNG (52-4) and persistence (52-3) depend on a strict
        wall/floor alphabet (ADR-096 §2 "mask is the truth")."""
        import sidequest.dungeon.materializer as _mat_module
        from sidequest.dungeon.themes import ThemePalette
        from sidequest.telemetry.spans.dungeon_materialize import (
            dungeon_materialize_fill_span,
        )

        palette = ThemePalette(themes={"t_organic": _theme_for_class("t_organic", "organic")})
        expansion = _expansion_with_themes("t_organic")
        request = _make_request_task3()

        exporter, original_tracer_fn, _spans_mod = _setup_otel_task3()
        try:
            with dungeon_materialize_fill_span(expansion_id=request.expansion_id) as span:
                result = _mat_module._stage_fill(
                    request, expansion=expansion, palette=palette, span=span
                )
        finally:
            _spans_mod.tracer = original_tracer_fn

        fill = next(iter(result.values()))
        assert isinstance(fill.mask.mask_bytes, bytes), "mask_bytes must be bytes"
        allowed = set(b"#.\n")
        leaked = {b for b in fill.mask.mask_bytes if b not in allowed}
        assert not leaked, (
            f"mask_bytes contained non-ASCII-mask bytes {leaked!r}; "
            "only wall (#), floor (.), and newline are allowed per ADR-096"
        )
        # At least one wall AND one floor must appear (a fully-wall or fully-
        # floor mask would mean the cellular pass produced a degenerate region;
        # no useful test fixture should be all-one-byte).
        assert b"#" in fill.mask.mask_bytes, "mask has no wall cells — degenerate"
        assert b"." in fill.mask.mask_bytes, "mask has no floor cells — degenerate"

    def test_emit_mask_sha256_is_64_hex_lowercase_and_deterministic(self) -> None:
        """``mask_sha`` is a 64-char lowercase hex SHA-256 of ``mask_bytes`` and
        is byte-identical for byte-identical inputs (the dedupe contract)."""
        import hashlib

        import sidequest.dungeon.materializer as _mat_module
        from sidequest.dungeon.themes import ThemePalette
        from sidequest.telemetry.spans.dungeon_materialize import (
            dungeon_materialize_fill_span,
        )

        palette = ThemePalette(themes={"t_organic": _theme_for_class("t_organic", "organic")})

        def _run() -> Any:
            expansion = _expansion_with_themes("t_organic")
            request = _make_request_task3(campaign_seed=12345, expansion_id=3)
            _exp, orig, mod = _setup_otel_task3()
            try:
                with dungeon_materialize_fill_span(expansion_id=request.expansion_id) as span:
                    return _mat_module._stage_fill(
                        request, expansion=expansion, palette=palette, span=span
                    )
            finally:
                mod.tracer = orig

        fill_a = next(iter(_run().values()))
        fill_b = next(iter(_run().values()))

        # 64-char lowercase hex
        assert len(fill_a.mask.mask_sha) == 64
        assert fill_a.mask.mask_sha == fill_a.mask.mask_sha.lower()
        int(fill_a.mask.mask_sha, 16)  # raises if not hex — what we want

        # Determinism across two materialization runs with identical seed
        assert fill_a.mask.mask_sha == fill_b.mask.mask_sha, (
            "identical inputs must yield identical mask_sha (dedupe contract)"
        )

        # SHA actually matches the bytes (not a fabricated string)
        expected = hashlib.sha256(fill_a.mask.mask_bytes).hexdigest()
        assert fill_a.mask.mask_sha == expected, (
            f"mask_sha {fill_a.mask.mask_sha!r} does not equal sha256(mask_bytes); "
            "expected = {expected!r}. The sha must be derived from the bytes, "
            "not constructed independently."
        )

    def test_block_info_cell_width_is_adr096_canonical_28(self) -> None:
        """``BlockInfo.cell_width`` is locked to 28 per ADR-096 — not derived
        from grid dimensions, not configurable per-region. This is a rule-of-
        the-format constant; downstream tile placement assumes it."""
        import sidequest.dungeon.materializer as _mat_module
        from sidequest.dungeon.themes import ThemePalette
        from sidequest.telemetry.spans.dungeon_materialize import (
            dungeon_materialize_fill_span,
        )

        # Four different generators — none of them can change cell_width.
        palette = ThemePalette(
            themes={
                "t_organic": _theme_for_class("t_organic", "organic"),
                "t_laby": _theme_for_class("t_laby", "labyrinthine"),
                "t_struct": _theme_for_class("t_struct", "structured"),
                "t_built": _theme_for_class("t_built", "built"),
            }
        )
        expansion = _expansion_with_themes("t_organic", "t_laby", "t_struct", "t_built")
        request = _make_request_task3()

        exporter, original_tracer_fn, _spans_mod = _setup_otel_task3()
        try:
            with dungeon_materialize_fill_span(expansion_id=request.expansion_id) as span:
                result = _mat_module._stage_fill(
                    request, expansion=expansion, palette=palette, span=span
                )
        finally:
            _spans_mod.tracer = original_tracer_fn

        for region_id, fill in result.items():
            assert fill.mask.block.cell_width == 28, (
                f"region {region_id!r} cell_width is {fill.mask.block.cell_width}, "
                "must be 28 per ADR-096 (cell-stepped math canonical)"
            )

    def test_block_info_dimensions_match_source_grid(self) -> None:
        """``BlockInfo.grid_width``/``grid_height`` equal the source ``Grid``'s
        column / row counts. The block is the ADR-096 cell-math anchor; if it
        drifts from the actual grid, downstream movement/AoE evaluation lies."""
        import sidequest.dungeon.materializer as _mat_module
        from sidequest.dungeon.themes import ThemePalette
        from sidequest.telemetry.spans.dungeon_materialize import (
            dungeon_materialize_fill_span,
        )

        palette = ThemePalette(themes={"t_organic": _theme_for_class("t_organic", "organic")})
        expansion = _expansion_with_themes("t_organic")
        request = _make_request_task3()

        exporter, original_tracer_fn, _spans_mod = _setup_otel_task3()
        try:
            with dungeon_materialize_fill_span(expansion_id=request.expansion_id) as span:
                result = _mat_module._stage_fill(
                    request, expansion=expansion, palette=palette, span=span
                )
        finally:
            _spans_mod.tracer = original_tracer_fn

        fill = next(iter(result.values()))
        grid = fill.grid
        assert fill.mask.block.grid_height == len(grid)
        assert fill.mask.block.grid_width == len(grid[0])

    def test_dungeon_materialize_mask_span_emitted_per_region(self) -> None:
        """A ``dungeon.materialize.mask`` span is emitted for every region with
        ``grid_width``, ``grid_height``, ``cell_width``, ``mask_sha`` attrs —
        the GM-panel lie-detector for mask emission."""
        import sidequest.dungeon.materializer as _mat_module
        from sidequest.dungeon.themes import ThemePalette
        from sidequest.telemetry.spans.dungeon_materialize import (
            SPAN_DUNGEON_MATERIALIZE_MASK,
            dungeon_materialize_fill_span,
        )

        palette = ThemePalette(
            themes={
                "t_organic": _theme_for_class("t_organic", "organic"),
                "t_laby": _theme_for_class("t_laby", "labyrinthine"),
            }
        )
        expansion = _expansion_with_themes("t_organic", "t_laby")
        request = _make_request_task3()

        exporter, original_tracer_fn, _spans_mod = _setup_otel_task3()
        try:
            with dungeon_materialize_fill_span(expansion_id=request.expansion_id) as span:
                result = _mat_module._stage_fill(
                    request, expansion=expansion, palette=palette, span=span
                )
        finally:
            _spans_mod.tracer = original_tracer_fn

        finished = exporter.get_finished_spans()
        mask_spans = [s for s in finished if s.name == SPAN_DUNGEON_MATERIALIZE_MASK]
        assert len(mask_spans) == 2, (
            f"expected one dungeon.materialize.mask span per region (2); got {len(mask_spans)}"
        )
        # Every mask span carries the routed attribute set
        masks_by_sha = {f.mask.mask_sha: f for f in result.values()}
        for span_obj in mask_spans:
            attrs = dict(span_obj.attributes or {})
            assert "grid_width" in attrs, f"missing grid_width on span; have {sorted(attrs)}"
            assert "grid_height" in attrs, f"missing grid_height on span; have {sorted(attrs)}"
            assert "cell_width" in attrs, f"missing cell_width on span; have {sorted(attrs)}"
            assert "mask_sha" in attrs, f"missing mask_sha on span; have {sorted(attrs)}"
            assert attrs["cell_width"] == 28
            # mask_sha on the span must be one of the real per-region sha values
            assert attrs["mask_sha"] in masks_by_sha, (
                f"span mask_sha {attrs['mask_sha']!r} does not match any "
                f"region's mask (have {sorted(masks_by_sha)}); the span is "
                "decoupled from the real emit"
            )

    def test_mask_span_nests_under_fill_span(self) -> None:
        """The mask span's parent context is the live ``dungeon.materialize.fill``
        span — proving the emit step runs INSIDE the fill stage (not bolted on
        after, not opened at module scope)."""
        import sidequest.dungeon.materializer as _mat_module
        from sidequest.dungeon.themes import ThemePalette
        from sidequest.telemetry.spans.dungeon_materialize import (
            SPAN_DUNGEON_MATERIALIZE_FILL,
            SPAN_DUNGEON_MATERIALIZE_MASK,
            dungeon_materialize_fill_span,
        )

        palette = ThemePalette(themes={"t_organic": _theme_for_class("t_organic", "organic")})
        expansion = _expansion_with_themes("t_organic")
        request = _make_request_task3()

        exporter, original_tracer_fn, _spans_mod = _setup_otel_task3()
        try:
            with dungeon_materialize_fill_span(expansion_id=request.expansion_id) as span:
                _mat_module._stage_fill(request, expansion=expansion, palette=palette, span=span)
        finally:
            _spans_mod.tracer = original_tracer_fn

        finished = exporter.get_finished_spans()
        by_name = {s.name: s for s in finished}
        assert SPAN_DUNGEON_MATERIALIZE_FILL in by_name, "fill span missing"
        assert SPAN_DUNGEON_MATERIALIZE_MASK in by_name, "mask span missing"

        mask_span = by_name[SPAN_DUNGEON_MATERIALIZE_MASK]
        fill_span = by_name[SPAN_DUNGEON_MATERIALIZE_FILL]
        assert mask_span.parent is not None, "mask span has no parent — not nested"
        assert mask_span.parent.span_id == fill_span.context.span_id, (
            "dungeon.materialize.mask must nest under dungeon.materialize.fill "
            "(the live context during fill-stage emit)"
        )

    def test_mask_span_routed_for_gm_panel(self) -> None:
        """The span constant is registered in ``SPAN_ROUTES`` with the lie-
        detector attribute extractor — set-but-not-routed is the Plan-7-Task-2
        defect class (GM panel would never render it)."""
        from sidequest.telemetry.spans import SPAN_ROUTES
        from sidequest.telemetry.spans.dungeon_materialize import (
            SPAN_DUNGEON_MATERIALIZE_MASK,
        )

        assert SPAN_DUNGEON_MATERIALIZE_MASK == "dungeon.materialize.mask"
        assert SPAN_DUNGEON_MATERIALIZE_MASK in SPAN_ROUTES, (
            "dungeon.materialize.mask is set but not routed — GM panel would "
            "never render it (Plan-7-Task-2 defect class)"
        )
        # The route extracts the routed attribute set
        route = SPAN_ROUTES[SPAN_DUNGEON_MATERIALIZE_MASK]
        assert route.component == "dungeon"

    def test_empty_grid_raises_loudly_no_silent_fallback(self) -> None:
        """An empty grid (``[]`` or ``[[]]``) at the emit seam is a loud
        ``ValueError`` — never a silent empty-mask. The mask is the truth for
        downstream stages; an empty mask would be a silent lie."""
        from sidequest.dungeon.materializer import _emit_mask

        with pytest.raises(ValueError, match="empty|grid|cells"):
            _emit_mask([])
        with pytest.raises(ValueError, match="empty|grid|cells"):
            _emit_mask([[]])

    def test_region_fill_is_still_frozen_after_mask_field_added(self) -> None:
        """Adding the ``mask`` field to ``RegionFill`` must not weaken the
        frozen+slots discipline — the dataclass still rejects field reassign."""
        import dataclasses

        from sidequest.dungeon.materializer import RegionFill

        # Frozen check: the dataclass-options dict is the canonical contract
        params = RegionFill.__dataclass_params__
        assert params.frozen is True, "RegionFill must stay frozen after mask added"
        # Re-assignment of the new field must raise on a real instance.
        # Build via _stage_fill so we don't have to guess Field defaults.
        import sidequest.dungeon.materializer as _mat_module
        from sidequest.dungeon.themes import ThemePalette
        from sidequest.telemetry.spans.dungeon_materialize import (
            dungeon_materialize_fill_span,
        )

        palette = ThemePalette(themes={"t_organic": _theme_for_class("t_organic", "organic")})
        expansion = _expansion_with_themes("t_organic")
        request = _make_request_task3()
        exporter, original_tracer_fn, _spans_mod = _setup_otel_task3()
        try:
            with dungeon_materialize_fill_span(expansion_id=request.expansion_id) as span:
                result = _mat_module._stage_fill(
                    request, expansion=expansion, palette=palette, span=span
                )
        finally:
            _spans_mod.tracer = original_tracer_fn

        fill = next(iter(result.values()))
        with pytest.raises(dataclasses.FrozenInstanceError):
            fill.mask = None  # type: ignore[misc]
