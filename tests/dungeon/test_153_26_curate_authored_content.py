"""Story 153-26 — DUNGEON-CURATE-TIMEOUT: keep curate within the wall-clock
cap (per-region) AND make the Layer-2 degrade preserve AUTHORED room content.

Two distinct defects, both surfaced by the 2026-06-20/21 beneath_sunden
playtest (see sprint/context/context-story-153-26.md):

1. PERF — the curate stage curates the WHOLE expansion band in one bounded
   call under a SINGLE FIXED ``asyncio.timeout(CURATE_DEADLINE_S)``, so the cap
   did NOT scale with band size: once a band grew large enough that normal LLM
   latency crossed the fixed cap, the WHOLE band Layer-2-degraded (the
   30337ms-vs-25000ms beneath_sunden failure on exp001.r2..r5). Fix (honest
   per-region budget — Keith's 2026-06-21 decision): scale the cap with region
   count (``CURATE_DEADLINE_S * region_count``) so the budget tracks the work.

2. CORRECTNESS — ``_degrade_region`` / ``_creatures_from_manifest`` translate
   only the procedural ``assemble_region`` manifest; they NEVER consult the
   authored ``rooms/<id>.yaml`` ``encounter_creatures``. So when a region with
   an authored binding (``entrance`` -> ``gnaw_swarm``) degrades, the authored
   encounter is silently dropped. The degrade must honor the LLM-free
   ``resolve_room_creatures`` read (which already emits ``monster_manual.room_bound``).

These tests are RED on develop. The loudness-regression guard
(``test_forced_deadline_degrade_stays_loud``) PASSES on develop and guards the
GREEN phase from silencing the degrade while fixing (2).

Contract notes (TEA-defined; see the SM/TEA assessment for 153-26):
- The fix threads a genre ``pack`` (``source_dir`` + ``effective_bestiary``)
  into ``_stage_curate`` and ``materialize`` so the degrade path can call
  ``resolve_room_creatures(pack, world_slug, region_id)``.
- ``pack`` MUST be optional (default ``None``): the existing ``_stage_curate`` /
  ``materialize`` suites do not pass it, and a packless degrade must stay loud
  (the current behavior, minus authored-content preservation).
"""

from __future__ import annotations

import inspect
import json
import logging
from pathlib import Path
from typing import Any

import pytest

import sidequest.dungeon.materializer as _mat
from sidequest.dungeon.materializer import MaterializationRequest, RegionFill
from sidequest.dungeon.persistence import FrontierEdge
from sidequest.dungeon.region_graph import Expansion
from sidequest.dungeon.region_graph.model import RegionNode
from sidequest.dungeon.themes import ThemePalette
from sidequest.genre.models.bestiary import Bestiary, BestiaryEntry
from sidequest.telemetry.spans.dungeon_materialize import dungeon_materialize_curate_span

# Reuse the established materializer test harness (real cookbook bundle, OTEL
# in-memory capture, the slow/valid SDK fakes, the curate-input builders).
from tests.dungeon.test_materializer import (
    _attach_pack,
    _commit_palette,
    _curate_inputs_two_regions,
    _fresh_snapshot,
    _otel_in_memory,
    _real_cookbook_bundle,
    _seed_graph_themed,
    _setup_otel_task3,
    _slow_then_valid_sdk_client,
    _theme_bound_to_look,
    _tooling_result,
    _well_formed_verdict_text,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spans_named(exporter: Any, name: str) -> list[Any]:
    return [s for s in exporter.get_finished_spans() if s.name == name]


def _authored_pack(
    source_root: Path,
    *,
    world_slug: str,
    region_ids: list[str],
    creature_id: str,
    creature_name: str,
) -> Any:
    """A duck-typed genre pack for ``resolve_room_creatures``.

    Writes ``{source_root}/worlds/{world_slug}/rooms/{rid}.yaml`` binding
    ``encounter_creatures: [creature_id]`` for each region id, and exposes a
    ``source_dir`` + an ``effective_bestiary`` carrying the bound creature.
    ``resolve_room_creatures`` reads exactly those two attributes.
    """
    rooms_dir = source_root / "worlds" / world_slug / "rooms"
    rooms_dir.mkdir(parents=True, exist_ok=True)
    for rid in region_ids:
        (rooms_dir / f"{rid}.yaml").write_text(
            f"id: {rid}\nencounter_creatures:\n- {creature_id}\n",
            encoding="utf-8",
        )
    bestiary = Bestiary(
        entries=[
            BestiaryEntry(
                id=creature_id,
                name=creature_name,
                level=1,
                hp=8,
                armor_class=12,
                attack_bonus=1,
            )
        ]
    )

    class _AuthoredPack:
        source_dir = source_root

        def effective_bestiary(self, world: str | None) -> tuple[Bestiary, str]:
            return bestiary, (world or "")

    return _AuthoredPack()


def _curate_inputs_world(
    *,
    world_slug: str,
    genre_slug: str = "caverns_and_claudes",
    algorithm: str = "prim",
    expansion_id: int = 1,
    depth_score: float = 0.5,
) -> tuple[Any, Any, Any, Any, str]:
    """Like test_materializer._curate_inputs, but threads genre_slug/world_slug
    onto the request so the authored-room path resolves under a real world dir.
    Returns (request, palette, expansion, fill_result, region_id)."""
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
        genre_slug=genre_slug,
        world_slug=world_slug,
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
    return request, palette, expansion, fill_result, rid


def _slow_proportional_sdk_client(*, per_region_s: float) -> Any:
    """Returns a well-formed verdict after sleeping ``per_region_s`` times the
    number of regions in this curate call's INPUT.

    Models "normal LLM latency that scales with band size". With the honest
    per-region budget (`CURATE_DEADLINE_S * region_count`), an N-region band of
    normal-latency regions completes; with a single FIXED cap it times out once
    the band is large enough (the beneath_sunden 30s-vs-25s failure).
    """
    import asyncio as _asyncio

    class _SlowProportional:
        async def complete_with_tools(self, *a: Any, model: str, messages: Any, **k: Any) -> Any:
            prompt = messages[0].content
            _, _, input_blob = prompt.partition("INPUT:\n")
            try:
                payload = json.loads(input_blob)
            except json.JSONDecodeError:
                payload = {}
            n = max(1, len(payload))
            await _asyncio.sleep(per_region_s * n)
            return _tooling_result(_well_formed_verdict_text(messages), model)

    return _SlowProportional()


# ---------------------------------------------------------------------------
# AC2 + AC4 — authored content survives a degrade; monster_manual.room_bound fires
# ---------------------------------------------------------------------------


async def test_degraded_region_surfaces_authored_encounter_creatures(
    tmp_path: Path,
) -> None:
    """AC2 + AC4 (RED on develop): a Layer-2 deadline degrade of a region whose
    authored ``rooms/<id>.yaml`` binds ``gnaw_swarm`` must STILL surface that
    authored creature, and emit ``monster_manual.room_bound`` — proving the
    degrade consulted the LLM-free authored binding rather than dropping it.
    """
    if "pack" not in inspect.signature(_mat._stage_curate).parameters:
        pytest.fail(
            "153-26 AC2: _stage_curate must accept a `pack` (optional, default "
            "None) so a Layer-2 degrade can call resolve_room_creatures and "
            "surface the authored rooms/<id>.yaml encounter_creatures (gnaw_swarm)"
        )

    world = "beneath_test"
    bundle = _real_cookbook_bundle()
    request, palette, expansion, fill_result, rid = _curate_inputs_world(
        world_slug=world, expansion_id=1
    )
    pack = _authored_pack(
        tmp_path,
        world_slug=world,
        region_ids=[rid],
        creature_id="gnaw_swarm",
        creature_name="Gnaw Swarm",
    )

    exporter, original_tracer_fn, _spans_mod = _setup_otel_task3()
    original_deadline = _mat.CURATE_DEADLINE_S
    _mat.CURATE_DEADLINE_S = 0.05  # type: ignore[attr-defined]
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
                pack=pack,
            )
    finally:
        _mat.CURATE_DEADLINE_S = original_deadline  # type: ignore[attr-defined]
        _spans_mod.tracer = original_tracer_fn

    # The degrade is real (and still loud — AC3 overlap).
    assert result.curated is False
    assert rid in result.uncurated_regions
    assert _spans_named(exporter, "dungeon.curate.degraded"), (
        "deadline path must Layer-2 degrade loudly"
    )

    # AC2: the authored creature survived the degrade.
    names = {c.name for c in result.region_creatures[rid]}
    assert "Gnaw Swarm" in names, (
        "the degraded region must still surface its authored encounter_creatures "
        f"(gnaw_swarm -> 'Gnaw Swarm'); degrade dropped it. Got creatures: {names}"
    )

    # AC4: the authored-content-preserved span fired (GM-panel lie detector).
    assert _spans_named(exporter, "monster_manual.room_bound"), (
        "consulting the authored binding on degrade must emit "
        "monster_manual.room_bound so the GM panel can SEE authored content "
        "survived the degrade rather than trusting the narrator"
    )


# ---------------------------------------------------------------------------
# AC1 — honest per-region budget: the cap scales with band size, so a
#        normal-latency multi-region band does NOT mass-degrade
# ---------------------------------------------------------------------------


async def test_band_curate_budget_scales_with_region_count() -> None:
    """AC1 (honest per-region budget — Keith's 2026-06-21 decision): a 2-region
    band whose per-region curate latency fits the per-region budget must NOT
    degrade, even though the WHOLE-band latency (2 x per-region) exceeds a single
    fixed cap. This pins the root-cause fix: the curate wall-clock cap must scale
    with region count (`CURATE_DEADLINE_S * region_count`) so the deadline tracks
    the work instead of degrading the whole band once it grows (the
    30337ms-vs-25000ms beneath_sunden failure on exp001.r2..r5).

    Without the fix (single fixed `CURATE_DEADLINE_S=0.4` cap) the 2-region call
    sleeps 0.5s > 0.4s -> the whole band Layer-2-degrades. With the fix the band
    cap is 0.4 x 2 = 0.8s > 0.5s -> curated.

    (Per the Design Deviation in the session: this retunes the original
    single-region-isolation test to the band-proportional-budget property the
    honest-budget approach delivers; chunking-level isolation is explicitly out.)
    """
    bundle = _real_cookbook_bundle()
    request, palette, expansion, fill_result = _curate_inputs_two_regions(expansion_id=9)
    r0 = expansion.new_nodes[0].id
    r1 = expansion.new_nodes[1].id

    exporter, original_tracer_fn, _spans_mod = _setup_otel_task3()
    original_deadline = _mat.CURATE_DEADLINE_S
    # Per-region budget 0.4s; per-region work 0.25s. Band work = 0.5s.
    # Fixed cap 0.4 < 0.5 -> would degrade; scaled cap 0.8 > 0.5 -> curated.
    _mat.CURATE_DEADLINE_S = 0.4  # type: ignore[attr-defined]
    try:
        with dungeon_materialize_curate_span(expansion_id=request.expansion_id) as span:
            result = await _mat._stage_curate(
                request,
                bundle=bundle,
                palette=palette,
                expansion=expansion,
                fill_result=fill_result,
                is_first_band_entry=True,
                claude_client=_slow_proportional_sdk_client(per_region_s=0.25),
                span=span,
            )
    finally:
        _mat.CURATE_DEADLINE_S = original_deadline  # type: ignore[attr-defined]
        _spans_mod.tracer = original_tracer_fn

    assert result.curated is True, (
        "an honest per-region budget must accommodate a normal-latency "
        "multi-region band — the cap must scale with region count, not stay a "
        "single fixed value that degrades the whole band once it grows"
    )
    assert not result.uncurated_regions, "no region should degrade under the scaled budget"
    assert result.region_creatures[r0] and result.region_creatures[r1], (
        "both regions ship curated creatures"
    )
    assert not _spans_named(exporter, "dungeon.curate.degraded"), (
        "a within-budget band must not emit a degrade span"
    )


# ---------------------------------------------------------------------------
# AC3 — loudness regression guard (PASSES on develop; guards GREEN)
# ---------------------------------------------------------------------------


async def test_forced_deadline_degrade_stays_loud(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC3 (PASSES on develop — regression guard): a forced deadline degrade
    stays LOUD — curated=False, the region in uncurated_regions, an ERROR log,
    and a routed dungeon.curate.degraded span (failure_kind='deadline'). The
    153-26 fix must NOT silence this while teaching the degrade to preserve
    authored content (No Silent Fallbacks / ADR-106 Amendment A).
    """
    from tests.dungeon.test_materializer import _curate_inputs

    bundle = _real_cookbook_bundle()
    request, palette, expansion, fill_result, _look = _curate_inputs(expansion_id=1)
    rid = expansion.new_nodes[0].id

    exporter, original_tracer_fn, _spans_mod = _setup_otel_task3()
    original_deadline = _mat.CURATE_DEADLINE_S
    _mat.CURATE_DEADLINE_S = 0.05  # type: ignore[attr-defined]
    try:
        with (
            caplog.at_level(logging.ERROR),
            dungeon_materialize_curate_span(expansion_id=request.expansion_id) as span,
        ):
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

    assert result.curated is False
    assert result.curated is not True  # forbidden silent raw-manifest-stamped-curated
    assert rid in result.uncurated_regions
    degraded = _spans_named(exporter, "dungeon.curate.degraded")
    assert degraded, "deadline path must Layer-2 degrade loudly"
    assert dict(degraded[0].attributes or {}).get("failure_kind") == "deadline"
    assert any("dungeon curate degraded" in r.getMessage() for r in caplog.records), (
        "the degrade must log LOUD at ERROR level (No Silent Fallbacks)"
    )


# ---------------------------------------------------------------------------
# AC5 — wiring/integration: authored content survives a degrade through the
#        REAL materialize() pipeline (not just _stage_curate in isolation)
# ---------------------------------------------------------------------------


async def test_materialize_degrade_preserves_authored_content_end_to_end(
    monkeypatch: Any, migrated_db: str, tmp_path: Path
) -> None:
    """AC5 (RED on develop): drive the REAL five-stage materialize() coordinator
    with a tiny injected CURATE_DEADLINE_S so the expansion degrades, against a
    pack whose authored rooms bind ``gnaw_swarm``. Assert the degrade is loud
    (dungeon.curate.degraded) AND the authored binding was honored end-to-end
    (monster_manual.room_bound) — proving ``pack`` is threaded through the whole
    pipeline, not just into _stage_curate in isolation.
    """
    if "pack" not in inspect.signature(_mat.materialize).parameters:
        pytest.fail(
            "153-26 AC5: materialize() must accept/thread a `pack` so the real "
            "pipeline's Layer-2 degrade surfaces authored room content "
            "(end-to-end wiring, not _stage_curate in isolation)"
        )

    import sidequest.telemetry.spans as _spans_module
    from tests.dungeon.conftest import build_pg_dungeon_repo

    _pool, repo, _sid = build_pg_dungeon_repo(monkeypatch, migrated_db)

    bundle = _real_cookbook_bundle()
    theme_id = "authored_crypt_153_26"
    palette = _commit_palette(theme_id)
    graph = _seed_graph_themed(theme_id)

    # _fresh_snapshot() carries world_slug="test_world"; author room bindings for
    # every region id an expansion-1 burst could mint (exp001.r0..r9).
    world = "test_world"
    region_ids = [f"exp001.r{n}" for n in range(10)]
    pack = _authored_pack(
        tmp_path,
        world_slug=world,
        region_ids=region_ids,
        creature_id="gnaw_swarm",
        creature_name="Gnaw Swarm",
    )

    fe = FrontierEdge(
        frontier_edge_id="fe1",
        from_region_id="entrance",
        heading="north",
        spawn_depth_score=15.0,
    )
    request = MaterializationRequest.build(
        campaign_seed=7,
        expansion_id=1,
        frontier_edge=fe,
        frontier=[fe],
        attach_region_ids=["entrance"],
        heading="north",
        burst_magnitude=3,
        lookahead_breadth=2,
        genre_slug="caverns_and_claudes",
        world_slug=world,
    )

    exporter, _provider, real_tracer = _otel_in_memory()
    original_tracer_fn = _spans_module.tracer
    _spans_module.tracer = lambda: real_tracer  # type: ignore[method-assign]
    original_deadline = _mat.CURATE_DEADLINE_S
    _mat.CURATE_DEADLINE_S = 0.05  # type: ignore[attr-defined]
    try:
        await _mat.materialize(
            request,
            graph=graph,
            bundle=bundle,
            palette=palette,
            dungeon_repository=repo,
            snapshot=_fresh_snapshot(),
            pack_tropes=_attach_pack("cave_in"),
            claude_client=_slow_then_valid_sdk_client(2.0),
            pack=pack,
        )
    finally:
        _mat.CURATE_DEADLINE_S = original_deadline  # type: ignore[attr-defined]
        _spans_module.tracer = original_tracer_fn

    assert _spans_named(exporter, "dungeon.curate.degraded"), (
        "the real pipeline must Layer-2 degrade under the tiny injected cap"
    )
    assert _spans_named(exporter, "monster_manual.room_bound"), (
        "through the REAL materialize() pipeline, a degraded region with an "
        "authored binding must surface it (monster_manual.room_bound) — proving "
        "pack is threaded end-to-end, not just in _stage_curate isolation"
    )
