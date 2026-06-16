"""Phase 3 — per-PC ``dungeon.map_emitted`` (Movement subsystem §Q-map / OP1).

CONTENT-FREE: synthetic ``RegionGraph`` + synthetic ``ThemePalette`` + a
hand-built ``GameSnapshot`` with ``player_seats``/``pc_regions``. NEVER loads a
live genre pack (``feedback_no_content_coupled_tests``).

These drive the per-connection YOU-ARE-HERE behavior:
  - the emit reads THIS connection's PC region (``player_id`` -> seat -> PC
    name -> ``region_for(perspective=pc)``), NOT the singular ``current_region``;
  - ``discovered`` stays the SHARED fog-of-war set;
  - a connection with no seated PC / no ``pc_regions`` entry emits
    ``dungeon.map_skipped(no_pc_region)`` (loud, no fallback);
  - the ``dungeon.map_emitted`` span carries ``pc_name`` + ``pc_region``.

TDD plan items 17-20 (per-PC split-party + per-connection map).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import pytest

from sidequest.dungeon.region_graph.model import RegionEdge, RegionGraph, RegionNode

if TYPE_CHECKING:
    from sidequest.dungeon.themes import ThemePalette
    from sidequest.server.session_state import _SessionData
from sidequest.server.websocket_handlers.map_emit import (
    _build_dungeon_map_payload,
    _resolve_connection_pc_region,
)


# Duck-typed palette stub: ``_build_dungeon_map_payload`` only calls
# ``palette.get(theme).display_name`` (and fail-soft-catches KeyError). A full
# real ``DungeonTheme`` requires content-shaped fields (generator_class,
# interior, depth_band, narrator) we don't need here — keep it content-free.
@dataclass
class _StubTheme:
    display_name: str


class _StubPalette:
    def __init__(self, themes: dict[str, _StubTheme]) -> None:
        self._themes = themes

    def get(self, theme_id: str) -> _StubTheme:
        return self._themes[theme_id]  # raises KeyError on miss (fail-soft path)


# --------------------------------------------------------------------------
# Synthetic fixtures (content-free)
# --------------------------------------------------------------------------
_ENTRANCE = "r1"


def _graph() -> RegionGraph:
    """A tiny 3-region graph: r1 (entrance) - r2 - r3."""
    g = RegionGraph(entrance_id=_ENTRANCE)
    g.add_node(RegionNode(id="r1", expansion_id=0, theme="t"))
    g.add_node(RegionNode(id="r2", expansion_id=1, theme="t"))
    g.add_node(RegionNode(id="r3", expansion_id=2, theme="t"))
    g.add_edge(RegionEdge(a="r1", b="r2", kind="corridor"))
    g.add_edge(RegionEdge(a="r2", b="r3", kind="stairs"))
    return g


def _palette() -> ThemePalette:
    # Duck-typed stub cast to ThemePalette — exercises only the .get().display_name
    # surface _build_dungeon_map_payload touches (content-free, no real DungeonTheme).
    return cast("ThemePalette", _StubPalette({"t": _StubTheme(display_name="Test Region")}))


def _snapshot(*, seats: dict[str, str], pc_regions: dict[str, str]) -> Any:
    """Minimal real GameSnapshot with seats + per-PC regions only."""
    from sidequest.game.session import GameSnapshot

    snap = GameSnapshot()
    snap.player_seats = dict(seats)
    snap.pc_regions = dict(pc_regions)
    return snap


# --------------------------------------------------------------------------
# OP1 — connection player_id -> seat -> PC name -> region resolution
# --------------------------------------------------------------------------
def test_resolve_pc_region_for_seated_pc() -> None:
    snap = _snapshot(
        seats={"pid-rux": "Rux", "pid-gorm": "Gorm"},
        pc_regions={"Rux": "r2", "Gorm": "r3"},
    )
    assert _resolve_connection_pc_region(snap, "pid-rux") == ("Rux", "r2")
    assert _resolve_connection_pc_region(snap, "pid-gorm") == ("Gorm", "r3")


def test_resolve_pc_region_spectator_no_seat() -> None:
    """player_id maps to no seated character (spectator / GM panel)."""
    snap = _snapshot(seats={"pid-rux": "Rux"}, pc_regions={"Rux": "r2"})
    assert _resolve_connection_pc_region(snap, "pid-spectator") == (None, None)


def test_resolve_pc_region_seated_but_no_region_entry() -> None:
    """Seated PC with no pc_regions entry -> (pc_name, None), no fallback."""
    snap = _snapshot(seats={"pid-rux": "Rux"}, pc_regions={})
    assert _resolve_connection_pc_region(snap, "pid-rux") == ("Rux", None)


# --------------------------------------------------------------------------
# Test 17 — per-connection YOU-ARE-HERE over a SHARED discovered set
# --------------------------------------------------------------------------
def test_per_connection_marker_split_party() -> None:
    graph = _graph()
    palette = _palette()
    discovered = ["r1", "r2", "r3"]  # SHARED fog-of-war set

    rux_payload = _build_dungeon_map_payload(
        graph=graph,
        palette=palette,
        pc_region="r2",
        discovered_regions=discovered,
        entrance_id=_ENTRANCE,
    )
    gorm_payload = _build_dungeon_map_payload(
        graph=graph,
        palette=palette,
        pc_region="r3",
        discovered_regions=discovered,
        entrance_id=_ENTRANCE,
    )

    # Rux's client: YOU-ARE-HERE on r2 only.
    assert rux_payload.current_location == "r2"
    assert rux_payload.region == "r2"
    current_rux = [loc.id for loc in rux_payload.explored if loc.is_current_room]
    assert current_rux == ["r2"]

    # Gorm's client: YOU-ARE-HERE on r3 only.
    assert gorm_payload.current_location == "r3"
    assert gorm_payload.region == "r3"
    current_gorm = [loc.id for loc in gorm_payload.explored if loc.is_current_room]
    assert current_gorm == ["r3"]

    # Test 20: discovered set is SHARED — both clients see all three regions,
    # including the region the OTHER PC discovered.
    assert {loc.id for loc in rux_payload.explored} == {"r1", "r2", "r3"}
    assert {loc.id for loc in gorm_payload.explored} == {"r1", "r2", "r3"}


# --------------------------------------------------------------------------
# Test 18 — emit uses perspective, not consensus (region_for None on split)
# --------------------------------------------------------------------------
def test_region_for_consensus_none_but_per_pc_resolves() -> None:
    snap = _snapshot(
        seats={"pid-rux": "Rux", "pid-gorm": "Gorm"},
        pc_regions={"Rux": "r2", "Gorm": "r3"},
    )
    # No perspective -> party split -> None (no fallback to current_region).
    assert snap.region_for() is None
    # Per-connection emit still resolves each PC's own region.
    assert _resolve_connection_pc_region(snap, "pid-rux") == ("Rux", "r2")
    assert _resolve_connection_pc_region(snap, "pid-gorm") == ("Gorm", "r3")


# --------------------------------------------------------------------------
# Test 19 — spectator connection -> map_skipped(no_pc_region), no payload,
#           no fallback to current_region (via the full emit entry point).
# --------------------------------------------------------------------------
def test_emit_spectator_map_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    from sidequest.server.websocket_handlers import map_emit as h

    events: list[dict[str, Any]] = []

    def _capture(event_type: str, fields: dict[str, Any], **kwargs: Any) -> None:
        events.append({"type": event_type, "fields": fields, **kwargs})

    monkeypatch.setattr(h, "_watcher_publish", _capture)

    snap = _snapshot(seats={"pid-rux": "Rux"}, pc_regions={"Rux": "r2"})
    # current_region is the STALE spawn anchor; a spectator must NOT see it.
    snap.current_region = "r1"

    emitted: list[Any] = []

    sd = _FakeSessionData(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        player_id="pid-spectator",
        graph=_graph(),
        palette=_palette(),
    )

    h._maybe_emit_dungeon_map(
        object(),
        sd=cast("_SessionData", sd),
        snapshot=snap,
        emit_fn=lambda *a, **k: emitted.append(a),
    )

    assert emitted == []  # NO payload emitted
    skipped = [e for e in events if e["type"] == "dungeon.map_skipped"]
    assert skipped, f"expected map_skipped, got {[e['type'] for e in events]}"
    assert skipped[-1]["fields"]["reason"] == "no_pc_region"
    # No emit carries the stale current_region.
    assert not any(e["type"] == "dungeon.map_emitted" for e in events)


# --------------------------------------------------------------------------
# Test 20 — dungeon.map_emitted span carries pc_name + pc_region; discovered
#           is the shared set (via the full emit entry point).
# --------------------------------------------------------------------------
def test_emit_span_carries_pc_name_and_region(monkeypatch: pytest.MonkeyPatch) -> None:
    from sidequest.server.websocket_handlers import map_emit as h

    events: list[dict[str, Any]] = []

    def _capture(event_type: str, fields: dict[str, Any], **kwargs: Any) -> None:
        events.append({"type": event_type, "fields": fields, **kwargs})

    monkeypatch.setattr(h, "_watcher_publish", _capture)

    snap = _snapshot(
        seats={"pid-rux": "Rux", "pid-gorm": "Gorm"},
        pc_regions={"Rux": "r2", "Gorm": "r3"},
    )
    snap.discovered_regions = ["r1", "r2", "r3"]  # shared; r3 was Gorm's find
    snap.current_region = "r1"

    emitted: list[Any] = []
    sd = _FakeSessionData(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        player_id="pid-rux",
        graph=_graph(),
        palette=_palette(),
    )

    h._maybe_emit_dungeon_map(
        object(),
        sd=cast("_SessionData", sd),
        snapshot=snap,
        emit_fn=lambda msg, kind: emitted.append(msg),
    )

    emit_events = [e for e in events if e["type"] == "dungeon.map_emitted"]
    assert emit_events, f"expected map_emitted, got {[e['type'] for e in events]}"
    fields = emit_events[-1]["fields"]
    assert fields["pc_name"] == "Rux"
    assert fields["pc_region"] == "r2"

    # The emitted payload's discovered set includes r3 (the OTHER PC's find).
    assert emitted, "expected a DUNGEON_MAP frame"
    payload = emitted[-1].payload
    assert {loc.id for loc in payload.explored} == {"r1", "r2", "r3"}
    assert payload.current_location == "r2"
    assert payload.region == "r2"


# --------------------------------------------------------------------------
# Fake _SessionData stub (avoids real SqliteStore / GenreLoader content load)
# --------------------------------------------------------------------------
class _FakeSessionData:
    """Just enough of ``_SessionData`` for ``_maybe_emit_dungeon_map``.

    The emit's content-path deps (DungeonStore.load_map, GenreLoader,
    load_theme_palette) are isolated behind the single
    ``_load_dungeon_map_context`` seam, stubbed below.
    """

    def __init__(
        self,
        *,
        genre_slug: str,
        world_slug: str,
        player_id: str,
        graph: RegionGraph,
        palette: ThemePalette,
    ) -> None:
        self.genre_slug = genre_slug
        self.world_slug = world_slug
        self.player_id = player_id
        self.store = object()
        self._graph = graph
        self._palette = palette


@pytest.fixture(autouse=True)
def _stub_content_path(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Stub the single content/IO seam inside ``_maybe_emit_dungeon_map`` so
    the full emit entry point runs CONTENT-FREE against the synthetic graph
    the _FakeSessionData carries.

    ``_load_dungeon_map_context(sd)`` returns ``(graph, palette, entrance_id)``
    or ``None`` (other-world no-op / no-schema / empty map). We return the
    synthetic pair so the per-PC payload logic runs unchanged."""
    from sidequest.server.websocket_handlers import map_emit as h

    def _stub_load(sd: Any) -> Any:
        return (sd._graph, sd._palette, _ENTRANCE)

    monkeypatch.setattr(h, "_load_dungeon_map_context", _stub_load, raising=True)
    yield
