"""Story 107-1 end-to-end: a fresh LOCATION_DESCRIPTION fires per procedural
room entry as the party descends the ADR-106 dungeon.

Epic 107 forensics (2026-06-13): descending beneath_sunden read as ONE scene —
the render pipeline (ADR-109/ADR-050, keyed off the per-room scene change)
under-fired, "fewer illustrations than rooms traversed". This test verifies the
full per-room render chain end-to-end through REAL components:

  materializer emit (room_yaml_emit.write_room_yaml)
    → load_room_payload (the per-room YAML source path)
    → _maybe_emit_location_description (the render trigger helper)

A successful in-dungeon move advances ``current_region`` (proven separately at
the movement-dispatch layer); the turn handler's region-mode render gate fires
``_maybe_emit_location_description(room_id_override=current_region)`` on that
change. The open question this test answers: does the helper actually SOURCE a
procedurally-materialized room (region ids like ``exp001.r1`` — NOT a static
room YAML, NOT an authored cartography region), so a distinct LOCATION_DESCRIPTION
goes out for EACH room entered rather than a single frozen scene?

If this is GREEN, the per-room render is wired (#835 + region machinery closed
the loop). If RED, the helper has no source path for procedural rooms and the
descent still under-fires — the genuine remaining 107-1 gap.

Marked integration: drives the real loader + emit helper against a
materializer-shaped tmp world tree.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sidequest.dungeon.room_yaml_emit import write_room_yaml
from sidequest.protocol.messages import LocationDescriptionMessage
from sidequest.protocol.models import LocationEntity


def _materialize_procedural_rooms(world_dir: Path) -> None:
    """Emit two procedural rooms exactly as the ADR-106 materializer does —
    via the real ``write_room_yaml`` seam, keyed by procedural region ids."""
    write_room_yaml(
        world_dir=world_dir,
        room_id="exp001.r1",
        description="A dropmouth shaft opens onto a dripping ledge of black stone.",
        entities=[
            LocationEntity(id="black_stone_ledge", label="a dripping ledge", tier="flavor_only"),
        ],
    )
    write_room_yaml(
        world_dir=world_dir,
        room_id="exp001.r2",
        description="The wider chamber yawns; bones crunch underfoot in the dark.",
        entities=[
            LocationEntity(id="bone_litter", label="a litter of bones", tier="flavor_only"),
        ],
    )


def _patch_genre_loader_find(monkeypatch, genre_root: Path) -> None:
    from sidequest.genre import loader as loader_mod

    def _fake_find(self, slug):  # noqa: ARG001
        return genre_root

    monkeypatch.setattr(loader_mod.GenreLoader, "find", _fake_find)


def _sd():
    sd = MagicMock()
    sd.genre_slug = "caverns_and_claudes"
    sd.world_slug = "beneath_sunden"
    sd.player_id = ""
    sd.genre_pack = MagicMock()
    sd.genre_pack.worlds = {"beneath_sunden": MagicMock()}
    return sd


def _emit_for_region(sd, region_id: str):
    """Drive the real render helper the way the turn handler's region-mode gate
    does on a region change: room_id_override = the newly-entered region id."""
    from sidequest.server.websocket_session_handler import (
        _maybe_emit_location_description,
    )

    emitted: list[object] = []

    def capture(msg, kind):  # noqa: ARG001
        emitted.append(msg)

    _maybe_emit_location_description(
        MagicMock(),
        sd=sd,
        snapshot=MagicMock(character_locations={}),
        actor=None,
        emit_fn=capture,
        room_id_override=region_id,
    )
    return emitted


@pytest.mark.integration
def test_each_procedural_room_entry_emits_a_fresh_location_description(tmp_path, monkeypatch):
    genre_root = tmp_path / "caverns_and_claudes"
    world_dir = genre_root / "worlds" / "beneath_sunden"
    world_dir.mkdir(parents=True)
    _materialize_procedural_rooms(world_dir)
    _patch_genre_loader_find(monkeypatch, genre_root)

    sd = _sd()

    # Descent: the party enters exp001.r1, then exp001.r2. Each entry is a
    # current_region change → one render-gate fire per room.
    first = _emit_for_region(sd, "exp001.r1")
    second = _emit_for_region(sd, "exp001.r2")

    assert len(first) == 1, (
        "no LOCATION_DESCRIPTION emitted for the first procedural room — the "
        "render helper cannot source a materialized ADR-106 room (the descent "
        "under-fires per the epic-107 forensics)"
    )
    assert len(second) == 1, "no LOCATION_DESCRIPTION emitted for the second procedural room"

    msg1, msg2 = first[0], second[0]
    assert isinstance(msg1, LocationDescriptionMessage)
    assert isinstance(msg2, LocationDescriptionMessage)

    # A FRESH scene per room — distinct region ids + distinct prose, not one
    # frozen scene reused for the whole descent.
    assert msg1.payload.region_id == "exp001.r1"
    assert msg2.payload.region_id == "exp001.r2"
    assert msg1.payload.region_id != msg2.payload.region_id
    assert "dropmouth shaft" in msg1.payload.prose
    assert "wider chamber" in msg2.payload.prose
