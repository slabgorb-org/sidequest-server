"""Story 163-8 (RED) — region-change-block integration harness.

Story 163-6 (weather zones, server#1136 + content#529) wired four emit call
sites into ``websocket_session_handler._execute_narration_turn``'s per-turn
broadcast block: the region-change-gated pair —
``_maybe_emit_location_description(room_id_override=current_region)`` and
``_maybe_regenerate_weather_on_region_change`` (fires ``weather.zone_changed``)
— plus the per-turn ``_maybe_emit_dungeon_map`` (SITE_MAP) and
``_maybe_emit_relationships`` projections that ride the same block.

The 163-6 re-review found every one of those helpers is tested ONLY in
isolation (``test_weather_zone_change.py``, ``test_relationships_emit.py``,
``test_location_description_emit.py`` drive the extracted helpers directly).
Nothing drives the production turn, so deleting any of the four call sites
from the handler would pass the whole suite — the exact failure class
"Every Test Suite Needs a Wiring Test" exists to catch.

So every test here drives the **real production turn**
(``_execute_narration_turn``) against the **real shipped glenross world**
(tea_and_murder — ``navigation_mode: region``, two authored climate zones:
``the_glenross_arms``/``the_post_office`` on ``glen_floor``, ``castle_ross``
on ``highland_pass``) and asserts BEHAVIOUR — emitted frames and watcher
events — never source text (CLAUDE.md "No Source-Text Wiring Tests").

Turn-driver scaffolding mirrors
``tests/integration/test_dungeon_room_population_153_23.py`` (the house
production-turn harness): stub orchestrator/local_dm/validator so the turn
runs without a live narrator; the region move itself is engine-deterministic —
the stubbed narration result carries a ``location`` heading that
``narration_apply`` resolves to a cartography region and advances
``current_region`` through the real apply path.

The region change is REAL: the party starts seated in ``the_glenross_arms``
and the narrator heading "Castle Ross" crosses it into ``castle_ross``
(zone ``glen_floor`` → ``highland_pass``) through the production seam.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import sidequest.server.websocket_handlers.map_emit as map_emit
import sidequest.server.websocket_session_handler as wsh
from sidequest.game.monster_manual import MonsterManual
from sidequest.game.weather import WeatherGenerator
from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path
from tests.game.test_disposition_beat import _npc

# Plain helpers (not fixtures) live in tests/server/conftest; session_fixture +
# otel_capture are re-exported into tests/integration via this dir's conftest.
from tests.server.conftest import (
    _build_turn_context_for_test,
    _make_minimal_narration_turn_result,
)

_content_required = pytest.mark.skipif(
    not GENRE_PACKS_DIR.is_dir(), reason="sidequest-content not on disk"
)


@pytest.fixture(autouse=True)
def _hermetic_sidecar(monkeypatch):
    """Hermeticity (152-5 house pattern): the post-narration sidecar-extraction
    watcher builds a live SDK-backed LLM and would reach the real
    claude-agent-sdk ``query()`` transport — ``run_narration_turn`` (stubbed
    per-test) does not cover this seam. Stub the watcher itself."""
    monkeypatch.setattr(
        "sidequest.server.websocket_session_handler.run_sidecar_extraction_watcher",
        AsyncMock(return_value=None),
    )


# Real glenross cartography regions (content#529 authored the zone split).
_ARMS_ID = "the_glenross_arms"  # zone: glen_floor
_ARMS_NAME = "The Glenross Arms"
_CASTLE_ID = "castle_ross"  # zone: highland_pass
_CASTLE_NAME = "Castle Ross"
_POST_OFFICE_ID = "the_post_office"  # zone: glen_floor
_POST_OFFICE_NAME = "The Post Office"

_GLEN_ZONE = "glen_floor"
_HIGHLAND_ZONE = "highland_pass"


# ---------------------------------------------------------------------------
# Turn-driver scaffolding — run the REAL production narration turn.
# ---------------------------------------------------------------------------


def _fake_local_dm() -> MagicMock:
    """Dormant LocalDM stub — empty DispatchPackage keeps the turn off the
    decompose path so the tests isolate the emit block."""
    from sidequest.protocol.dispatch import DispatchPackage

    fake = MagicMock()
    fake.decompose = AsyncMock(
        return_value=DispatchPackage(
            turn_id="t-163-8",
            per_player=[],
            cross_player=[],
            confidence_global=0.0,
        )
    )
    return fake


def _arm_handler_for_turn(sd, handler, *, location: str | None) -> None:
    """Stub the narrator/validator collaborators so ``_execute_narration_turn``
    completes without a live LLM. ``location`` is the narration result's scene
    heading — the engine-deterministic region-advance driver: a heading naming
    a cartography region makes ``narration_apply`` advance ``current_region``
    through the real seam; ``None`` leaves the party where it stands."""
    result = _make_minimal_narration_turn_result()
    result.location = location

    async def _run(action: str, turn_context: object, *, room: object = None) -> object:
        return result

    sd.orchestrator.run_narration_turn = AsyncMock(side_effect=_run)
    sd.local_dm = _fake_local_dm()
    handler._validator = MagicMock()
    handler._validator.submit = AsyncMock()
    handler._validator.is_running = MagicMock(return_value=True)


def _bind_glenross(sd):
    """Graft the REAL tea_and_murder pack's worlds + source tree onto the
    fixture pack and point the session at glenross.

    Same graft shape as 153-23's ``_graft_real_caverns_pack``: the fixture's
    light RulesConfig()/ProgressionConfig stay (turn machinery unchanged, the
    fate ruleset path is out of this story's scope) while the WORLD — the
    thing the region-change block reads — is the real shipped model:
    ``worlds`` (cartography + regions + zones + map treatment) and the
    world dir (weather.yaml, history.yaml) both resolve to real content.

    Returns the glenross world dir."""
    from sidequest.genre.loader import load_genre_pack

    pack_dir = find_pack_path("tea_and_murder")
    real = load_genre_pack(pack_dir)
    sd.genre_pack.worlds = real.worlds
    sd.genre_pack.source_dir = real.source_dir
    sd.genre_slug = "tea_and_murder"
    sd.world_slug = "glenross"
    sd.snapshot.genre_slug = "tea_and_murder"
    sd.snapshot.world_slug = "glenross"
    world_dir = pack_dir / "worlds" / "glenross"
    # session_world_dir prefers the recorded Path — the connect handler records
    # it in production; the harness mirrors that binding.
    sd.world_dir = world_dir
    # Weather-regen seed derivation requires a real game_slug (No Silent
    # Fallbacks — regenerate_weather_for_region raises on None).
    sd.game_slug = "glenross-163-8"
    # Empty real Manual so ensure_loaded short-circuits — no bestiary machinery
    # in these tests (153-23's trick).
    sd.monster_manual = MonsterManual(genre="tea_and_murder", world="glenross")
    return world_dir


def _seat_weather(sd, world_dir, *, zone: str = _GLEN_ZONE) -> None:
    """Mirror the 163-6 bootstrap cache: real generator over the real
    glenross weather.yaml + a live state sampled from ``zone``. (Bootstrap
    wiring itself is 163-6's covered territory; this harness owns the
    on-move re-gen call site.)"""
    gen = WeatherGenerator(world_dir / "weather.yaml")
    sd.weather_generator = gen
    sd.weather_season = "spring"
    sd.weather_state = gen.generate(zone, "spring", seed=1)


def _seat_party_in(sd, region_id: str) -> None:
    """Seat the fixture PC in ``region_id`` on every region axis the block
    reads: spawn anchor (current_region), per-PC graph truth (pc_regions),
    and fog-of-war (discovered_regions)."""
    snap = sd.snapshot
    snap.current_region = region_id
    snap.pc_regions["TestHero"] = region_id
    if region_id not in snap.discovered_regions:
        snap.discovered_regions.append(region_id)


@pytest.fixture
def watcher_events(monkeypatch) -> list[dict]:
    """Capture every watcher event the turn publishes from BOTH modules that
    own the guarded call sites — the handler (narrator.region_patch_check)
    and map_emit (weather/location/dungeon emits). Patch-where-used: each
    module binds ``publish_event`` as ``_watcher_publish`` at import time."""
    captured: list[dict] = []

    def _capture(event_type: str, fields: dict, **kwargs) -> None:
        captured.append({"event_type": event_type, "fields": fields, **kwargs})

    monkeypatch.setattr(map_emit, "_watcher_publish", _capture)
    monkeypatch.setattr(wsh, "_watcher_publish", _capture)
    return captured


def _record_frames(sd, handler, monkeypatch) -> list:
    """Record every shared-world frame the turn broadcasts. The production
    ``_emit_shared_world_frame`` closure broadcasts through the HANDLER's
    ``_room`` (slug-connect binds it; the fixture only binds ``sd._room``),
    so bind it here exactly as the connect path does, then replace
    ``broadcast`` to observe the real dispatch seam without connected
    sockets."""
    frames: list = []
    handler._room = sd._room
    monkeypatch.setattr(
        sd._room,
        "broadcast",
        lambda msg, exclude_socket_id=None: frames.append(msg),
    )
    return frames


async def _drive_turn(
    sd,
    handler,
    *,
    location: str | None,
    action: str = "We take the high road toward the castle.",
) -> None:
    _arm_handler_for_turn(sd, handler, location=location)
    turn_context = _build_turn_context_for_test(sd)
    await handler._execute_narration_turn(sd, action, turn_context)


def _events(watcher_events: list[dict], event_type: str) -> list[dict]:
    return [e for e in watcher_events if e["event_type"] == event_type]


def _frames_of(frames: list, kind: str) -> list:
    return [m for m in frames if str(getattr(m, "type", "")).endswith(kind)]


def _assert_region_branch_ran(watcher_events: list[dict], *, changed: bool) -> dict:
    """Harness anchor: ``narrator.region_patch_check`` fires on EVERY
    region-mode turn (163-6's lie-detector), so its presence proves the drive
    reached the region-mode branch — isolating "the harness never got there"
    from "the guarded emit is unwired"."""
    checks = _events(watcher_events, "narrator.region_patch_check")
    assert checks, (
        "narrator.region_patch_check never fired — the drive did not reach the "
        "region-mode branch of _execute_narration_turn (harness precondition, "
        "not the guarded emit): either the turn crashed earlier or the world "
        "did not bind as a region-mode world"
    )
    last = checks[-1]
    assert last["fields"]["region_changed"] is changed, (
        f"region_patch_check saw region_changed={last['fields']['region_changed']} "
        f"(expected {changed}) — the region advance itself did not behave as the "
        f"test staged it: {last['fields']}"
    )
    return last


# ---------------------------------------------------------------------------
# weather.zone_changed — the region-change-gated re-gen call site
# (websocket_session_handler → _maybe_regenerate_weather_on_region_change).
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
@_content_required
async def test_region_change_turn_fires_weather_zone_changed(
    session_fixture, watcher_events, monkeypatch
):
    """Crossing glen_floor → highland_pass through the PRODUCTION turn must
    re-sample the session weather and publish exactly one
    ``weather.zone_changed`` from the handler's region-change block.

    The 163-6 unit tests drive the extracted helper directly; this is the
    wiring guard that the handler actually CALLS it on a real region change."""
    sd, handler = session_fixture
    try:
        world_dir = _bind_glenross(sd)
    except PackNotFound:
        pytest.skip("tea_and_murder not on disk")
    _seat_weather(sd, world_dir, zone=_GLEN_ZONE)
    _seat_party_in(sd, _ARMS_ID)

    await _drive_turn(sd, handler, location=_CASTLE_NAME)

    _assert_region_branch_ran(watcher_events, changed=True)
    hits = _events(watcher_events, "weather.zone_changed")
    assert len(hits) == 1, (
        "weather.zone_changed did not fire from the production turn — the "
        "region-change block never called _maybe_regenerate_weather_on_region_"
        f"change (events seen: {sorted({e['event_type'] for e in watcher_events})})"
    )
    fields = hits[0]["fields"]
    assert fields["from_zone"] == _GLEN_ZONE, fields
    assert fields["to_zone"] == _HIGHLAND_ZONE, fields
    assert fields["region"] == _CASTLE_ID, fields
    assert hits[0]["component"] == "location"
    assert sd.weather_state is not None and sd.weather_state.zone == _HIGHLAND_ZONE, (
        "the event fired but the session weather never re-sampled — "
        f"weather_state.zone={getattr(sd.weather_state, 'zone', None)!r}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
@_content_required
async def test_same_zone_region_change_does_not_refire_weather(
    session_fixture, watcher_events, monkeypatch
):
    """Negative invariant (No Silent Fallbacks pairing): a REAL region change
    within the SAME climate zone (the_glenross_arms → the_post_office, both
    glen_floor) must NOT fire weather.zone_changed nor touch the state —
    through the production path, not the unit-tested helper."""
    sd, handler = session_fixture
    try:
        world_dir = _bind_glenross(sd)
    except PackNotFound:
        pytest.skip("tea_and_murder not on disk")
    _seat_weather(sd, world_dir, zone=_GLEN_ZONE)
    _seat_party_in(sd, _ARMS_ID)
    state_before = sd.weather_state

    await _drive_turn(sd, handler, location=_POST_OFFICE_NAME)

    _assert_region_branch_ran(watcher_events, changed=True)
    assert not _events(watcher_events, "weather.zone_changed"), (
        "weather.zone_changed fired on a same-zone region move — the "
        "zone-unchanged gate does not hold through the production path"
    )
    assert sd.weather_state is state_before, (
        "the session weather was re-sampled on a same-zone move"
    )


# ---------------------------------------------------------------------------
# LOCATION_DESCRIPTION — the region-change-gated emit
# (websocket_session_handler → _maybe_emit_location_description with
# room_id_override=current_region).
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
@_content_required
async def test_region_change_turn_emits_location_description(
    session_fixture, watcher_events, monkeypatch
):
    """A region change through the production turn must emit a
    LOCATION_DESCRIPTION frame for the NEW region (the Location-tab refresh
    the frozen-panel playtest bug was about) plus its
    ``location_description.emitted`` watcher event."""
    sd, handler = session_fixture
    try:
        world_dir = _bind_glenross(sd)
    except PackNotFound:
        pytest.skip("tea_and_murder not on disk")
    _seat_weather(sd, world_dir, zone=_GLEN_ZONE)
    _seat_party_in(sd, _ARMS_ID)
    frames = _record_frames(sd, handler, monkeypatch)

    await _drive_turn(sd, handler, location=_CASTLE_NAME)

    _assert_region_branch_ran(watcher_events, changed=True)
    emitted = [
        e
        for e in _events(watcher_events, "location_description.emitted")
        if e["fields"].get("room_id") == _CASTLE_ID
    ]
    assert emitted, (
        "location_description.emitted never fired for the NEW region — the "
        "region-change block did not call _maybe_emit_location_description "
        f"with room_id_override=current_region (events seen: "
        f"{sorted({e['event_type'] for e in watcher_events})})"
    )
    loc_frames = [
        m
        for m in _frames_of(frames, "LOCATION_DESCRIPTION")
        if getattr(m.payload, "region_id", None) == _CASTLE_ID
    ]
    assert loc_frames, (
        "no LOCATION_DESCRIPTION frame for castle_ross was broadcast — the "
        "event fired but the typed frame never reached the shared-world "
        f"broadcast seam (frames seen: {[str(getattr(m, 'type', '')) for m in frames]})"
    )
    payload = loc_frames[-1].payload
    assert payload.region_name == _CASTLE_NAME, payload
    assert payload.prose, "authored glenross region description did not reach the payload"


@pytest.mark.integration
@pytest.mark.asyncio
@_content_required
async def test_unchanged_region_holds_the_region_change_gate(
    session_fixture, watcher_events, monkeypatch
):
    """Negative invariant: a turn whose heading resolves to the region the
    party is ALREADY in (same-region scene drift) must fire NEITHER
    location_description for a region change NOR weather re-gen — the
    ``region_changed`` gate holds through the production path."""
    sd, handler = session_fixture
    try:
        world_dir = _bind_glenross(sd)
    except PackNotFound:
        pytest.skip("tea_and_murder not on disk")
    _seat_weather(sd, world_dir, zone=_GLEN_ZONE)
    _seat_party_in(sd, _ARMS_ID)
    frames = _record_frames(sd, handler, monkeypatch)

    await _drive_turn(sd, handler, location=_ARMS_NAME)

    _assert_region_branch_ran(watcher_events, changed=False)
    assert not _events(watcher_events, "location_description.emitted"), (
        "location_description.emitted fired without a region change — the "
        "region_changed gate does not hold through the production path"
    )
    assert not _frames_of(frames, "LOCATION_DESCRIPTION"), (
        "a LOCATION_DESCRIPTION frame was broadcast without a region change"
    )
    assert not _events(watcher_events, "weather.zone_changed"), (
        "weather.zone_changed fired without a region change"
    )


# ---------------------------------------------------------------------------
# RELATIONSHIPS — the per-turn roster projection riding the same block
# (websocket_session_handler → _maybe_emit_relationships).
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
@_content_required
async def test_turn_emits_relationships_roster(
    session_fixture, watcher_events, otel_capture, monkeypatch
):
    """A turn with an encountered NPC in the snapshot must broadcast a
    RELATIONSHIPS frame (ADR-136 roster projection) from the production
    per-turn block, and the ``relationships.emitted`` span must fire —
    the GM-panel proof the projection engaged."""
    from sidequest.telemetry.spans import SPAN_RELATIONSHIPS_EMITTED

    sd, handler = session_fixture
    try:
        world_dir = _bind_glenross(sd)
    except PackNotFound:
        pytest.skip("tea_and_murder not on disk")
    _seat_weather(sd, world_dir, zone=_GLEN_ZONE)
    _seat_party_in(sd, _ARMS_ID)
    npc = _npc("Tabitha")
    npc.last_seen_turn = 1
    npc.record_disposition_beat(turn=1, delta=3, reason="candor", location="parlor")
    sd.snapshot.npcs.append(npc)
    frames = _record_frames(sd, handler, monkeypatch)

    await _drive_turn(sd, handler, location=_CASTLE_NAME)

    rel_frames = _frames_of(frames, "RELATIONSHIPS")
    assert rel_frames, (
        "no RELATIONSHIPS frame was broadcast — the per-turn block never "
        "called _maybe_emit_relationships (frames seen: "
        f"{[str(getattr(m, 'type', '')) for m in frames]})"
    )
    names = [entry.name for entry in rel_frames[-1].payload.entries]
    assert "Tabitha" in names, names
    rel_spans = [
        s for s in otel_capture.get_finished_spans() if s.name == SPAN_RELATIONSHIPS_EMITTED
    ]
    assert rel_spans, (
        "relationships.emitted span never fired from the production turn — "
        f"spans seen: {sorted({s.name for s in otel_capture.get_finished_spans()})}"
    )


# ---------------------------------------------------------------------------
# SITE_MAP (dungeon-map) — the per-turn map projection riding the same block
# (websocket_session_handler → _maybe_emit_dungeon_map). Glenross declares no
# sites, so the honest per-turn observable is the emitter's own LOUD skip:
# an unseated pc_region publishes dungeon.map_skipped(no_pc_region) — proof
# the call site executes every turn. (The full SITE_MAP emit body is 164-4's
# unit-tested territory; the handler call site is what 163-8 guards.)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
@_content_required
async def test_turn_reaches_site_map_emit_call_site(session_fixture, watcher_events, monkeypatch):
    """Every narration turn must reach _maybe_emit_dungeon_map. With the PC
    deliberately missing from pc_regions, the emitter's OP1 guard publishes
    ``dungeon.map_skipped(reason=no_pc_region)`` — a loud, deterministic
    proof the production block called it. If the call site is deleted, no
    dungeon.* event fires at all and this test catches the unwiring."""
    sd, handler = session_fixture
    try:
        world_dir = _bind_glenross(sd)
    except PackNotFound:
        pytest.skip("tea_and_murder not on disk")
    _seat_weather(sd, world_dir, zone=_GLEN_ZONE)
    # Spawn anchor set, but NO pc_regions entry and a location-less narration
    # result — nothing seeds region_for(), so the OP1 skip is deterministic.
    sd.snapshot.current_region = _ARMS_ID
    if _ARMS_ID not in sd.snapshot.discovered_regions:
        sd.snapshot.discovered_regions.append(_ARMS_ID)

    await _drive_turn(sd, handler, location=None)

    skips = [
        e
        for e in _events(watcher_events, "dungeon.map_skipped")
        if e["fields"].get("reason") == "no_pc_region"
    ]
    assert skips, (
        "dungeon.map_skipped(no_pc_region) never fired — the per-turn block "
        "did not call _maybe_emit_dungeon_map (events seen: "
        f"{sorted({e['event_type'] for e in watcher_events})})"
    )
    assert skips[0]["fields"]["pc_name"] == "TestHero", skips[0]["fields"]
