"""Region-mode MAP_UPDATE projection (EH-2 burning_peace playtest 2026-06-05).

PLAYTEST BUG: the Map tab read "No map data yet" for the entire opening of a
region-mode world. Root cause — the cartography MAP_UPDATE emit was gated on
``_region_changed`` (only fired on a move to a DIFFERENT region), so turn 1 and
intra-region moves (teahouse -> Hakone road, both in ``edo``) emitted nothing
and the UI never received the region graph (``mapData`` stayed null → MapWidget's
``!mapData`` empty state).

``_maybe_emit_cartography_map`` is the fix: the region-mode sibling of
``_maybe_emit_dungeon_map``, it fires EVERY region-mode turn (idempotent), so a
single discovered region still ships the graph. These tests drive the helper
directly with a synthetic region-mode world (no live-pack coupling) and assert:

  - a region-mode turn with NO region change still emits a MAP_UPDATE (the bug),
  - the payload carries the full cartography graph + a lie-detector span,
  - non-region-mode (room_graph) worlds are a clean no-op.
"""

from __future__ import annotations

from types import SimpleNamespace

from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.models.world import CartographyConfig, NavigationMode, Region
from sidequest.server.websocket_handlers.map_emit import _maybe_emit_cartography_map


def _burning_peace_regions() -> dict[str, Region]:
    return {
        "edo": Region(
            name="Edo",
            summary="The teahouse capital.",
            description="The teahouse capital.",
            adjacent=["hakone"],
        ),
        "hakone": Region(
            name="Hakone",
            summary="The mountain pass.",
            description="The mountain pass.",
            adjacent=["edo"],
        ),
    }


def _sd(mode: NavigationMode = NavigationMode.region):
    world_obj = SimpleNamespace(
        cartography=CartographyConfig(navigation_mode=mode, regions=_burning_peace_regions())
    )
    pack = SimpleNamespace(worlds={"burning_peace": world_obj})
    return SimpleNamespace(
        genre_pack=pack,
        world_slug="burning_peace",
        genre_slug="elemental_harmony",
        player_id="p1",
    )


def _snapshot(*, current_region: str = "edo", discovered=("edo",)) -> GameSnapshot:
    snap = GameSnapshot(
        genre_slug="elemental_harmony",
        world_slug="burning_peace",
        turn_manager=TurnManager(),
    )
    snap.current_region = current_region
    snap.discovered_regions = list(discovered)
    return snap


def _emits() -> tuple[list, object]:
    sent: list = []

    def emit_fn(msg, wire_type):
        sent.append((wire_type, msg))

    return sent, emit_fn


def test_single_region_no_change_still_emits_map_update():
    """The bug: 1 discovered region + 0 routes, no region change this turn — the
    Map tab must STILL receive the cartography graph (not the empty state)."""
    sent, emit_fn = _emits()
    snap = _snapshot(current_region="edo", discovered=("edo",))

    _maybe_emit_cartography_map(
        object(), sd=_sd(), snapshot=snap, emit_fn=emit_fn, acting_perspective=None
    )

    map_updates = [m for (t, m) in sent if t == "MAP_UPDATE"]
    assert len(map_updates) == 1, (
        f"a region-mode turn must emit a MAP_UPDATE even with no region change; "
        f"got {len(map_updates)} (sent: {[t for (t, _) in sent]})"
    )
    payload = map_updates[0].payload
    # The full authored graph ships — not just the discovered subset.
    assert set(payload.cartography["regions"].keys()) == {"edo", "hakone"}, (
        f"the MAP_UPDATE must carry the full cartography graph; "
        f"got {list(payload.cartography['regions'].keys())}"
    )
    # The single discovered region rides the visited overlay.
    explored_ids = {e.get("id") for e in payload.explored}
    assert "edo" in explored_ids, (
        f"the discovered region must appear in the visited overlay; got {payload.explored}"
    )


def test_emits_lie_detector_span(otel_capture):
    """OTEL principle: the Map-tab seam must be observable on the GM panel — emit
    cartography.map_emitted so a regression (silent skip) is visible."""
    _, emit_fn = _emits()
    snap = _snapshot()

    _maybe_emit_cartography_map(
        object(), sd=_sd(), snapshot=snap, emit_fn=emit_fn, acting_perspective=None
    )

    names = [s.name for s in otel_capture.get_finished_spans()]
    assert any("cartography.map_emitted" in n for n in names), (
        f"the cartography map emit must fire a lie-detector span; got {names}"
    )


def test_room_graph_world_is_a_noop():
    """A room_graph world uses SITE_MAP, not MAP_UPDATE — the cartography
    helper must be a clean no-op (no MAP_UPDATE, no skip span churn)."""
    sent, emit_fn = _emits()
    snap = _snapshot()

    _maybe_emit_cartography_map(
        object(),
        sd=_sd(mode=NavigationMode.room_graph),
        snapshot=snap,
        emit_fn=emit_fn,
        acting_perspective=None,
    )

    assert not [m for (t, m) in sent if t == "MAP_UPDATE"], (
        "a room_graph world must not emit a cartography MAP_UPDATE"
    )
