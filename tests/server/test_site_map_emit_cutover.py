"""SITE_MAP emit cutover (Track B, Tasks 7+8) — the beneath_sunden fence
dissolves: ANY world with a declared site emits its site map when THIS
connection's PC stands in the site's graph, and the cartography emit stands
down for that connection. The emitted payload carries the site descriptor
(``site_id``/``site_name``/``archetype``/``extent``).

RED (story 164-4): today ``_maybe_emit_dungeon_map`` is fenced to
beneath_sunden (``region_projection.applies_to``) and labels its frame
``DUNGEON_MAP``; ``_maybe_emit_cartography_map`` only stands down for the
sunden "deep" phase. The two cutover tests below FAIL until Tasks 7/8 land.
The two guard tests pass today and must KEEP passing — they pin the
behavior the cutover is not allowed to break.

Wiring per CLAUDE.md ("fixture-driven behavior tests", never source-text
greps): synthetic non-sunden world + the REAL ``_maybe_emit_dungeon_map`` /
``_maybe_emit_cartography_map`` invocations + assertions on the emitted
typed message and watcher spans. CONTENT-FREE: no live pack.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

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

# ---------------------------------------------------------------------------
# Content-free doubles (shape shared with test_descent_phase_map_switch.py)
# ---------------------------------------------------------------------------


class _StubTheme:
    display_name = "Test Region"


class _StubPalette:
    def get(self, theme_id: str) -> _StubTheme:
        return _StubTheme()


class _StubDungeonRepo:
    """Duck-typed repository: only ``load_map(*, entrance_id, site_id)`` is
    consumed by the site-map emit path."""

    def __init__(self, graphs_by_site: dict[str, RegionGraph]) -> None:
        self._graphs = graphs_by_site

    def load_map(self, *, entrance_id: str, site_id: str = "frontier") -> RegionGraph:
        g = self._graphs.get(site_id)
        return g if g is not None else RegionGraph(entrance_id=entrance_id)


def _tavern_graph() -> RegionGraph:
    """The gilded_boar site's stored graph: namespaced entrance + one room."""
    g = RegionGraph(entrance_id="gilded_boar:entrance")
    g.add_node(RegionNode(id="gilded_boar:entrance", expansion_id=0, theme="t"))
    g.add_node(RegionNode(id="gilded_boar:r2", expansion_id=1, theme="t"))
    g.add_edge(RegionEdge(a="gilded_boar:entrance", b="gilded_boar:r2", kind="corridor"))
    return g


def _tavern_world_sd() -> Any:
    """A NON-beneath_sunden region world with one declared bounded site.
    This is the fence-dissolution fixture: under the OLD contract
    ``applies_to('spaghetti_western', 'gilded_reach')`` is False and no site
    map can ever emit here."""
    regions = {
        "dustcross": Region(
            name="Dustcross",
            summary="The crossroads town.",
            description="The crossroads town.",
            adjacent=[],
        ),
    }
    cart = CartographyConfig(
        navigation_mode=NavigationMode.region,
        regions=regions,
        sites=[
            SiteDecl(
                site_id="gilded_boar",
                name="The Gilded Boar",
                archetype="tavern",
                attached_to="dustcross",
                extent="bounded",
            )
        ],
    )
    world_obj = SimpleNamespace(cartography=cart)
    pack = SimpleNamespace(worlds={"gilded_reach": world_obj})
    return SimpleNamespace(
        genre_pack=pack,
        world_slug="gilded_reach",
        genre_slug="spaghetti_western",
        player_id="p1",
        dungeon_repository=_StubDungeonRepo({"gilded_boar": _tavern_graph()}),
    )


def _snapshot(
    *, pc_region: str | None, discovered: tuple[str, ...], seated: bool = True
) -> GameSnapshot:
    snap = GameSnapshot(
        genre_slug="spaghetti_western",
        world_slug="gilded_reach",
        turn_manager=TurnManager(),
    )
    if seated:
        snap.player_seats = {"p1": "Tex"}
        if pc_region is not None:
            snap.pc_regions = {"Tex": pc_region}
            snap.current_region = pc_region
    snap.discovered_regions = list(discovered)
    return snap


def _capture_events(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    def _cap(event_type: str, fields: dict[str, Any], **kw: Any) -> None:
        events.append({"type": event_type, "fields": fields, **kw})

    monkeypatch.setattr(h, "_watcher_publish", _cap)
    return events


# ---------------------------------------------------------------------------
# CUTOVER (RED today): site scene on a non-sunden world ships the SITE_MAP
# ---------------------------------------------------------------------------


def test_site_map_emits_for_non_sunden_site_world(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fence dissolves: a PC inside gilded_boar (spaghetti_western — NOT
    beneath_sunden) gets a site map frame labeled SITE_MAP whose payload
    carries the owning site's descriptor fields. Fails today: applies_to()
    fences the emit to beneath_sunden and the label is DUNGEON_MAP."""
    events = _capture_events(monkeypatch)
    emitted: list[tuple[str, Any]] = []
    snap = _snapshot(
        pc_region="gilded_boar:r2",
        discovered=("gilded_boar:entrance", "gilded_boar:r2"),
    )

    h._maybe_emit_dungeon_map(
        object(),
        sd=cast("Any", _tavern_world_sd()),
        snapshot=snap,
        emit_fn=lambda msg, kind: emitted.append((kind, msg)),
    )

    kinds = [k for k, _ in emitted]
    assert "SITE_MAP" in kinds, (
        f"a site scene on a non-sunden world must ship a SITE_MAP frame, got {kinds}"
    )
    assert "DUNGEON_MAP" not in kinds, "one cutover, no alias — the old label must be gone"

    msg = next(m for k, m in emitted if k == "SITE_MAP")
    assert msg.payload.site_id == "gilded_boar"
    assert msg.payload.site_name == "The Gilded Boar"
    assert msg.payload.archetype == "tavern"
    assert msg.payload.extent == "bounded"
    assert msg.payload.current_location == "gilded_boar:r2"
    assert msg.payload.region == "gilded_boar:r2"
    assert msg.player_id == "p1"

    # OTEL: the GM panel must see the emit engage (never a silent seam).
    assert any(e["type"] == "dungeon.map_emitted" for e in events), (
        f"expected dungeon.map_emitted span, got {[e['type'] for e in events]}"
    )

    # Fog of war: both discovered nodes render; YOU-ARE-HERE marks the PC room.
    explored = {loc.id: loc for loc in msg.payload.explored}
    assert set(explored) == {"gilded_boar:entrance", "gilded_boar:r2"}
    assert explored["gilded_boar:r2"].is_current_room
    assert not explored["gilded_boar:entrance"].is_current_room


def test_cartography_stands_down_in_site_scene_on_non_sunden_world(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mutually-exclusive-map invariant generalizes with the fence: when
    THIS connection's PC is inside a site on ANY world, the surface
    cartography MAP_UPDATE must stand down (loudly) instead of clobbering
    the site map in the UI's single mapData slot. Fails today: the stand-down
    gate is _descent_phase()=='deep', which is 'n/a' off beneath_sunden."""
    events = _capture_events(monkeypatch)
    sent: list[tuple[str, Any]] = []
    snap = _snapshot(
        pc_region="gilded_boar:r2",
        discovered=("gilded_boar:entrance", "gilded_boar:r2"),
    )

    h._maybe_emit_cartography_map(
        object(),
        sd=cast("Any", _tavern_world_sd()),
        snapshot=snap,
        emit_fn=lambda msg, kind: sent.append((kind, msg)),
        acting_perspective="Tex",
    )

    assert not [m for (t, m) in sent if t == "MAP_UPDATE"], (
        "cartography must NOT clobber the site map while the PC is in a site scene"
    )
    assert any(e["type"] == "cartography.map_skipped" for e in events), (
        f"the stand-down must be loud, got {[e['type'] for e in events]}"
    )


# ---------------------------------------------------------------------------
# GUARDS (green today, must stay green through the cutover)
# ---------------------------------------------------------------------------


def test_world_scene_keeps_cartography_and_suppresses_site_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PC on the world cartography (dustcross): the cartography MAP_UPDATE
    still owns the map and the site emit ships nothing — the cutover must
    not regress the every-other-world path."""
    _capture_events(monkeypatch)
    sent: list[tuple[str, Any]] = []
    emitted: list[tuple[str, Any]] = []
    snap = _snapshot(pc_region="dustcross", discovered=("dustcross",))

    h._maybe_emit_cartography_map(
        object(),
        sd=cast("Any", _tavern_world_sd()),
        snapshot=snap,
        emit_fn=lambda msg, kind: sent.append((kind, msg)),
        acting_perspective="Tex",
    )
    h._maybe_emit_dungeon_map(
        object(),
        sd=cast("Any", _tavern_world_sd()),
        snapshot=snap,
        emit_fn=lambda msg, kind: emitted.append((kind, msg)),
    )

    assert [m for (t, m) in sent if t == "MAP_UPDATE"], (
        "on the world scene the cartography MAP_UPDATE must still ship"
    )
    assert emitted == [], "no site frame may ship for a PC on the world cartography"


def test_unseated_connection_skips_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """A connection with no seated PC gets the loud no_pc_region skip — never
    a silent fall-through to a stale region (OP1)."""
    events = _capture_events(monkeypatch)
    emitted: list[tuple[str, Any]] = []
    snap = _snapshot(pc_region=None, discovered=(), seated=False)

    h._maybe_emit_dungeon_map(
        object(),
        sd=cast("Any", _tavern_world_sd()),
        snapshot=snap,
        emit_fn=lambda msg, kind: emitted.append((kind, msg)),
    )

    assert emitted == []
    skipped = [e for e in events if e["type"] == "dungeon.map_skipped"]
    assert skipped, f"expected dungeon.map_skipped, got {[e['type'] for e in events]}"
    assert skipped[-1]["fields"]["reason"] == "no_pc_region"


# ---------------------------------------------------------------------------
# REWORK RED (Reviewer HIGH, 2026-07-10): the palette degrade must be LOUD
# ---------------------------------------------------------------------------


def test_missing_theme_palette_degrade_is_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    """A site world without an authored ``themes/`` dir degrades to id-labels
    (the null palette — correct, kept) but the degrade must be LOUD: a
    ``dungeon.theme_palette_missing`` watcher event with world + site_id, so
    the GM panel can tell "themeless tavern world, working as intended" from
    "Sünden's themes/ dir silently vanished" (No Silent Fallbacks;
    ``ThemePaletteMissingError``'s own fail-loud contract). RED until Dev
    adds the span — today the ``except ThemePaletteMissingError`` branch in
    ``_load_site_map_context`` emits nothing at all."""
    from sidequest.game.sites.models import SiteDescriptor

    events = _capture_events(monkeypatch)
    sd = _tavern_world_sd()
    site = SiteDescriptor(
        site_id="gilded_boar",
        name="The Gilded Boar",
        archetype="tavern",
        attached_to="dustcross",
        extent="bounded",
    )

    ctx = h._load_site_map_context(cast("Any", sd), site)

    # The degrade itself stays: graph loads, a null palette comes back (every
    # lookup misses -> the payload builder's fail-soft id-label path).
    assert ctx is not None, "a missing palette must degrade, never skip the frame"
    _graph, palette, _entrance = ctx
    with pytest.raises(KeyError):
        palette.get("any_theme")

    # The RED driver: the degrade must be observable.
    missing = [e for e in events if e["type"] == "dungeon.theme_palette_missing"]
    assert missing, (
        "the ThemePaletteMissingError -> null-palette degrade must emit a "
        f"dungeon.theme_palette_missing watcher event, got {[e['type'] for e in events]}"
    )
    assert missing[-1]["fields"]["world"] == "gilded_reach"
    assert missing[-1]["fields"]["site_id"] == "gilded_boar"
