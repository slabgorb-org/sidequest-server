"""Story 158-12 — DUNGEON-CURATE-DETERMINISTIC: the Stage-3 curate pass no
longer calls an LLM. Materialization becomes pure-deterministic end-to-end
(``assemble_region -> _creatures_from_manifest -> _append_authored_creatures``)
and the live narrator owns creature prose at narration time.

Design authority: ADR-106 **Amendment C** (2026-06-23). These tests RED on
``develop`` and GREEN once the LLM is removed from ``_stage_curate``.

Why these are RED on develop (the honest contract signal):
``materialize`` / ``_stage_curate`` currently REQUIRE an injected ``claude_client``
(``materialize`` raises ``TypeError`` if the kwarg is missing; both reject
``None`` loudly). Every behaviour Amendment C promises — determinism, a clean
curate span, authored-binding survival on the MAIN path — is unreachable on
develop precisely because the per-expansion Haiku call is mandatory. So driving
``materialize`` with NO ``claude_client`` is the contract: it cannot run today,
and after the deletion it runs deterministically with zero LLM.

Verification is by OTEL spans + behaviour (committed ``region_population``
mutations, read back through the real Pg commit path), never source-text grep
(server CLAUDE.md "No Source-Text Wiring Tests"). The two signature checks use
``inspect.signature`` — the sanctioned reflection "tripwire", not a source read.

AC map:
- AC1  no LLM in curate path  -> test_stage_curate_signature_drops_claude_client
                                  + test_materialize_runs_with_no_llm_client
- AC2  deterministic rosters  -> test_materialize_is_seed_deterministic
- AC3  curate span always clean -> test_curate_span_is_clean_no_failure_spans
- AC4  authored bindings survive -> test_authored_binding_survives_deterministic_materialize
- AC5  CurationError carve-out (i) retained -> test_curation_error_carveout_i_retained
        (GREEN regression guard — must stay GREEN across the refactor)
- AC6  no regression on seating + narration: the no-LLM materialize tests below
        are themselves wiring tests (real materialize -> real Pg commit -> real
        region_population read); the existing
        tests/dungeon/test_materializer_wiring.py stays green (Dev's GREEN-phase).
- AC7  sync collapse is RECOMMENDED-BUT-SEPARABLE, so these tests do NOT assert
        ``materialize`` is sync — ``_run_materialize`` awaits iff the call returns
        a coroutine, surviving either choice.
"""

from __future__ import annotations

import inspect
import json
import logging
from pathlib import Path
from typing import Any

import pytest

import sidequest.dungeon.materializer as _mat
from sidequest.dungeon.materializer import CurationError
from sidequest.genre.models.bestiary import Bestiary, BestiaryEntry
from sidequest.server.dispatch.region_population import load_region_population

# Stable harness builders (inputs + OTEL capture). These are the same load-
# bearing helpers the existing wiring / 153-26 suites import; they build the
# real cookbook bundle, palette, seed graph, request, snapshot, and the
# in-memory OTEL tracer. They do NOT include the curate-LLM fakes (this story
# removes those) — none are imported here, by design.
from tests.dungeon.test_materializer import (
    _attach_pack,
    _commit_palette,
    _fresh_snapshot,
    _make_request_task3,
    _otel_in_memory,
    _real_cookbook_bundle,
    _seed_graph_themed,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _run_materialize(*args: Any, **kwargs: Any) -> Any:
    """Call ``materialize`` with NO ``claude_client`` and await iff it returns a
    coroutine.

    On develop this raises ``TypeError`` at the call site (``claude_client`` is a
    required kwarg) -> RED. After the deletion the kwarg is gone and the call
    binds; AC7 (sync collapse) is separable, so we await only when the result is
    awaitable (works whether Dev keeps ``materialize`` async or collapses it to
    sync). ``*args`` passes the positional ``request`` straight through."""
    result = _mat.materialize(*args, **kwargs)
    if inspect.isawaitable(result):
        result = await result
    return result


def _install_in_memory_tracer() -> tuple[Any, Any]:
    """Install the in-memory OTEL tracer (canonical 153-26 AC5 pattern). Returns
    ``(exporter, restore)`` — call ``restore()`` in a finally."""
    import sidequest.telemetry.spans as _spans_module

    exporter, _provider, real_tracer = _otel_in_memory()
    original = _spans_module.tracer
    _spans_module.tracer = lambda: real_tracer  # type: ignore[method-assign]

    def restore() -> None:
        _spans_module.tracer = original  # type: ignore[method-assign]

    return exporter, restore


def _spans_named(exporter: Any, name: str) -> list[Any]:
    return [s for s in exporter.get_finished_spans() if s.name == name]


def _region_populations(repo: Any) -> dict[str, dict]:
    """The committed ``region_population`` mutation payloads keyed by region id —
    each is the JSON-safe ``{"creatures": [...], "big_bad": ... }`` dict written
    by ``_stage_commit`` via ``_curated_to_payload`` (name/creature_type/
    telegraph/hp/threat_level). This is the exact "committed region_population
    mutation" AC2 requires to be byte-identical across seeded runs."""
    pops: dict[str, dict] = {}
    for m in repo.load_mutations():
        if getattr(m, "kind", None) == "region_population":
            pops[m.region_id] = m.payload
    return pops


def _authored_pack(
    source_root: Path,
    *,
    world_slug: str,
    region_ids: list[str],
    creature_id: str,
    creature_name: str,
) -> Any:
    """A duck-typed genre pack for ``resolve_room_creatures``: writes
    ``{root}/worlds/{world}/rooms/{rid}.yaml`` binding ``encounter_creatures:
    [creature_id]`` and exposes ``source_dir`` + ``effective_bestiary``. Defined
    locally (NOT imported from the 153-26 suite, whose degrade-ladder tests this
    story retires)."""
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


# ---------------------------------------------------------------------------
# AC1 — no LLM in the curate path
# ---------------------------------------------------------------------------


def test_stage_curate_signature_drops_claude_client() -> None:
    """AC1 (RED on develop): the LLM client is gone from the curate seam.

    Reflection tripwire (server CLAUDE.md sanctions ``inspect.signature`` checks
    as runtime-type interrogation, not a source read): neither ``_stage_curate``
    nor ``materialize`` may accept a ``claude_client`` parameter once the Haiku
    call is deleted. RED today (both thread it); GREEN after the deletion.
    """
    curate_params = inspect.signature(_mat._stage_curate).parameters
    assert "claude_client" not in curate_params, (
        "_stage_curate still accepts `claude_client` — Amendment C removes the "
        "LLM from the curate stage; the client argument must be gone"
    )
    materialize_params = inspect.signature(_mat.materialize).parameters
    assert "claude_client" not in materialize_params, (
        "materialize still threads `claude_client` — Amendment C drops the "
        "argument from the coordinator (and the build_llm_client(purpose='tool') "
        "that exists only to feed curate)"
    )


async def test_materialize_runs_with_no_llm_client(
    monkeypatch: pytest.MonkeyPatch, migrated_db: str
) -> None:
    """AC1 behaviour (RED on develop): the real five-stage ``materialize`` runs
    end-to-end with ZERO LLM client and commits a populated roster.

    On develop ``materialize`` raises ``TypeError`` (``claude_client`` required)
    -> RED. After the deletion it runs the pure-deterministic path and freezes
    ``region_population`` rows — proving no LLM is needed anywhere on the curate
    seam (and serving as the wiring test: real materialize -> real Pg commit ->
    real region_population read).
    """
    from tests.dungeon.conftest import build_pg_dungeon_repo

    _pool, repo, _sid = build_pg_dungeon_repo(monkeypatch, migrated_db)
    theme_id = "det_crypt_158_12"
    palette = _commit_palette(theme_id)
    graph = _seed_graph_themed(theme_id)
    request = _make_request_task3(campaign_seed=158, expansion_id=1)

    _exporter, restore = _install_in_memory_tracer()
    try:
        await _run_materialize(
            request,
            graph=graph,
            bundle=_real_cookbook_bundle(),
            palette=palette,
            dungeon_repository=repo,
            snapshot=_fresh_snapshot(),
            pack_tropes=_attach_pack("cave_in"),
        )
    finally:
        restore()

    pops = _region_populations(repo)
    assert pops, (
        "materialize committed no region_population mutations — the deterministic "
        "curate path must still freeze rosters for the generated regions"
    )
    assert any(p.get("creatures") for p in pops.values()), (
        "every committed region_population is empty — the deterministic path must "
        "translate the assemble_region manifest into creatures"
    )


# ---------------------------------------------------------------------------
# AC2 — deterministic materialization: same seed => byte-identical rosters
# ---------------------------------------------------------------------------


async def test_materialize_is_seed_deterministic(
    monkeypatch: pytest.MonkeyPatch, migrated_db: str
) -> None:
    """AC2 (RED on develop): materialize one expansion band TWICE from one
    ``campaign_seed`` into two fresh sessions and assert the committed
    ``region_population`` mutations (creatures + big_bad, incl. HP + threat) are
    byte-identical.

    RED today (no LLM-free path exists — ``materialize`` requires a client). After
    the deletion the curate stage is pure seeded compute, so the two runs must
    produce identical rosters. This reverses the ``RegionCuration`` "curation
    deliberately breaks byte-reproducibility" note in behaviour: same seed => same
    roster.
    """
    from tests.dungeon.conftest import build_pg_dungeon_repo

    def _build_inputs() -> tuple[Any, Any, Any, Any]:
        # NB: each build_pg_dungeon_repo reopens the global pool, so read the
        # committed rosters back into memory BEFORE building the next repo.
        _pool, repo, _sid = build_pg_dungeon_repo(monkeypatch, migrated_db)
        theme_id = "det_crypt_158_12"
        palette = _commit_palette(theme_id)
        graph = _seed_graph_themed(theme_id)
        request = _make_request_task3(campaign_seed=2026, expansion_id=1)
        return repo, palette, graph, request

    _exporter, restore = _install_in_memory_tracer()
    try:
        repo_a, palette_a, graph_a, request_a = _build_inputs()
        await _run_materialize(
            request_a,
            graph=graph_a,
            bundle=_real_cookbook_bundle(),
            palette=palette_a,
            dungeon_repository=repo_a,
            snapshot=_fresh_snapshot(),
            pack_tropes=_attach_pack("cave_in"),
        )
        pops_a = _region_populations(repo_a)

        repo_b, palette_b, graph_b, request_b = _build_inputs()
        await _run_materialize(
            request_b,
            graph=graph_b,
            bundle=_real_cookbook_bundle(),
            palette=palette_b,
            dungeon_repository=repo_b,
            snapshot=_fresh_snapshot(),
            pack_tropes=_attach_pack("cave_in"),
        )
        pops_b = _region_populations(repo_b)
    finally:
        restore()

    assert pops_a, "run A committed no region_population rows — cannot assert determinism"
    blob_a = json.dumps(pops_a, sort_keys=True)
    blob_b = json.dumps(pops_b, sort_keys=True)
    assert blob_a == blob_b, (
        "same campaign_seed produced DIFFERENT committed region_population "
        "rosters — materialization is not seed-reproducible.\n"
        f"run A: {blob_a}\nrun B: {blob_b}"
    )


# ---------------------------------------------------------------------------
# AC3 — the curate span is always clean: curated=true, zero failure spans
# ---------------------------------------------------------------------------


async def test_curate_span_is_clean_no_failure_spans(
    monkeypatch: pytest.MonkeyPatch, migrated_db: str
) -> None:
    """AC3 (RED on develop): driving the real ``materialize`` emits a
    ``dungeon.materialize.curate`` span stamped ``curated=true`` and ZERO
    ``dungeon.curate.parse_failed`` / ``dungeon.curate.degraded`` spans on any
    path.

    RED today: ``materialize`` raises before any span opens (``claude_client``
    required), so no clean curate span exists. After the deletion the two failure
    spans are unreachable (the retry/deadline/degrade ladder is gone) and curate
    is always clean.
    """
    from tests.dungeon.conftest import build_pg_dungeon_repo

    _pool, repo, _sid = build_pg_dungeon_repo(monkeypatch, migrated_db)
    theme_id = "det_crypt_158_12"
    palette = _commit_palette(theme_id)
    graph = _seed_graph_themed(theme_id)
    request = _make_request_task3(campaign_seed=77, expansion_id=1)

    exporter, restore = _install_in_memory_tracer()
    try:
        await _run_materialize(
            request,
            graph=graph,
            bundle=_real_cookbook_bundle(),
            palette=palette,
            dungeon_repository=repo,
            snapshot=_fresh_snapshot(),
            pack_tropes=_attach_pack("cave_in"),
        )
    finally:
        restore()

    curate_spans = _spans_named(exporter, "dungeon.materialize.curate")
    assert curate_spans, (
        "no dungeon.materialize.curate span fired — the deterministic curate "
        "stage must still open and close its stage span"
    )
    for s in curate_spans:
        attrs = dict(s.attributes or {})
        assert attrs.get("curated") is True, (
            "the curate span must be stamped curated=true on the deterministic "
            f"path (got curated={attrs.get('curated')!r}); a deterministic "
            "materialize never degrades"
        )

    assert not _spans_named(exporter, "dungeon.curate.parse_failed"), (
        "dungeon.curate.parse_failed fired — the Layer-1 retry that emits it is "
        "removed with the LLM; this span must be unreachable"
    )
    assert not _spans_named(exporter, "dungeon.curate.degraded"), (
        "dungeon.curate.degraded fired — the Layer-2 degrade that emits it is "
        "removed with the LLM; this span must be unreachable"
    )


# ---------------------------------------------------------------------------
# AC4 — authored bindings survive on the (now sole) deterministic main path
# ---------------------------------------------------------------------------


async def test_authored_binding_survives_deterministic_materialize(
    monkeypatch: pytest.MonkeyPatch, migrated_db: str, tmp_path: Path
) -> None:
    """AC4 (RED on develop): a region with an authored ``rooms/<id>.yaml``
    ``encounter_creatures`` binding (``entrance``/region -> ``gnaw_swarm``) still
    surfaces that creature under its authored name via
    ``_append_authored_creatures``, through the REAL materialize -> commit ->
    ``region_population`` path — and emits ``monster_manual.room_bound``.

    On develop ``_append_authored_creatures`` runs ONLY on the degrade path; the
    deterministic main path is unreachable (``materialize`` requires a client) ->
    RED. After the deletion it runs for EVERY region on the sole path, so the
    authored creature is frozen into the roster.
    """
    from tests.dungeon.conftest import build_pg_dungeon_repo

    # Isolation (SM-flagged gotcha): with genre "caverns_and_claudes" + a
    # synthetic world, _resolve_world_dir would write rooms/<id>.yaml into the
    # REAL content pack (gitignored -> git stays clean) and poison sibling
    # load_genre_pack tests. Redirect the emit to tmp; we assert on spans +
    # committed rosters, never the written YAMLs.
    emit_root = tmp_path / "emitted_world_dirs"
    monkeypatch.setattr(_mat, "_resolve_world_dir", lambda request: emit_root / request.world_slug)

    _pool, repo, _sid = build_pg_dungeon_repo(monkeypatch, migrated_db)
    theme_id = "authored_crypt_158_12"
    palette = _commit_palette(theme_id)
    graph = _seed_graph_themed(theme_id)

    world = "test_world"  # matches _fresh_snapshot()'s world_slug
    region_ids = [f"exp001.r{n}" for n in range(10)]
    pack = _authored_pack(
        tmp_path,
        world_slug=world,
        region_ids=region_ids,
        creature_id="gnaw_swarm",
        creature_name="Gnaw Swarm",
    )

    from sidequest.dungeon.materializer import MaterializationRequest
    from sidequest.dungeon.persistence import FrontierEdge

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

    exporter, restore = _install_in_memory_tracer()
    try:
        await _run_materialize(
            request,
            graph=graph,
            bundle=_real_cookbook_bundle(),
            palette=palette,
            dungeon_repository=repo,
            snapshot=_fresh_snapshot(),
            pack_tropes=_attach_pack("cave_in"),
            pack=pack,
        )
    finally:
        restore()

    dungeon_map = repo.load_map(entrance_id="entrance")
    generated = [nid for nid in dungeon_map.nodes if nid != "entrance"]
    assert generated, "materialize committed no generated regions — cannot probe authored binding"

    surfaced_names: set[str] = set()
    for rid in generated:
        roster, big_bad = load_region_population(repo, rid)
        surfaced_names.update(c.name for c in roster)
        if big_bad is not None:
            surfaced_names.add(big_bad.name)

    assert "Gnaw Swarm" in surfaced_names, (
        "the authored encounter_creatures binding (gnaw_swarm -> 'Gnaw Swarm') "
        "did not survive deterministic materialization; _append_authored_creatures "
        f"must run on the main path. Surfaced: {sorted(surfaced_names)}"
    )
    assert _spans_named(exporter, "monster_manual.room_bound"), (
        "consulting the authored binding on the deterministic main path must emit "
        "monster_manual.room_bound so the GM panel SEES authored content survived"
    )


# ---------------------------------------------------------------------------
# AC5 — CurationError carve-out (i) retained (GREEN regression guard)
# ---------------------------------------------------------------------------


def test_curation_error_carveout_i_retained() -> None:
    """AC5 (GREEN on develop; must STAY GREEN): a structurally-invalid *assembled*
    manifest still raises ``CurationError`` and fails loud — a real upstream
    content bug is never degraded into shipped content. Carve-out (i) survives
    the refactor; carve-out (ii) (curated row dropped 'cr') disappears with the
    verdict path.

    This is a regression guard, not a RED test: ``_creatures_from_manifest`` is
    the function the deterministic path now uses for EVERY region, so its loud
    rejection of a corrupt manifest must not be softened while removing the LLM.
    """
    from sidequest.game.cookbook.models import RegionContentManifest

    bundle = _real_cookbook_bundle()

    # (a) a wandering row missing 'cr' — cannot CR->HP translate.
    bad_row_manifest = RegionContentManifest(
        race="goblin",
        cr_band="cr_low",
        size_budget={},
        wandering_table=[{"name": "Skulker", "type": "humanoid"}],  # no 'cr'
        loot_table=[],
        special_rooms=[],
        big_bad=None,
    )
    with pytest.raises(CurationError):
        _mat._creatures_from_manifest(bad_row_manifest, bundle)

    # (b) a big_bad with no 'cr' under an unknown cr_band — cannot resolve a band
    # ceiling, so the CR->HP seam is impossible. Still loud, never degraded.
    bad_band_manifest = RegionContentManifest(
        race="goblin",
        cr_band="cr_does_not_exist",
        size_budget={},
        wandering_table=[],
        loot_table=[],
        special_rooms=[],
        big_bad={"name": "The Maw", "min_band": "cr_high"},  # no 'cr'
    )
    with pytest.raises(CurationError):
        _mat._creatures_from_manifest(bad_band_manifest, bundle)


# ---------------------------------------------------------------------------
# AC4 (extension) — a BROKEN authored binding on the (now sole) deterministic
#   main path is loud-but-GRACEFUL: it must NOT crash the player-facing connect.
#   Ports the live Reviewer-153-26-HIGH invariant off the retired degrade path.
# ---------------------------------------------------------------------------


def _authored_pack_dangling(source_root: Path, *, world_slug: str, region_ids: list[str]) -> Any:
    """A pack whose rooms bind an id ABSENT from the bestiary (a ``gnaw_swarm`` ->
    ``gnaw_swrm`` typo). ``resolve_room_creatures`` raises ``RoomCreatureBindingError``."""
    rooms_dir = source_root / "worlds" / world_slug / "rooms"
    rooms_dir.mkdir(parents=True, exist_ok=True)
    for rid in region_ids:
        (rooms_dir / f"{rid}.yaml").write_text(
            f"id: {rid}\nencounter_creatures:\n- gnaw_swrm\n",  # typo: not in bestiary
            encoding="utf-8",
        )
    bestiary = Bestiary(
        entries=[
            BestiaryEntry(
                id="gnaw_swarm", name="Gnaw Swarm", level=1, hp=8, armor_class=12, attack_bonus=1
            )
        ]
    )

    class _DanglingPack:
        source_dir = source_root

        def effective_bestiary(self, world: str | None) -> tuple[Bestiary, str]:
            return bestiary, (world or "")

    return _DanglingPack()


async def test_broken_authored_binding_stays_loud_but_graceful(
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A region whose authored ``rooms/<id>.yaml`` binds a DANGLING bestiary id (a
    homebrew typo) must stay loud-but-GRACEFUL on the deterministic main path: the
    real ``materialize`` must NOT raise (no crashed connect), it must emit
    ``dungeon.curate.authored_bind_failed`` (GM-panel lie-detector) + log ERROR,
    and the region still ships its procedural creatures. Ports the live
    Reviewer-153-26-HIGH invariant onto the post-Amendment-C main path.
    """
    from tests.dungeon.conftest import build_pg_dungeon_repo

    emit_root = tmp_path / "emitted_world_dirs"
    monkeypatch.setattr(_mat, "_resolve_world_dir", lambda request: emit_root / request.world_slug)

    _pool, repo, _sid = build_pg_dungeon_repo(monkeypatch, migrated_db)
    theme_id = "dangling_crypt_158_12"
    palette = _commit_palette(theme_id)
    graph = _seed_graph_themed(theme_id)
    world = "test_world"
    pack = _authored_pack_dangling(
        tmp_path, world_slug=world, region_ids=[f"exp001.r{n}" for n in range(10)]
    )

    from sidequest.dungeon.materializer import MaterializationRequest
    from sidequest.dungeon.persistence import FrontierEdge

    fe = FrontierEdge(
        frontier_edge_id="fe1", from_region_id="entrance", heading="north", spawn_depth_score=15.0
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

    exporter, restore = _install_in_memory_tracer()
    try:
        with caplog.at_level(logging.ERROR):
            # MUST NOT raise — the broken binding is caught and curate proceeds.
            await _run_materialize(
                request,
                graph=graph,
                bundle=_real_cookbook_bundle(),
                palette=palette,
                dungeon_repository=repo,
                snapshot=_fresh_snapshot(),
                pack_tropes=_attach_pack("cave_in"),
                pack=pack,
            )
    finally:
        restore()

    assert _spans_named(exporter, "dungeon.curate.authored_bind_failed"), (
        "a dangling authored binding caught on the main path must emit "
        "dungeon.curate.authored_bind_failed so the GM panel SEES the dropped content"
    )
    assert any(r.levelno >= logging.ERROR for r in caplog.records), (
        "the caught binding error must log LOUD at ERROR level"
    )
    # The region still ships procedural coal — graceful, not an empty crash.
    dungeon_map = repo.load_map(entrance_id="entrance")
    generated = [nid for nid in dungeon_map.nodes if nid != "entrance"]
    assert generated, "materialize committed no generated regions (the connect crashed?)"
    surfaced: set[str] = set()
    for rid in generated:
        roster, _bb = load_region_population(repo, rid)
        surfaced.update(c.name for c in roster)
    assert "Gnaw Swarm" not in surfaced, "the dangling-bound creature must not appear"
    assert surfaced, "the region must still ship its procedural creatures despite the bad binding"


async def test_malformed_authored_room_yaml_stays_loud_but_graceful(
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Sibling of the dangling-id case (Reviewer-153-26 round-2 HIGH), on the
    deterministic main path: a MALFORMED ``rooms/<id>.yaml`` (a homebrew YAML typo)
    must also stay loud-but-GRACEFUL — ``materialize`` must NOT crash, it emits
    ``dungeon.curate.authored_bind_failed`` + logs ERROR, and ships procedural coal.
    """
    from tests.dungeon.conftest import build_pg_dungeon_repo

    emit_root = tmp_path / "emitted_world_dirs"
    monkeypatch.setattr(_mat, "_resolve_world_dir", lambda request: emit_root / request.world_slug)
    world = "test_world"
    rooms_dir = tmp_path / "worlds" / world / "rooms"
    rooms_dir.mkdir(parents=True, exist_ok=True)
    for n in range(10):
        (rooms_dir / f"exp001.r{n}.yaml").write_text(
            "encounter_creatures: [gnaw_swarm, broken\n",  # unterminated → YAMLError
            encoding="utf-8",
        )
    bestiary = Bestiary(
        entries=[
            BestiaryEntry(
                id="gnaw_swarm", name="Gnaw Swarm", level=1, hp=8, armor_class=12, attack_bonus=1
            )
        ]
    )

    class _MalformedPack:
        source_dir = tmp_path

        def effective_bestiary(self, w: str | None) -> tuple[Bestiary, str]:
            return bestiary, (w or "")

    _pool, repo, _sid = build_pg_dungeon_repo(monkeypatch, migrated_db)
    theme_id = "malformed_crypt_158_12"
    palette = _commit_palette(theme_id)
    graph = _seed_graph_themed(theme_id)

    from sidequest.dungeon.materializer import MaterializationRequest
    from sidequest.dungeon.persistence import FrontierEdge

    fe = FrontierEdge(
        frontier_edge_id="fe1", from_region_id="entrance", heading="north", spawn_depth_score=15.0
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

    exporter, restore = _install_in_memory_tracer()
    try:
        with caplog.at_level(logging.ERROR):
            await _run_materialize(
                request,
                graph=graph,
                bundle=_real_cookbook_bundle(),
                palette=palette,
                dungeon_repository=repo,
                snapshot=_fresh_snapshot(),
                pack_tropes=_attach_pack("cave_in"),
                pack=_MalformedPack(),
            )
    finally:
        restore()

    assert _spans_named(exporter, "dungeon.curate.authored_bind_failed"), (
        "a malformed room YAML caught on the main path must emit "
        "dungeon.curate.authored_bind_failed (not crash the connect)"
    )
    assert any(r.levelno >= logging.ERROR for r in caplog.records), (
        "the caught YAML error must log LOUD at ERROR level"
    )
    assert [nid for nid in repo.load_map(entrance_id="entrance").nodes if nid != "entrance"], (
        "materialize must still commit generated regions (no crashed connect)"
    )
