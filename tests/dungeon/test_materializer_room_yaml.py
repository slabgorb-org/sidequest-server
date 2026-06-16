"""Materializer writes <world>/rooms/<id>.yaml per region (Story 55-1).

Covers AC-9 (per-region YAML emit inside _stage_commit, freeze invariant
on existing files, empty-input no-op) and AC-11 (wiring proof — the
helper has a non-test caller inside _stage_commit, not just a
standalone unit-test seam).
"""

from __future__ import annotations

from pathlib import Path

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


# ---------------------------------------------------------------------------
# AC-11: wiring — _stage_emit_room_yamls has a non-test caller
# ---------------------------------------------------------------------------


def test_emit_helper_has_a_caller_in_production_code() -> None:
    """CLAUDE.md 'Every Test Suite Needs a Wiring Test': the helper must
    be CALLED FROM _stage_commit in the production materializer, not
    just exist as a free function exercised by these tests.

    def + at least one call site = ≥2 mentions of the symbol in
    materializer.py.
    """
    src = (
        Path(__file__).resolve().parents[2] / "sidequest" / "dungeon" / "materializer.py"
    ).read_text()
    assert "def _stage_emit_room_yamls(" in src, (
        "_stage_emit_room_yamls must be defined in materializer.py."
    )
    assert src.count("_stage_emit_room_yamls(") >= 2, (
        "_stage_emit_room_yamls must have a non-test caller in "
        "materializer.py (def + call = ≥2 mentions). The wiring test "
        "exists to prove the helper is reachable from production code."
    )


def test_emit_call_site_is_inside_stage_commit() -> None:
    """Sharper wiring claim: the call site lives inside _stage_commit's
    body (not in a sibling helper that isn't actually invoked during a
    real materialization)."""
    src = (
        Path(__file__).resolve().parents[2] / "sidequest" / "dungeon" / "materializer.py"
    ).read_text()

    # Find _stage_commit's body by locating its def and the next top-level def.
    start = src.find("def _stage_commit(")
    assert start >= 0, "_stage_commit must exist in materializer.py."
    after_start = src[start + len("def _stage_commit(") :]
    next_def = after_start.find("\ndef ")
    body = after_start if next_def < 0 else after_start[:next_def]
    assert "_stage_emit_room_yamls(" in body, (
        "_stage_emit_room_yamls must be CALLED from inside _stage_commit's "
        "body (not just defined elsewhere). Without this call site the "
        "production materializer never emits the room YAMLs."
    )


def test_emit_runs_after_conn_commit() -> None:
    """Freeze invariant has a temporal dimension: the YAML write must run
    AFTER conn.commit() so a rolled-back expansion never produces orphan
    YAMLs on disk. In source-order terms inside _stage_commit, the
    emit call site must appear strictly after the conn.commit() line."""
    src = (
        Path(__file__).resolve().parents[2] / "sidequest" / "dungeon" / "materializer.py"
    ).read_text()
    start = src.find("def _stage_commit(")
    assert start >= 0
    after_start = src[start:]
    next_def = after_start[len("def _stage_commit(") :].find("\ndef ")
    body = after_start if next_def < 0 else after_start[: len("def _stage_commit(") + next_def]

    commit_idx = body.find("conn.commit()")
    emit_idx = body.find("_stage_emit_room_yamls(")
    assert commit_idx >= 0, "conn.commit() must remain inside _stage_commit."
    assert emit_idx >= 0, "_stage_emit_room_yamls() must be called from inside _stage_commit."
    assert emit_idx > commit_idx, (
        "AC-9 ordering: _stage_emit_room_yamls must be called AFTER "
        "conn.commit() so a rolled-back expansion produces no orphan "
        "YAMLs on disk. Found emit at offset "
        f"{emit_idx}, commit at offset {commit_idx}."
    )
