"""Descent-phase map switch (playtest 2026-06-22, beneath_sunden ``697cbc14``).

beneath_sunden is a HYBRID world: an authored surface cartography graph
(``ropefoot``/``the_dropmouth``, region-mode MAP_UPDATE) PLUS the ADR-106
procedural deep (``entrance``/``expNNN.rN``, DUNGEON_MAP). Both map emitters fire
EVERY turn into the UI's single ``mapData`` slot, and the cartography emit is
dispatched second — so it CLOBBERED the DUNGEON_MAP and the player saw the two
surface regions no matter how deep they stood (``dungeon.map_emitted region=
exp001.r2`` immediately overwritten by ``cartography.map_emitted discovered=1``).

The fix is a per-connection descent-phase gate (``_descent_phase``) so exactly
ONE map projection owns the turn:
  - ``"deep"``    -> the PC's region is a node in the procedural graph:
                     DUNGEON_MAP owns the map, the cartography emit stands down
                     (``cartography.map_skipped`` reason=``deep_phase``).
  - ``"surface"`` -> the PC is above the rope (a cartography region, NOT a
                     dungeon node): the cartography MAP_UPDATE owns the map, the
                     dungeon emit stands down (``dungeon.map_skipped`` reason=
                     ``surface_phase``) instead of shipping a useless 0/N frame.
  - ``"n/a"``     -> not a dungeon world: cartography behaves exactly as before.

CONTENT-FREE: synthetic ``RegionGraph`` + stub palette + a hand-built snapshot;
the single content/IO seam (``_load_dungeon_map_context``) is stubbed, never a
live pack (``feedback_no_content_coupled_tests``).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from sidequest.dungeon.region_graph.model import RegionEdge, RegionGraph, RegionNode
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.models.world import CartographyConfig, NavigationMode, Region
from sidequest.server.websocket_handlers import map_emit as h

if TYPE_CHECKING:
    from sidequest.dungeon.themes import ThemePalette
    from sidequest.server.session_state import _SessionData


_ENTRANCE = "entrance"


# Duck-typed palette stub — _build_dungeon_map_payload only touches
# palette.get(theme).display_name (fail-soft on KeyError); a real DungeonTheme
# needs content-shaped fields we don't want here.
@dataclass
class _StubTheme:
    display_name: str


class _StubPalette:
    def get(self, theme_id: str) -> _StubTheme:
        return _StubTheme(display_name="Test Region")


def _deep_graph() -> RegionGraph:
    """The synthetic procedural deep: entrance - exp001.r2."""
    g = RegionGraph(entrance_id=_ENTRANCE)
    g.add_node(RegionNode(id="entrance", expansion_id=0, theme="t"))
    g.add_node(RegionNode(id="exp001.r2", expansion_id=1, theme="t"))
    g.add_edge(RegionEdge(a="entrance", b="exp001.r2", kind="shaft"))
    return g


def _palette() -> ThemePalette:
    return cast("ThemePalette", _StubPalette())


def _beneath_sunden_sd() -> Any:
    """A beneath_sunden-shaped session: region-mode surface cartography
    (ropefoot/the_dropmouth) PLUS a dungeon (supplied by stubbing
    _load_dungeon_map_context). The two graphs are deliberately disjoint."""
    regions = {
        "ropefoot": Region(
            name="Ropefoot",
            summary="The rope landing.",
            description="The rope landing.",
            adjacent=["the_dropmouth"],
        ),
        "the_dropmouth": Region(
            name="The Dropmouth",
            summary="The shaft mouth.",
            description="The shaft mouth.",
            adjacent=["ropefoot"],
        ),
    }
    world_obj = SimpleNamespace(
        cartography=CartographyConfig(navigation_mode=NavigationMode.region, regions=regions)
    )
    pack = SimpleNamespace(worlds={"beneath_sunden": world_obj})
    return SimpleNamespace(
        genre_pack=pack,
        world_slug="beneath_sunden",
        genre_slug="caverns_and_claudes",
        player_id="p1",
    )


def _snapshot(*, pc_region: str, discovered: tuple[str, ...] = ("ropefoot",)) -> GameSnapshot:
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        turn_manager=TurnManager(),
    )
    snap.player_seats = {"p1": "Rux"}
    snap.pc_regions = {"Rux": pc_region}
    snap.current_region = pc_region
    snap.discovered_regions = list(discovered)
    return snap


@pytest.fixture
def _stub_deep_ctx(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the single content/IO seam so _descent_phase + the dungeon emit run
    against the synthetic deep graph (entrance/exp001.r2), content-free."""

    def _stub_load(sd: Any) -> Any:
        return (_deep_graph(), _palette(), _ENTRANCE)

    monkeypatch.setattr(h, "_load_dungeon_map_context", _stub_load, raising=True)


def _capture_events(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    def _cap(event_type: str, fields: dict[str, Any], **kw: Any) -> None:
        events.append({"type": event_type, "fields": fields, **kw})

    monkeypatch.setattr(h, "_watcher_publish", _cap)
    return events


# --------------------------------------------------------------------------
# dungeon emit: SURFACE phase stands down (no 0/N frame to be clobbered)
# --------------------------------------------------------------------------
def test_dungeon_emit_stands_down_on_surface(
    _stub_deep_ctx: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = _capture_events(monkeypatch)
    emitted: list[Any] = []
    snap = _snapshot(pc_region="ropefoot")  # NOT a node in the deep graph

    h._maybe_emit_dungeon_map(
        object(),
        sd=cast("_SessionData", _beneath_sunden_sd()),
        snapshot=snap,
        emit_fn=lambda *a, **k: emitted.append(a),
    )

    assert emitted == [], "dungeon emit must NOT ship a frame when the PC is on the surface"
    skipped = [e for e in events if e["type"] == "dungeon.map_skipped"]
    assert skipped, f"expected dungeon.map_skipped, got {[e['type'] for e in events]}"
    assert skipped[-1]["fields"]["reason"] == "surface_phase"
    assert not any(e["type"] == "dungeon.map_emitted" for e in events)


# --------------------------------------------------------------------------
# dungeon emit: DEEP phase still ships the DUNGEON_MAP (guard against over-suppression)
# --------------------------------------------------------------------------
def test_dungeon_emit_ships_in_deep(
    _stub_deep_ctx: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = _capture_events(monkeypatch)
    emitted: list[Any] = []
    snap = _snapshot(pc_region="exp001.r2", discovered=("entrance", "exp001.r2"))

    h._maybe_emit_dungeon_map(
        object(),
        sd=cast("_SessionData", _beneath_sunden_sd()),
        snapshot=snap,
        emit_fn=lambda msg, kind: emitted.append((kind, msg)),
    )

    assert any(k == "DUNGEON_MAP" for k, _ in emitted), "deep phase must ship the DUNGEON_MAP"
    assert any(e["type"] == "dungeon.map_emitted" for e in events)


# --------------------------------------------------------------------------
# cartography emit: DEEP phase stands down (the clobber bug — the heart of the fix)
# --------------------------------------------------------------------------
def test_cartography_emit_stands_down_in_deep(
    _stub_deep_ctx: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = _capture_events(monkeypatch)
    sent: list[Any] = []
    snap = _snapshot(pc_region="exp001.r2", discovered=("entrance", "exp001.r2"))

    h._maybe_emit_cartography_map(
        object(),
        sd=cast("_SessionData", _beneath_sunden_sd()),
        snapshot=snap,
        emit_fn=lambda msg, kind: sent.append((kind, msg)),
        acting_perspective="Rux",
    )

    assert not [m for (t, m) in sent if t == "MAP_UPDATE"], (
        "cartography must NOT clobber the deep DUNGEON_MAP with the surface graph"
    )
    skipped = [e for e in events if e["type"] == "cartography.map_skipped"]
    assert skipped, f"expected cartography.map_skipped, got {[e['type'] for e in events]}"
    assert skipped[-1]["fields"]["reason"] == "deep_phase"


# --------------------------------------------------------------------------
# cartography emit: SURFACE phase still ships the MAP_UPDATE
# --------------------------------------------------------------------------
def test_cartography_emit_ships_on_surface(
    _stub_deep_ctx: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _capture_events(monkeypatch)
    sent: list[Any] = []
    snap = _snapshot(pc_region="ropefoot", discovered=("ropefoot",))

    h._maybe_emit_cartography_map(
        object(),
        sd=cast("_SessionData", _beneath_sunden_sd()),
        snapshot=snap,
        emit_fn=lambda msg, kind: sent.append((kind, msg)),
        acting_perspective="Rux",
    )

    assert [m for (t, m) in sent if t == "MAP_UPDATE"], (
        "on the surface the cartography MAP_UPDATE must still ship"
    )


# --------------------------------------------------------------------------
# regression: non-dungeon region world -> phase "n/a" -> cartography unchanged.
# No _load_dungeon_map_context stub: the REAL applies_to() returns False, so the
# gate must not interfere (and must not blow up resolving a non-dungeon sd).
# --------------------------------------------------------------------------
def test_cartography_emit_unchanged_for_non_dungeon_world(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capture_events(monkeypatch)
    sent: list[Any] = []
    regions = {"edo": Region(name="Edo", summary="x", description="x", adjacent=[])}
    world_obj = SimpleNamespace(
        cartography=CartographyConfig(navigation_mode=NavigationMode.region, regions=regions)
    )
    pack = SimpleNamespace(worlds={"burning_peace": world_obj})
    sd = SimpleNamespace(
        genre_pack=pack,
        world_slug="burning_peace",
        genre_slug="elemental_harmony",
        player_id="p1",
    )
    snap = GameSnapshot(
        genre_slug="elemental_harmony",
        world_slug="burning_peace",
        turn_manager=TurnManager(),
    )
    snap.current_region = "edo"
    snap.discovered_regions = ["edo"]

    h._maybe_emit_cartography_map(
        object(),
        sd=cast("_SessionData", sd),
        snapshot=snap,
        emit_fn=lambda msg, kind: sent.append((kind, msg)),
        acting_perspective=None,
    )

    assert [m for (t, m) in sent if t == "MAP_UPDATE"], (
        "a non-dungeon region world must still emit MAP_UPDATE (phase n/a)"
    )
