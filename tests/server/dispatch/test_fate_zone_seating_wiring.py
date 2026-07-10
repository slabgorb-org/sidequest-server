"""RED wiring tests for Task 13 — Fate zone projection at conflict seating
(ADR-096 v2, Track C3: production reachability).

Unit tests prove ``project_conflict_zones`` works in isolation; this suite
proves it is REACHABLE FROM PRODUCTION (Every Test Suite Needs a Wiring Test):
when a Fate-bound pack seats a conflict on a room with a persisted tactical
grid, ``instantiate_encounter_from_trigger`` must project zones right after the
Task-8 cell seating — gated on ``isinstance(ruleset, FateRulesetModule)`` so
WN/dial packs are untouched.

Fixture shape: the live ``pulp_noir`` Fate pack through the production loader
(model: test_153_9_fate_other_seating.py) + the persisted-mask/RegionTactical
shape reused from tests/server/tactical_emit_fixtures.py. The DUMBBELL mask is
hand-verified (see test_zones.py): entrance anchor (1,1) is in the TOP lobe,
creature anchor (5,3) in the BOTTOM lobe — so a correctly-wired projection
seats the two sides in DIFFERENT zones.

Span capture: ``tactical.zone.projected`` must reach the turn_telemetry sink
via ``publish_event`` (GM-panel lie detector), so the capture monkeypatches the
spans module's ``publish_event`` AND installs a recording tracer (the mirror
skips NonRecordingSpans — test_tactical_telemetry_sink.py's documented plan-doc
bug class).

RED/GREEN map: the two Fate-path tests FAIL until Task 13 is wired; the WN-gate
and no-grid tests pin existing no-op boundaries and are stable across RED and
GREEN (model: test_encounter_position_seating.py's no-store guard).
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import sidequest.telemetry.spans as spans_module
import sidequest.telemetry.spans.tactical as tac
from sidequest.agents.orchestrator import NpcMention
from sidequest.dungeon.tactical import RegionTactical, TokenAnchor
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.loader import load_genre_pack
from sidequest.server.dispatch.encounter_lifecycle import instantiate_encounter_from_trigger
from tests._helpers.genre_paths import PackNotFound, find_pack_path
from tests.server.tactical_emit_fixtures import (
    _FakeDungeonStore,
    _runtime_mask_dict,
    build_sd_with_tactical_region,
)

_FATE_PACK = "pulp_noir"
_ROOM_ID = "region_fate_zone_wiring"

# Hand-verified two-lobe cavern (see tests/game/tactical/test_zones.py).
_DUMBBELL_ROWS = [
    "#######",
    "#.....#",
    "#.#.#.#",
    "#.....#",
    "#######",
]

# WN initiative rolls 1d8+DEX at seating; the caverns negative-gate test needs
# a stat block (model: test_encounter_position_seating.py).
_WN_STATS = {"STR": 12, "DEX": 12, "CON": 12, "INT": 12, "WIS": 12, "CHA": 12}


def _has_fate_content() -> bool:
    try:
        find_pack_path(_FATE_PACK)
        return True
    except PackNotFound:
        return False


_needs_fate_pack = pytest.mark.skipif(
    not _has_fate_content(), reason=f"{_FATE_PACK} pack not on disk"
)


def _combat_encounter_type(pack) -> str:
    """The pack's first category=='combat' confrontation type (no hardcoding —
    survives a content rename of the confrontation key)."""
    for cdef in pack.rules.confrontations or []:
        if cdef.category == "combat":
            return cdef.confrontation_type
    raise AssertionError(f"{_FATE_PACK} authors no combat-category confrontation")


def _fate_gridded_session():
    """(pack, snapshot, dungeon_store) for a pulp_noir conflict seated on a
    gridded room: entrance anchor (1,1) for the PC, creature anchor (5,3) for
    the Other — opposite lobes of the DUMBBELL."""
    pack = load_genre_pack(find_pack_path(_FATE_PACK))
    tactical = RegionTactical(
        region_id=_ROOM_ID,
        features=[],
        anchors=[TokenAnchor((1, 1), "entrance"), TokenAnchor((5, 3), "creature")],
        pois=[],
        exit_thresholds={},
    )
    mask = _runtime_mask_dict(_DUMBBELL_ROWS)
    mask["tactical"] = tactical.to_dict()
    store = _FakeDungeonStore({_ROOM_ID: mask})

    snap = GameSnapshot(
        genre_slug=_FATE_PACK,
        world_slug="case_files",
        turn_manager=TurnManager(interaction=5),
    )
    snap.character_locations["Sam"] = _ROOM_ID
    return pack, snap, store


def _instantiate_fate_conflict(pack, snap, store):
    return instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type=_combat_encounter_type(pack),
        player_name="Sam",
        npcs_present=[],
        genre_slug=_FATE_PACK,
        materialized_threat=NpcMention(name="Silas Vance", role="hostile", side="opponent"),
        dungeon_store=store,
    )


@pytest.fixture
def zone_span_capture(monkeypatch):
    """Recording tracer + captured publish_event — returns the published list."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    local = provider.get_tracer("test-fate-zone-seating-wiring")
    monkeypatch.setattr(spans_module, "tracer", lambda: local)
    published: list[tuple] = []
    monkeypatch.setattr(
        tac, "publish_event", lambda et, fields, **kw: published.append((et, fields, kw))
    )
    return published


def _zone_projected_events(published: list[tuple]) -> list[tuple]:
    return [
        (et, fields, kw)
        for et, fields, kw in published
        if fields.get("op") == "tactical.zone.projected"
    ]


# --- The headline wiring: Fate conflict on a grid projects zones -----------------------


@_needs_fate_pack
def test_fate_conflict_on_grid_projects_zones_and_seats_actors():
    """After a Fate-bound pack seats a conflict on a gridded room, the encounter
    carries zones and every cell-seated actor carries a zone — the inert
    ADR-144 slots are live FROM PRODUCTION, not just unit-tested."""
    pack, snap, store = _fate_gridded_session()
    enc = _instantiate_fate_conflict(pack, snap, store)

    assert enc is not None, "Fate conflict failed to instantiate"
    assert snap.encounter is enc, "the trigger must write the encounter to the snapshot"
    assert enc.zones, (
        "encounter.zones is empty after seating a Fate conflict on a gridded "
        "room — project_conflict_zones is not reachable from production"
    )
    zones_by_name = {a.name: a.per_actor_state.get("zone") for a in enc.actors}
    for name, zone in zones_by_name.items():
        assert zone in enc.zones, f"{name} carries no valid zone; got {zones_by_name}"
    # Entrance (1,1) and creature (5,3) anchors sit in opposite lobes of the
    # DUMBBELL — a correct projection must NOT collapse the sides into one zone.
    assert zones_by_name["Sam"] != zones_by_name["Silas Vance"], (
        f"PC and Other were zoned together across the pinch; got {zones_by_name}"
    )


@_needs_fate_pack
def test_fate_zone_projection_span_fires_at_seating(zone_span_capture):
    """The projection decision must be observable on the GM panel from the
    PRODUCTION path — ``tactical.zone.projected`` mirrored into the
    turn_telemetry sink at conflict instantiation (OTEL lie detector)."""
    pack, snap, store = _fate_gridded_session()
    enc = _instantiate_fate_conflict(pack, snap, store)

    assert enc is not None
    events = _zone_projected_events(zone_span_capture)
    assert events, (
        "tactical.zone.projected never reached the sink at Fate conflict "
        "seating — the GM panel cannot tell the projection engaged; saw ops "
        f"{sorted({f.get('op') for _, f, _ in zone_span_capture if f.get('op')})}"
    )
    _, fields, kw = events[0]
    assert kw["component"] == "tactical"
    assert fields["zone_count"] == 2, "the DUMBBELL room projects exactly two zones"
    # Review round 1 [MEDIUM] B: the outcome count rides the production span too.
    assert fields["placed_count"] == 2, "both seated actors must be counted as placed"


# --- Fail-loud guards: impossible states raise, they don't silently skip ---------------
# Review round 1 [MEDIUM] K (RED): the Task-13 gate silently `return`ed on two
# states the codebase's own construction rules make impossible — a tactical
# block without persisted mask bytes (materializer only merges tactical into a
# dict that already carries mask_bytes_b64) and a gridded seat with no pack
# ruleset (GenrePack.rules is required; `_raise_missing_ruleset` doctrine: "a
# missing ruleset is a configuration error"). Impossible states fail loud — the
# sibling legitimate state (room with no tactical block) keeps its honest-skip
# span, unchanged.


@_needs_fate_pack
def test_seating_fails_loud_on_tactical_block_without_mask_bytes():
    """A persisted mask dict carrying a ``tactical`` block but NO
    ``mask_bytes_b64`` is a data-integrity violation no write path can produce —
    reaching it means the store is corrupt, and the seam must raise, not
    silently skip the projection (No Silent Fallbacks)."""
    pack, snap, store = _fate_gridded_session()
    corrupt = store.load_masks()[_ROOM_ID]
    del corrupt["mask_bytes_b64"]
    corrupt_store = _FakeDungeonStore({_ROOM_ID: corrupt})

    with pytest.raises(ValueError, match="mask_bytes_b64"):
        _instantiate_fate_conflict(pack, snap, corrupt_store)


def test_seating_fails_loud_on_missing_pack_ruleset():
    """Once a tactical grid is live, a missing pack/ruleset is a configuration
    error (`_raise_missing_ruleset` doctrine), never a silent skip. Drives the
    seating helper directly — the production trigger cannot produce a None pack,
    which is exactly why the guard must raise if it ever fires."""
    from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
    from sidequest.server.dispatch.encounter_lifecycle import _seat_tactical_cells

    _pack, snap, store = _fate_gridded_session()
    enc = StructuredEncounter(
        encounter_type="social",
        player_metric=EncounterMetric(name="advantage", threshold=3),
        opponent_metric=EncounterMetric(name="advantage", threshold=3),
        actors=[EncounterActor(name="Sam", role="combatant", side="player")],
    )

    with pytest.raises(ValueError, match="ruleset"):
        _seat_tactical_cells(
            encounter=enc,
            snapshot=snap,
            dungeon_store=store,
            player_name="Sam",
            pack=None,
        )


# --- Negative gates: WN untouched, no-grid is a clean no-op ----------------------------


def test_wn_pack_on_same_grid_projects_no_zones(zone_span_capture):
    """The capability gate keys on ``isinstance(ruleset, FateRulesetModule)`` —
    a WWN pack seating combat on the SAME kind of gridded room must get cell
    seating (Task 8) but NO zone projection: zones stay empty, no actor grows a
    'zone', and the projection span never fires. Stable across RED and GREEN."""
    sd, snap, _room_id = build_sd_with_tactical_region(creature_revealed=False)
    snap.characters[0].stats.update(_WN_STATS)

    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=sd.genre_pack,
        encounter_type="combat",
        player_name="Rux",
        npcs_present=[NpcMention(name="rope-spider", side="opponent")],
        genre_slug="caverns_and_claudes",
        allow_synthetic_opponent=True,
        dungeon_store=sd.dungeon_store,
    )
    assert enc is not None
    # Discriminate gate-off from grid-dead: cells WERE seated (the grid is
    # live), so an empty zones list is the Fate gate, not a broken fixture.
    assert any(a.per_actor_state.get("cell") for a in enc.actors), (
        "fixture grid never seated cells — this negative test would pass vacuously"
    )
    assert enc.zones == [], f"a WWN pack grew Fate zones: {enc.zones}"
    for actor in enc.actors:
        assert "zone" not in actor.per_actor_state, (
            f"{actor.name} was zone-seated under a WWN binding — the isinstance gate leaked"
        )
    assert not _zone_projected_events(zone_span_capture), (
        "tactical.zone.projected fired for a WWN pack — the gate must keep WN/dial packs untouched"
    )


@_needs_fate_pack
def test_fate_conflict_without_grid_is_a_clean_noop(zone_span_capture):
    """A Fate conflict seated with NO dungeon_store (region-mode session, no
    tactical grid) seats normally and projects nothing — no zones, no crash
    (No Silent Fallbacks: absence stays visible as absence). Stable across RED
    and GREEN."""
    pack, snap, _store = _fate_gridded_session()
    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type=_combat_encounter_type(pack),
        player_name="Sam",
        npcs_present=[],
        genre_slug=_FATE_PACK,
        materialized_threat=NpcMention(name="Silas Vance", role="hostile", side="opponent"),
    )
    assert enc is not None, "grid-less Fate seating must still instantiate"
    assert enc.zones == [], "zones appeared without any tactical grid"
    for actor in enc.actors:
        assert "zone" not in actor.per_actor_state
    assert not _zone_projected_events(zone_span_capture), (
        "tactical.zone.projected fired with no grid to project"
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
