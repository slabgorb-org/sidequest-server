"""SITE_MAP emit contract — Track B, Tasks 7+8 (story 164-4).

The map-emit layer must serve ANY site scene, not just beneath_sunden:

  - a bounded site (tavern) projects its interior graph as a ``SITE_MAP``
    frame carrying site identity (``site_id``/``site_name``/``archetype``/
    ``extent``) so the UI can name and distinguish sites (164-5 consumes);
  - the legacy Sünden frontier keeps emitting its deep map — now as
    ``SITE_MAP`` with ``site_id="frontier"`` (same payload body, new wire
    type + identity fields);
  - the cartography MAP_UPDATE stands down while a site scene is active —
    the 2026-06-22 clobber gate, generalized beyond the beneath_sunden
    ``applies_to`` fence (the 158-36 single-``mapData``-slot problem);
  - a plain region world with no sites keeps its cartography behavior
    unchanged (characterization pin — green before AND after the cutover).

Seam note: the single content/IO seam (``_load_dungeon_map_context``) is
stubbed exactly as in ``test_descent_phase_map_switch.py`` — the Task 7
plan modifies that helper's gate but keeps its (graph, palette, entrance)
contract. The synthetic ``sd`` ALSO carries a duck-typed
``dungeon_repository`` returning the same graph, so an implementation that
reads the store directly (keyed by ``site_id``) finds the identical world.

CONTENT-FREE: synthetic ``RegionGraph`` + stub palette + hand-built
snapshots, never a live pack (``feedback_no_content_coupled_tests``).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from sidequest.dungeon.region_graph.model import RegionEdge, RegionGraph, RegionNode
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.models.world import (
    CartographyConfig,
    NavigationMode,
    Region,
    SiteDecl,
)
from sidequest.server.websocket_handlers import map_emit as h

if TYPE_CHECKING:
    from sidequest.dungeon.themes import ThemePalette
    from sidequest.server.session_state import _SessionData

FRONTIER_DECL = SiteDecl(
    site_id="frontier",
    name="The Deep",
    archetype="megadungeon",
    attached_to="the_dropmouth",
    extent="frontier",
)

TAVERN_DECL = SiteDecl(
    site_id="gilded_boar",
    name="The Gilded Boar",
    archetype="tavern",
    attached_to="village_green",
    extent="bounded",
)


@dataclass
class _StubTheme:
    display_name: str


class _StubPalette:
    def get(self, theme_id: str) -> _StubTheme:
        return _StubTheme(display_name="Test Region")


def _palette() -> ThemePalette:
    return cast("ThemePalette", _StubPalette())


def _region(name: str, adjacent: list[str]) -> Region:
    return Region(name=name, summary="x", description="x", adjacent=adjacent)


def _deep_graph() -> RegionGraph:
    """The legacy Sünden frontier: BARE node ids (un-namespaced in B1)."""
    g = RegionGraph(entrance_id="entrance")
    g.add_node(RegionNode(id="entrance", expansion_id=0, theme="t"))
    g.add_node(RegionNode(id="exp001.r2", expansion_id=1, theme="t"))
    g.add_edge(RegionEdge(a="entrance", b="exp001.r2", kind="shaft"))
    return g


def _tavern_graph() -> RegionGraph:
    """A bounded site's interior: ``{site_id}:``-namespaced node ids."""
    g = RegionGraph(entrance_id="gilded_boar:entrance")
    g.add_node(RegionNode(id="gilded_boar:entrance", expansion_id=0, theme="t"))
    g.add_node(RegionNode(id="gilded_boar:r2", expansion_id=0, theme="t"))
    g.add_edge(RegionEdge(a="gilded_boar:entrance", b="gilded_boar:r2", kind="corridor"))
    return g


class _GraphStore:
    """Duck-typed dungeon-repository double: whatever the call shape,
    ``load_map`` answers with the one synthetic graph."""

    def __init__(self, graph: RegionGraph) -> None:
        self._graph = graph

    def load_map(self, **kwargs: Any) -> RegionGraph:
        return self._graph


def _sunden_sd() -> Any:
    regions = {
        "ropefoot": _region("Ropefoot", ["the_dropmouth"]),
        "the_dropmouth": _region("The Dropmouth", ["ropefoot"]),
    }
    world_obj = SimpleNamespace(
        cartography=CartographyConfig(
            navigation_mode=NavigationMode.region,
            regions=regions,
            sites=[FRONTIER_DECL],
        )
    )
    pack = SimpleNamespace(worlds={"beneath_sunden": world_obj})
    return SimpleNamespace(
        genre_pack=pack,
        world_slug="beneath_sunden",
        genre_slug="caverns_and_claudes",
        player_id="p1",
        dungeon_repository=_GraphStore(_deep_graph()),
    )


def _tavern_sd() -> Any:
    """NOT beneath_sunden — the whole point: a different genre/world whose
    bounded site must be served by the same emit path."""
    regions = {
        "village_green": _region("Village Green", ["high_street"]),
        "high_street": _region("High Street", ["village_green"]),
    }
    world_obj = SimpleNamespace(
        cartography=CartographyConfig(
            navigation_mode=NavigationMode.region,
            regions=regions,
            sites=[TAVERN_DECL],
        )
    )
    pack = SimpleNamespace(worlds={"kettleford": world_obj})
    return SimpleNamespace(
        genre_pack=pack,
        world_slug="kettleford",
        genre_slug="tea_time",
        player_id="p1",
        dungeon_repository=_GraphStore(_tavern_graph()),
    )


def _snapshot(
    *,
    pc_region: str,
    world_slug: str,
    genre_slug: str,
    discovered: tuple[str, ...],
) -> GameSnapshot:
    snap = GameSnapshot(
        genre_slug=genre_slug,
        world_slug=world_slug,
        turn_manager=TurnManager(),
    )
    snap.player_seats = {"p1": "Rux"}
    snap.pc_regions = {"Rux": pc_region}
    snap.current_region = pc_region
    snap.discovered_regions = list(discovered)
    return snap


def _stub_loader(monkeypatch: pytest.MonkeyPatch, graph: RegionGraph, entrance_id: str) -> None:
    """Stub the content/IO seam (same seam as test_descent_phase_map_switch)."""

    def _stub_load(*args: Any, **kwargs: Any) -> Any:
        return (graph, _palette(), entrance_id)

    monkeypatch.setattr(h, "_load_dungeon_map_context", _stub_load, raising=True)


def _capture_events(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    def _cap(event_type: str, fields: dict[str, Any], **kw: Any) -> None:
        events.append({"type": event_type, "fields": fields, **kw})

    monkeypatch.setattr(h, "_watcher_publish", _cap)
    return events


# --------------------------------------------------------------------------
# bounded site scene -> SITE_MAP with site identity (the fence is GONE)
# --------------------------------------------------------------------------
def test_bounded_site_scene_emits_site_map_with_site_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-beneath_sunden world's bounded site must project its interior
    map — as a SITE_MAP frame naming the site."""
    events = _capture_events(monkeypatch)
    _stub_loader(monkeypatch, _tavern_graph(), "gilded_boar:entrance")
    emitted: list[tuple[Any, str]] = []
    snap = _snapshot(
        pc_region="gilded_boar:r2",
        world_slug="kettleford",
        genre_slug="tea_time",
        discovered=("gilded_boar:entrance", "gilded_boar:r2"),
    )

    h._maybe_emit_dungeon_map(
        object(),
        sd=cast("_SessionData", _tavern_sd()),
        snapshot=snap,
        emit_fn=lambda msg, kind: emitted.append((msg, kind)),
    )

    kinds = [k for _, k in emitted]
    assert kinds == ["SITE_MAP"], f"site scene must ship a SITE_MAP frame, got {kinds}"
    msg = emitted[0][0]
    assert msg.payload.site_id == "gilded_boar"
    assert msg.payload.site_name == "The Gilded Boar"
    assert msg.payload.archetype == "tavern"
    assert msg.payload.extent == "bounded"
    assert msg.payload.current_location == "gilded_boar:r2"
    assert msg.player_id == "p1"
    assert {loc.id for loc in msg.payload.explored} == {
        "gilded_boar:entrance",
        "gilded_boar:r2",
    }
    entrance_rooms = [loc for loc in msg.payload.explored if loc.id == "gilded_boar:entrance"]
    assert entrance_rooms and entrance_rooms[0].room_type == "entrance"
    assert any(e["type"] == "dungeon.map_emitted" for e in events)


# --------------------------------------------------------------------------
# Sünden continuity: the legacy frontier deep still emits — as SITE_MAP
# --------------------------------------------------------------------------
def test_sunden_frontier_emits_deep_map_as_site_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LOAD-BEARING (AC4 precursor): the beneath_sunden deep map must keep
    shipping through the cutover — same payload body, SITE_MAP wire type,
    frontier site identity."""
    _capture_events(monkeypatch)
    _stub_loader(monkeypatch, _deep_graph(), "entrance")
    emitted: list[tuple[Any, str]] = []
    snap = _snapshot(
        pc_region="exp001.r2",
        world_slug="beneath_sunden",
        genre_slug="caverns_and_claudes",
        discovered=("entrance", "exp001.r2"),
    )

    h._maybe_emit_dungeon_map(
        object(),
        sd=cast("_SessionData", _sunden_sd()),
        snapshot=snap,
        emit_fn=lambda msg, kind: emitted.append((msg, kind)),
    )

    kinds = [k for _, k in emitted]
    assert kinds == ["SITE_MAP"], f"the deep must ship as SITE_MAP after the cutover, got {kinds}"
    msg = emitted[0][0]
    assert msg.payload.site_id == "frontier"
    assert msg.payload.site_name == "The Deep"
    assert msg.payload.archetype == "megadungeon"
    assert msg.payload.extent == "frontier"
    assert msg.payload.current_location == "exp001.r2"
    assert {loc.id for loc in msg.payload.explored} == {"entrance", "exp001.r2"}
    current = [loc for loc in msg.payload.explored if loc.is_current_room]
    assert [loc.id for loc in current] == ["exp001.r2"], (
        "the per-PC YOU-ARE-HERE marker must survive the cutover"
    )


# --------------------------------------------------------------------------
# cartography stands down inside ANY site scene (un-stubbed — the real gate)
# --------------------------------------------------------------------------
def test_cartography_stands_down_inside_a_bounded_site_scene(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UN-STUBBED: with the PC inside a bounded site of a NON-beneath_sunden
    world, today's ``applies_to`` fence answers "not a dungeon world" and the
    cartography MAP_UPDATE ships — clobbering the site map in the UI's single
    ``mapData`` slot (158-36). After the cutover the scene-context gate must
    stand it down."""
    events = _capture_events(monkeypatch)
    sent: list[tuple[str, Any]] = []
    snap = _snapshot(
        pc_region="gilded_boar:r2",
        world_slug="kettleford",
        genre_slug="tea_time",
        discovered=("village_green",),
    )

    h._maybe_emit_cartography_map(
        object(),
        sd=cast("_SessionData", _tavern_sd()),
        snapshot=snap,
        emit_fn=lambda msg, kind: sent.append((kind, msg)),
        acting_perspective="Rux",
    )

    assert not [m for (t, m) in sent if t == "MAP_UPDATE"], (
        "cartography must NOT clobber a site scene's map with the surface graph"
    )
    skipped = [e for e in events if e["type"] == "cartography.map_skipped"]
    assert skipped, f"expected cartography.map_skipped, got {[e['type'] for e in events]}"


# --------------------------------------------------------------------------
# characterization pins — green before AND after the cutover
# --------------------------------------------------------------------------
def test_cartography_unchanged_for_siteless_region_world(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PIN (green now, green after): a plain region-mode world with no sites
    keeps its cartography MAP_UPDATE exactly as before."""
    _capture_events(monkeypatch)
    sent: list[tuple[str, Any]] = []
    regions = {"edo": _region("Edo", [])}
    world_obj = SimpleNamespace(
        cartography=CartographyConfig(navigation_mode=NavigationMode.region, regions=regions)
    )
    sd = SimpleNamespace(
        genre_pack=SimpleNamespace(worlds={"burning_peace": world_obj}),
        world_slug="burning_peace",
        genre_slug="elemental_harmony",
        player_id="p1",
    )
    snap = _snapshot(
        pc_region="edo",
        world_slug="burning_peace",
        genre_slug="elemental_harmony",
        discovered=("edo",),
    )

    h._maybe_emit_cartography_map(
        object(),
        sd=cast("_SessionData", sd),
        snapshot=snap,
        emit_fn=lambda msg, kind: sent.append((kind, msg)),
        acting_perspective="Rux",
    )

    assert [m for (t, m) in sent if t == "MAP_UPDATE"], (
        "a siteless region world must still emit its cartography MAP_UPDATE"
    )


def test_site_emit_stands_down_on_world_scene(monkeypatch: pytest.MonkeyPatch) -> None:
    """PIN (green now, green after): a PC on the surface cartography gets no
    site/dungeon frame — the cartography emit owns that turn. Guards the
    inverse of the clobber (over-suppression)."""
    events = _capture_events(monkeypatch)
    _stub_loader(monkeypatch, _deep_graph(), "entrance")
    emitted: list[tuple[Any, str]] = []
    snap = _snapshot(
        pc_region="ropefoot",
        world_slug="beneath_sunden",
        genre_slug="caverns_and_claudes",
        discovered=("ropefoot",),
    )

    h._maybe_emit_dungeon_map(
        object(),
        sd=cast("_SessionData", _sunden_sd()),
        snapshot=snap,
        emit_fn=lambda msg, kind: emitted.append((msg, kind)),
    )

    assert emitted == [], "no site frame may ship for a world-scene connection"
    assert not any(e["type"] == "dungeon.map_emitted" for e in events)
    assert any(e["type"] == "dungeon.map_skipped" for e in events), (
        "the stand-down must stay LOUD (a skip span), never silent"
    )
