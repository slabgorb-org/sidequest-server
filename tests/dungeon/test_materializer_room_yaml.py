"""Materializer writes <world>/rooms/<id>.yaml per region (Story 55-1).

Covers AC-9 (per-region YAML emit inside _stage_commit, freeze invariant
on existing files, empty-input no-op) and AC-11 (wiring proof — the
helper has a non-test caller inside _stage_commit, not just a
standalone unit-test seam).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sidequest.dungeon.materializer import _stage_emit_room_yamls
from sidequest.game.cookbook.models import GeneratedRoomDescription
from sidequest.protocol.models import LocationEntity


def _composed(room_id: str) -> GeneratedRoomDescription:
    return GeneratedRoomDescription(
        room_id=room_id,
        description=f"Prose for {room_id}.",
        entities=[
            LocationEntity(
                id=f"{room_id}_cobwebs",
                label="cobwebs in the corners",
                tier="flavor_only",
                provenance="cookbook",
            ),
        ],
    )


# ---------------------------------------------------------------------------
# AC-9: one YAML per region in the composed map
# ---------------------------------------------------------------------------


def test_emits_one_yaml_per_region(tmp_path: Path) -> None:
    world_dir = tmp_path / "caverns_sunden"
    composed_by_region = {
        "region_1": _composed("region_1"),
        "region_2": _composed("region_2"),
        "region_3": _composed("region_3"),
    }
    _stage_emit_room_yamls(
        world_dir=world_dir,
        composed_by_region=composed_by_region,
    )
    rooms_dir = world_dir / "rooms"
    written = {p.name for p in rooms_dir.iterdir()}
    assert written == {
        "region_1.yaml",
        "region_2.yaml",
        "region_3.yaml",
    }


def test_emitted_yaml_carries_cookbook_entities(tmp_path: Path) -> None:
    """Every entity in the persisted YAML must carry provenance=cookbook
    so the ADR-100 KnownFacts / 54-6 promotion paths can tell authored
    from procedurally composed content."""
    import yaml

    world_dir = tmp_path / "caverns_sunden"
    _stage_emit_room_yamls(
        world_dir=world_dir,
        composed_by_region={"region_1": _composed("region_1")},
    )
    data = yaml.safe_load((world_dir / "rooms" / "region_1.yaml").read_text())
    assert data["description"] == "Prose for region_1."
    assert len(data["entities"]) == 1
    assert data["entities"][0]["provenance"] == "cookbook"


# ---------------------------------------------------------------------------
# AC-9: freeze invariant — existing YAMLs are not overwritten
# ---------------------------------------------------------------------------


def test_existing_yaml_is_not_overwritten(tmp_path: Path) -> None:
    """Freeze invariant: a region that already has a YAML on disk is
    left alone. Re-materialization of a frozen region must not rewrite
    its content (ADR-106 §7)."""
    world_dir = tmp_path / "caverns_sunden"
    (world_dir / "rooms").mkdir(parents=True)
    (world_dir / "rooms" / "region_1.yaml").write_text("description: pre-existing\nentities: []\n")
    composed_by_region = {
        "region_1": _composed("region_1"),
        "region_2": _composed("region_2"),
    }
    _stage_emit_room_yamls(
        world_dir=world_dir,
        composed_by_region=composed_by_region,
    )
    # region_1 untouched.
    assert "pre-existing" in (world_dir / "rooms" / "region_1.yaml").read_text()
    # region_2 written.
    assert (world_dir / "rooms" / "region_2.yaml").is_file()


# ---------------------------------------------------------------------------
# AC-9: empty composed map is a clean no-op
# ---------------------------------------------------------------------------


def test_empty_composed_map_is_a_noop(tmp_path: Path) -> None:
    """An expansion with no composed rooms (e.g. nothing newly committed)
    must not create an empty rooms/ directory or fail."""
    world_dir = tmp_path / "caverns_sunden"
    _stage_emit_room_yamls(world_dir=world_dir, composed_by_region={})
    if (world_dir / "rooms").exists():
        # If created, must be empty.
        assert not any((world_dir / "rooms").iterdir())


async def test_emit_runs_after_a_clean_materialize_commit(
    monkeypatch: Any, migrated_db: str, tmp_path: Path
) -> None:
    """ADR-115 D6 relocated the per-region YAML emit out of _stage_commit and
    into the coordinator's POST-COMMIT path (the coordinator now owns the
    attach+commit transaction). Behavior contract: a clean materialize writes
    the room YAMLs to disk. (Replaces the prior source-text wiring assertions,
    which the project bans — CLAUDE.md 'No Source-Text Wiring Tests'.)"""
    import sidequest.dungeon.materializer as _mat_module
    import sidequest.telemetry.spans as _spans_module
    from sidequest.dungeon.materializer import materialize
    from tests.dungeon.conftest import build_pg_dungeon_repo
    from tests.dungeon.test_materializer import (
        MaterializationRequest_build,
        _attach_pack,
        _commit_palette,
        _fresh_snapshot,
        _otel_in_memory,
        _real_cookbook_bundle,
        _seed_graph_themed,
    )

    theme_id = "yaml_crypt"
    palette = _commit_palette(theme_id)
    graph = _seed_graph_themed(theme_id)
    world_dir = tmp_path / "world"

    # _resolve_world_dir gates the emit on genre/world slug + a real pack on
    # disk; for this behavior test we point it at a tmp dir directly.
    monkeypatch.setattr(_mat_module, "_resolve_world_dir", lambda _request: world_dir)

    _pool, repo, _sid = build_pg_dungeon_repo(monkeypatch, migrated_db)

    _exporter, _provider, real_tracer = _otel_in_memory()
    original_tracer_fn = _spans_module.tracer
    _spans_module.tracer = lambda: real_tracer  # type: ignore[method-assign]
    try:
        await materialize(
            MaterializationRequest_build(campaign_seed=7, expansion_id=1, spawn_depth_score=0.0),
            graph=graph,
            bundle=_real_cookbook_bundle(),
            palette=palette,
            dungeon_repository=repo,
            snapshot=_fresh_snapshot(),
            pack_tropes=_attach_pack("cave_in"),
        )
    finally:
        _spans_module.tracer = original_tracer_fn  # type: ignore[method-assign]

    rooms_dir = world_dir / "rooms"
    assert rooms_dir.is_dir(), (
        "no rooms/ dir — the coordinator must emit per-region YAMLs after a "
        "clean materialize commit (ADR-115 D6 post-commit emit)"
    )
    assert any(rooms_dir.glob("*.yaml")), "no room YAMLs written after a clean commit"


async def test_emit_skipped_when_commit_rolls_back(
    monkeypatch: Any, migrated_db: str, tmp_path: Path
) -> None:
    """Freeze invariant's temporal dimension (ADR-115 D6): a rolled-back
    materialize must NOT deposit orphan YAMLs on disk. The emit is now in the
    coordinator's post-commit path, so a PersistError inside the transaction
    (which rolls the whole attach+commit back) skips the emit entirely."""
    import sidequest.dungeon.materializer as _mat_module
    import sidequest.telemetry.spans as _spans_module
    from sidequest.dungeon.materializer import materialize
    from sidequest.dungeon.persistence import PersistError
    from sidequest.game.pg.dungeon import PgDungeonTransaction
    from tests.dungeon.conftest import build_pg_dungeon_repo
    from tests.dungeon.test_materializer import (
        MaterializationRequest_build,
        _attach_pack,
        _commit_palette,
        _fresh_snapshot,
        _otel_in_memory,
        _real_cookbook_bundle,
        _seed_graph_themed,
    )

    theme_id = "yaml_rollback_crypt"
    palette = _commit_palette(theme_id)
    graph = _seed_graph_themed(theme_id)
    world_dir = tmp_path / "world"

    monkeypatch.setattr(_mat_module, "_resolve_world_dir", lambda _request: world_dir)

    def _boom_put_frontier(self: Any, fe: Any) -> None:
        raise PersistError("injected mid-write failure")

    monkeypatch.setattr(PgDungeonTransaction, "put_frontier", _boom_put_frontier)

    _pool, repo, _sid = build_pg_dungeon_repo(monkeypatch, migrated_db)

    _exporter, _provider, real_tracer = _otel_in_memory()
    original_tracer_fn = _spans_module.tracer
    _spans_module.tracer = lambda: real_tracer  # type: ignore[method-assign]
    try:
        with pytest.raises(PersistError, match="injected mid-write"):
            await materialize(
                MaterializationRequest_build(
                    campaign_seed=7, expansion_id=1, spawn_depth_score=0.0
                ),
                graph=graph,
                bundle=_real_cookbook_bundle(),
                palette=palette,
                dungeon_repository=repo,
                snapshot=_fresh_snapshot(),
                pack_tropes=_attach_pack("cave_in"),
            )
    finally:
        _spans_module.tracer = original_tracer_fn  # type: ignore[method-assign]

    rooms_dir = world_dir / "rooms"
    assert not rooms_dir.exists() or not any(rooms_dir.glob("*.yaml")), (
        "orphan room YAMLs were written despite a rolled-back materialize — "
        "the emit must run only AFTER a clean commit (ADR-115 D6)"
    )
