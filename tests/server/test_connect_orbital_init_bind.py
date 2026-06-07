"""RED rework tests — Story 95-1: connect-time orbital init/resume bind.

Round-trip 1 (Reviewer REJECT). These cover the connect wiring helper
``_bind_initial_orbital_scope`` that the first GREEN pass shipped untested —
and the HIGH defect Avasarala found in it: a blank ``cartography.starting_region``
(``CartographyConfig.starting_region`` defaults to ``str = ""`` with no
non-empty validation) on an orbital world SILENTLY skips the fail-loud init
bind. That contradicts the bind-on-init AC ("starting_region with no matching
star body fails loud, not a silent system_root fallback") and the <critical>
No Silent Fallbacks rule.

The helper is module-private but importable; the Reviewer explicitly sanctioned
direct-call tests with a synthetic room + genre_pack over the full
Postgres-backed ConnectHandler harness.

Contract pinned for Dev (GREEN):

  ``_bind_initial_orbital_scope(room, *, genre_pack, world_slug, is_resume)``

  - Fresh (is_resume=False), orbital world, VALID starting_region -> centers
    scope + party_body_id on the matching body.
  - Fresh, orbital world, BLANK/missing starting_region -> raises
    RegionScopeBindError (fail loud). **This is the round-trip-1 regression.**
  - Resume (is_resume=True) -> centers on the party's persisted current_region.
  - Non-orbital world (orbital_content is None) -> clean no-op, no raise.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from sidequest.game.persistence import GameMode
from sidequest.game.repository import SaveRepository
from sidequest.game.session import GameSnapshot
from sidequest.genre.models.world import CartographyConfig, NavigationMode, Region
from sidequest.handlers.connect import _bind_initial_orbital_scope
from sidequest.orbital.scope_bind import RegionScopeBindError
from sidequest.server.session_room import SessionRoom

SECTOR_FIXTURE = (
    Path(__file__).resolve().parent.parent / "orbital" / "fixtures" / "world_sector_join"
)


def _sector_regions() -> dict[str, Region]:
    """Region ids match the sector fixture's body-ids (identity join)."""
    return {
        "yula": Region(name="Yula", summary="amber", description="amber", adjacent=["vorn"]),
        "vorn": Region(name="Vorn", summary="blue", description="blue", adjacent=["yula"]),
    }


def _pack_with_starting_region(starting_region: str) -> SimpleNamespace:
    """A genre pack whose ``sector`` world declares ``starting_region``."""
    world_obj = SimpleNamespace(
        cartography=CartographyConfig(
            navigation_mode=NavigationMode.region,
            starting_region=starting_region,
            regions=_sector_regions(),
        )
    )
    return SimpleNamespace(worlds={"sector": world_obj})


def _orbital_room(snap: GameSnapshot) -> SessionRoom:
    """Room whose Session has the sector orbital tier loaded."""
    room = SessionRoom(slug="sector_world", mode=GameMode.SOLO)
    room.bind_world(
        snapshot=snap,
        store=MagicMock(spec=SaveRepository),
        world_dir=SECTOR_FIXTURE,
    )
    return room


def _non_orbital_room(snap: GameSnapshot) -> SessionRoom:
    """Room with NO orbital tier (world_dir=None -> orbital_content is None)."""
    room = SessionRoom(slug="plain_world", mode=GameMode.SOLO)
    room.bind_world(
        snapshot=snap,
        store=MagicMock(spec=SaveRepository),
        world_dir=None,
    )
    return room


def test_init_bind_fresh_centers_on_starting_region() -> None:
    """Fresh connect to an orbital world centers the chart on starting_region."""
    snap = GameSnapshot(genre_slug="test_pack", world_slug="sector")
    room = _orbital_room(snap)
    assert room.session.orbital_content is not None

    _bind_initial_orbital_scope(
        room,
        genre_pack=_pack_with_starting_region("yula"),
        world_slug="sector",
        is_resume=False,
    )

    assert room.session.party_body_id == "yula"
    assert room.session.orbital_scope.center_body_id == "yula"


def test_init_bind_fresh_blank_starting_region_fails_loud() -> None:
    """ROUND-TRIP-1 REGRESSION: a fresh orbital world with a blank
    starting_region must FAIL LOUD, not silently leave the chart at system
    root. ``CartographyConfig.starting_region`` defaults to ``""``, so this is
    the canonical content-misconfiguration the bind-on-init fail-loud exists to
    surface — exactly the footgun for a homebrew author who omits the key."""
    snap = GameSnapshot(genre_slug="test_pack", world_slug="sector")
    room = _orbital_room(snap)
    assert room.session.orbital_content is not None

    with pytest.raises(RegionScopeBindError):
        _bind_initial_orbital_scope(
            room,
            genre_pack=_pack_with_starting_region(""),
            world_slug="sector",
            is_resume=False,
        )

    # No partial mutation on the fail-loud path.
    assert room.session.party_body_id is None
    assert room.session.orbital_scope.is_system_root


def test_resume_bind_centers_on_persisted_current_region() -> None:
    """Resume centers the chart on the party's persisted current_region (not
    the starting_region) — orbital_scope is transient and resets each connect,
    so resume must re-center on where the party actually is."""
    snap = GameSnapshot(genre_slug="test_pack", world_slug="sector")
    snap.current_region = "vorn"
    room = _orbital_room(snap)

    _bind_initial_orbital_scope(
        room,
        genre_pack=_pack_with_starting_region("yula"),
        world_slug="sector",
        is_resume=True,
    )

    assert room.session.party_body_id == "vorn"
    assert room.session.orbital_scope.center_body_id == "vorn"


def test_init_bind_noop_for_non_orbital_world() -> None:
    """A non-orbital world (orbital_content is None) is a clean no-op — no raise
    even with a blank starting_region, and the chart is never touched.
    Regression guard: caverns_and_claudes / tea_and_murder connect cleanly."""
    snap = GameSnapshot(genre_slug="plain", world_slug="sector")
    room = _non_orbital_room(snap)
    assert room.session.orbital_content is None

    # Must not raise even though starting_region is blank — there is no chart.
    _bind_initial_orbital_scope(
        room,
        genre_pack=_pack_with_starting_region(""),
        world_slug="sector",
        is_resume=False,
    )

    assert room.session.party_body_id is None
