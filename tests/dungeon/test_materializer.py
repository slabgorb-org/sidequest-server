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
        monkeypatch: Any,
        migrated_db: str,
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
        from sidequest.telemetry.spans.dungeon_materialize import (
            SPAN_DUNGEON_MATERIALIZE,
            SPAN_DUNGEON_MATERIALIZE_ATTACH,
            SPAN_DUNGEON_MATERIALIZE_COMMIT,
            SPAN_DUNGEON_MATERIALIZE_CURATE,
            SPAN_DUNGEON_MATERIALIZE_DESIGN,
            SPAN_DUNGEON_MATERIALIZE_FILL,
        )
        from tests.dungeon.conftest import build_pg_dungeon_repo

        # ADR-115 D6: the coordinator opens dungeon_repository.transaction()
        # spanning attach+commit, so the repo must be a real PgDungeonRepository
        # (DungeonStore has no transaction()). The five stages are no-op'd, so
        # the txn is opened+committed empty — this test asserts ONLY span
        # nesting/order.
        _pool, store, _sid = build_pg_dungeon_repo(monkeypatch, migrated_db)

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

        def _design_noop(*args: object, **kwargs: object) -> tuple[object, object]:
            # The real _stage_design ALWAYS returns (Expansion,
            # GenerationReport) (Task 2 hard contract); the coordinator
            # unconditionally unpacks it. This stub honors that contract so
            # the test exercises the real unpack path while still no-op'ing
            # all stage logic — it only asserts span nesting/order.
            return (object(), object())

        def _commit_noop(*args: object, **kwargs: object) -> tuple[object, dict]:
            # ADR-115 D6: _stage_commit returns (world_dir, composed_by_region)
            # for the coordinator's post-commit YAML emit; the coordinator
            # unconditionally unpacks it. Stub honors that contract (None
            # world_dir → no emit) while no-op'ing the commit logic.
            return (None, {})

        _mat_module._stage_design = _design_noop  # type: ignore[assignment]
        _mat_module._stage_fill = _noop  # type: ignore[assignment]
        # ADR-106 Amendment C: _stage_curate is now a sync no-op (no LLM).
        _mat_module._stage_curate = _noop  # type: ignore[assignment]
        _mat_module._stage_attach = _noop  # type: ignore[assignment]
        _mat_module._stage_commit = _commit_noop  # type: ignore[assignment]
        try:
            req = self._build_request()
            # ADR-115 D6 follow-up: the coordinator reads graph.entrance_id for
            # fresh-save detection BEFORE the txn opens (and rejects graph=None
            # loudly), so this span-ordering test passes a minimal real graph.
            # The empty repo makes load_map/load_frontier return empty; stages
            # stay no-op'd so the test still asserts ONLY span nesting/order.
            from sidequest.dungeon.region_graph import RegionGraph  # noqa: PLC0415

            # snapshot/pack_tropes are required materialize() params; the
            # curate+attach stages are monkeypatched to no-ops here so the
            # values are never read — passing real-shaped placeholders keeps
            # the signature satisfied while the test asserts ONLY span
            # nesting/order (unchanged Task-1 contract).
            await materialize(
                req,
                graph=RegionGraph(entrance_id="entrance"),
                bundle=None,
                palette=None,
                dungeon_repository=store,
                snapshot=_fresh_snapshot(),
                pack_tropes=_attach_pack("cave_in"),
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
    """Every dungeon.materialize.* and frontier.expand constant must be in
    SPAN_ROUTES — the routing-completeness gate must not fail on our additions.
    (ADR-106 Amendment C retired the dungeon.curate.parse_failed / .degraded
    spans with the LLM curate ladder.)"""
    from sidequest.telemetry.spans import SPAN_ROUTES
    from sidequest.telemetry.spans.dungeon_materialize import (
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
                    tx=store,
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
                    tx=store,
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
                    tx=store,
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

    async def test_coordinator_threads_attach_result_into_commit(
        self, monkeypatch: Any, migrated_db: str
    ) -> None:
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
        from sidequest.dungeon.region_graph import RegionNode
        from sidequest.dungeon.themes import ThemePalette
        from tests.dungeon.conftest import build_pg_dungeon_repo

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

        # ADR-115 D6: the coordinator opens dungeon_repository.transaction()
        # and the REAL _stage_attach writes its threads through that tx, so a
        # real PgDungeonRepository is required (DungeonStore has no
        # transaction()).
        _pool, store, _sid = build_pg_dungeon_repo(monkeypatch, migrated_db)

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
        # _stage_commit returns (world_dir, composed_by_region) post-D6; the
        # coordinator unpacks it, so the stub mirrors that shape.
        def _capturing_commit(*args: Any, **kwargs: Any) -> tuple[object, dict]:
            captured["commit_kwargs"] = kwargs
            return (None, {})

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
                )
        finally:
            _spans_module.tracer = original_tracer_fn  # type: ignore[method-assign]

        assert calls["n"] >= 1, "the injected put_frontier was never reached"

        # ADR-115 D6 atomicity contract: the coordinator owns ONE transaction
        # spanning _stage_attach (which opened the complication threads on
        # ``tx``) AND _stage_commit. A commit_expansion/put_frontier
        # PersistError rolls the WHOLE txn back — expansion rows, frontier
        # edges, mutations AND the attach-stage threads all discard together.
        # NO half-attached expansion / NO orphan ledger (the Plan-5 contract
        # the D6 lift regressed and this guard pins restored).
        assert repo.load_map(entrance_id="entrance").nodes == {}, (
            "half-attached expansion survived — the materialize transaction() "
            "boundary must discard the half-written txn on PersistError"
        )
        assert repo.load_frontier() == [], "orphan frontier rows survived"
        assert repo.load_mutations() == [], "orphan setpiece-state mutation rows survived rollback"
        assert repo.open_threads() == [], (
            "orphan ledger threads survived rollback — the attach-stage "
            "open_thread writes must ride the SAME txn the commit rolls back "
            "(ADR-115 D6: open_thread on the materialize transaction, not its "
            "own session_tx). A non-empty list means attach threads committed "
            "independently of the expansion (the regression)."
        )

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
                # ADR-115 D6: the coordinator owns the txn; this focused
                # direct call opens one and passes it as ``tx`` (mirrors the
                # production attach+commit single-transaction boundary).
                with (
                    dungeon_materialize_commit_span(expansion_id=2) as span2,
                    repo.transaction() as tx2,
                ):
                    _stage_commit(
                        request2,
                        curation=curation2,
                        graph=live,
                        expansion=exp2,
                        attach_result=attach_result2,
                        is_fresh_save=False,
                        tx=tx2,
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
