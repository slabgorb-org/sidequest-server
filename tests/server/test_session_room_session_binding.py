"""SessionRoom.session lifecycle tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.game.persistence import GameMode, SqliteStore
from sidequest.game.session import GameSnapshot
from sidequest.server.session import Session
from sidequest.server.session_room import SessionRoom


def _make_room(slug: str = "test_world") -> SessionRoom:
    return SessionRoom(slug=slug, mode=GameMode.SOLO)


def test_session_property_raises_before_bind_world():
    room = _make_room()
    with pytest.raises(RuntimeError, match="Session not yet bound"):
        _ = room.session


def test_session_property_returns_session_after_bind_world(tmp_path: Path):
    room = _make_room()
    snap = GameSnapshot()
    store = SqliteStore(tmp_path / "t.db")
    room.bind_world(snapshot=snap, store=store)
    assert isinstance(room.session, Session)
    # Same snapshot reference — Session reads through.
    assert room.session is room.session  # property is stable post-bind
    snap.clock_t_hours = 5.0
    assert room.session.clock.t_hours == 5.0


def test_session_advance_via_room_persists_to_room_snapshot(tmp_path: Path):
    """Advancing via room.session writes to room._snapshot."""
    from sidequest.orbital.beats import StoryBeat, StoryBeatKind

    room = _make_room()
    snap = GameSnapshot()
    store = SqliteStore(tmp_path / "t.db")
    room.bind_world(snapshot=snap, store=store)

    room.session.advance_via_beat(StoryBeat(kind=StoryBeatKind.ENCOUNTER, trigger="test"))
    # The room's snapshot is the canonical reference.
    assert room.snapshot is snap
    assert snap.clock_t_hours == 1.0


def test_bind_world_is_idempotent_on_session(tmp_path: Path):
    """Second bind_world call (idempotent per existing semantics) does not rebuild Session."""
    room = _make_room()
    snap = GameSnapshot()
    store = SqliteStore(tmp_path / "t.db")
    room.bind_world(snapshot=snap, store=store)
    s1 = room.session
    # Second call is a no-op per existing bind_world idempotency contract.
    room.bind_world(snapshot=snap, store=store)
    s2 = room.session
    assert s1 is s2


def test_bind_world_loads_orbital_content_when_world_dir_has_orbits(tmp_path: Path):
    """Orbital tier (orbits.yaml present) → session.orbital_content populated."""
    fixtures = Path(__file__).parent.parent / "orbital" / "fixtures" / "world_minimal"
    room = _make_room()
    snap = GameSnapshot()
    store = SqliteStore(tmp_path / "t.db")
    room.bind_world(snapshot=snap, store=store, world_dir=fixtures)

    content = room.session.orbital_content
    assert content is not None
    assert "coyote" in content.orbits.bodies


def test_bind_world_no_orbital_tier_leaves_content_none(tmp_path: Path):
    """World without orbits.yaml binds cleanly with orbital_content=None."""
    empty_world = tmp_path / "empty_world"
    empty_world.mkdir()
    room = _make_room()
    snap = GameSnapshot()
    store = SqliteStore(tmp_path / "t.db")
    room.bind_world(snapshot=snap, store=store, world_dir=empty_world)

    assert room.session.orbital_content is None


def test_bind_world_without_world_dir_back_compat(tmp_path: Path):
    """Existing call sites that don't pass world_dir still work — orbital_content=None."""
    room = _make_room()
    snap = GameSnapshot()
    store = SqliteStore(tmp_path / "t.db")
    room.bind_world(snapshot=snap, store=store)
    assert room.session.orbital_content is None
