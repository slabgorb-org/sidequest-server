"""Tests for Task 2: CuratedCreature.threat_level derived from CR band.

Keith ruling 2026-06-22: derive a 1-4 B/X threat tier from the region's CR
band — ordinal of the band in affinities.cr_bands, clamped to [1, 4].
Big-bad gets region tier + 1 (capped at 4). ADR-114: no raw cr on
CuratedCreature.
"""
from pathlib import Path

import pytest

from sidequest.dungeon.materializer import CuratedCreature, CurationError, _threat_from_band
from sidequest.game.cookbook.loader import load_cookbook

# Discovery mirrors test_materializer.py: parents[3] is the orchestrator root.
_BENEATH_SUNDEN_WORLD = (
    Path(__file__).resolve().parents[3]
    / "sidequest-content/genre_packs/caverns_and_claudes/worlds/beneath_sunden"
)


def _bundle():
    """Load the real beneath_sunden cookbook — established pattern from
    test_materializer.py::_real_cookbook_bundle(). No Postgres needed."""
    return load_cookbook(_BENEATH_SUNDEN_WORLD)


def test_threat_from_band_shallow_is_tier_one():
    bundle = _bundle()
    shallow = bundle.affinities.cr_bands[0].id
    assert _threat_from_band(bundle, shallow) == 1


def test_threat_from_band_clamps_to_four():
    bundle = _bundle()
    deepest = bundle.affinities.cr_bands[-1].id
    assert _threat_from_band(bundle, deepest) == min(4, len(bundle.affinities.cr_bands))


def test_threat_from_band_unknown_raises():
    bundle = _bundle()
    with pytest.raises(CurationError, match="cr_band"):
        _threat_from_band(bundle, "nonexistent_band_xyz")


def test_curated_creature_has_threat_level_field():
    from sidequest.game.creature_core import hp_pool_from_hp

    c = CuratedCreature(
        name="x",
        creature_type="t",
        telegraph="g",
        hp=hp_pool_from_hp(4),
        threat_level=2,
    )
    assert c.threat_level == 2


# ---------------------------------------------------------------------------
# Task 3: freeze curated region population to the dungeon store at commit.
# ---------------------------------------------------------------------------


def test_curated_to_payload_round_trips_shape():
    from sidequest.dungeon.materializer import _curated_to_payload
    from sidequest.game.creature_core import hp_pool_from_hp

    c = CuratedCreature(
        name="Gnaw-Swarm",
        creature_type="swarm",
        telegraph="chittering",
        hp=hp_pool_from_hp(6),
        threat_level=1,
    )
    p = _curated_to_payload(c)
    assert p == {
        "name": "Gnaw-Swarm",
        "creature_type": "swarm",
        "telegraph": "chittering",
        "hp": c.hp.model_dump(),
        "threat_level": 1,
    }


# ---------------------------------------------------------------------------
# Task 3 wiring test: region_population rows persisted by materialize()
# ---------------------------------------------------------------------------


async def test_region_population_rows_land_in_dungeon_store(
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: str,
) -> None:
    """Drive the real materialize() coordinator and assert that
    ``region_population`` mutation rows were committed to the dungeon store,
    each with a ``creatures`` list whose entries carry ``threat_level``.

    Non-circular: the test never calls ``_curated_to_payload`` or
    ``_stage_commit`` directly — it calls ``materialize()`` and inspects
    ``repo.load_mutations()``.  If ``_stage_commit`` does not write
    ``region_population`` rows the assertion fails."""
    import sidequest.telemetry.spans as _spans_module
    from sidequest.dungeon.materializer import materialize
    from tests.dungeon.conftest import build_pg_dungeon_repo
    from tests.dungeon.test_materializer import (
        _attach_pack,
        _commit_palette,
        _fresh_snapshot,
        _make_request_task3,
        _otel_in_memory,
        _real_cookbook_bundle,
        _reflecting_sdk_client,
        _seed_graph_themed,
    )

    _pool, repo, _sid = build_pg_dungeon_repo(monkeypatch, migrated_db)

    bundle = _real_cookbook_bundle()
    theme_id = "pop_wiring_crypt"
    palette = _commit_palette(theme_id)
    graph = _seed_graph_themed(theme_id)

    _exporter, _provider, real_tracer = _otel_in_memory()
    original_tracer_fn = _spans_module.tracer
    _spans_module.tracer = lambda: real_tracer  # type: ignore[method-assign]
    try:
        req = _make_request_task3()
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

    pops = [m for m in repo.load_mutations() if m.kind == "region_population"]
    assert pops, (
        "no region_population rows were persisted — "
        "_stage_commit is not writing the curated roster (Task 3 wiring broken)"
    )
    sample = pops[0].payload
    assert "creatures" in sample and isinstance(sample["creatures"], list), (
        f"region_population payload missing 'creatures' list: {sample!r}"
    )
    assert all("threat_level" in c for c in sample["creatures"]), (
        f"some creature entries missing 'threat_level': {sample['creatures']!r}"
    )
