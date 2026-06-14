"""Story 107-1 — the render helper can SOURCE a procedurally-materialized room,
so each room entry can produce a fresh, distinct LOCATION_DESCRIPTION.

Epic 107 forensics (2026-06-13): descending beneath_sunden read as ONE scene —
the render pipeline (ADR-109/ADR-050) under-fired, "fewer illustrations than
rooms traversed". A procedural ADR-106 room has a region id like ``exp001.r1``
that is NOT a static room YAML shipped in the world and NOT an authored
cartography region — so the open question was whether
``_maybe_emit_location_description`` could source it at all.

This test answers exactly that, through REAL components:

  materializer emit (room_yaml_emit.write_room_yaml)
    → load_room_payload (the per-room YAML source path)
    → _maybe_emit_location_description (the render-trigger helper)

What GREEN here proves: the helper resolves a materialized procedural room's
``rooms/<region_id>.yaml`` and emits a distinct LocationDescriptionMessage per
room (distinct region_id + prose). The ADR-106 YAML round-trip works and the
descent's rooms are individually sourceable — no single frozen scene.

What GREEN here does NOT prove (and is deliberately out of scope):
  - It does NOT exercise the production turn-handler gate that DECIDES when to
    call the helper (the ``_is_region_mode_world`` + ``_region_changed`` branch
    in websocket_session_handler.py:2257-2287). This test calls the helper
    directly with ``room_id_override``, i.e. as the handler would AFTER its gate
    passes. Confirming the gate fires per room on a live descent is the
    epic-107 "verify" step — a headless sq-playtest counting per-room
    LOCATION_DESCRIPTION OTEL emits (carried as a delivery finding).

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

_UNSET = object()


def _materialize_procedural_rooms(world_dir: Path) -> None:
    """Emit two procedural room YAMLs using the same ``write_room_yaml`` seam the
    ADR-106 materializer uses. This exercises the YAML write/read shape only — NOT
    the full materializer pipeline (rolling, set-piece/trope/quest attach)."""
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


def _sd(*, cartography=_UNSET):
    """Synthetic _SessionData. ``cartography`` defaults to a harmless MagicMock
    (the positive path never reaches the cartography fallback because the room
    YAML loads). Pass ``cartography=None`` for the no-source case so the fallback
    finds nothing instead of a phantom MagicMock region."""
    sd = MagicMock()
    sd.genre_slug = "caverns_and_claudes"
    sd.world_slug = "beneath_sunden"
    sd.player_id = ""
    world = MagicMock()
    if cartography is not _UNSET:
        world.cartography = cartography
    sd.genre_pack = MagicMock()
    sd.genre_pack.worlds = {"beneath_sunden": world}
    return sd


def _emit_for_region(sd, region_id: str):
    """Call the emit helper directly with a synthetic snapshot, as the turn
    handler would call it AFTER its region-change gate passes
    (room_id_override = the newly-entered region id). The gate itself
    (navigation_mode check + current_region diff) is not exercised here — see the
    module docstring."""
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

    # Two successive room entries → one helper call per room (as the handler would
    # issue after each current_region change).
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

    # A FRESH scene per room — distinct region ids + distinct prose, not one frozen
    # scene reused for the whole descent.
    assert msg1.payload.region_id == "exp001.r1"
    assert msg2.payload.region_id == "exp001.r2"
    assert "dropmouth shaft" in msg1.payload.prose
    assert "wider chamber" in msg2.payload.prose


@pytest.mark.integration
def test_unmaterialized_room_emits_nothing(tmp_path, monkeypatch):
    """No-source guard (the exact under-fire failure mode): a region with no
    materialized YAML and no cartography region must emit ZERO messages — not one
    blank LocationDescriptionMessage. ``cartography=None`` ensures the fallback
    finds nothing (a default MagicMock world would fake a region and mask this)."""
    genre_root = tmp_path / "caverns_and_claudes"
    world_dir = genre_root / "worlds" / "beneath_sunden"
    world_dir.mkdir(parents=True)
    _materialize_procedural_rooms(world_dir)  # r1/r2 exist; r99 does not
    _patch_genre_loader_find(monkeypatch, genre_root)

    sd = _sd(cartography=None)

    result = _emit_for_region(sd, "exp001.r99")
    assert result == [], (
        "an unmaterialized room must emit nothing (no_source guard) — emitting a "
        "blank LOCATION_DESCRIPTION would be a silent under-fire"
    )
