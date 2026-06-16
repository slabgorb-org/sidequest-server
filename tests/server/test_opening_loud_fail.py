"""Loud-fail watcher events for the opening-directive populator.

Bug: opening narration skips Kestrel beat (sq-playtest-pingpong.md, found
by Keith). Root cause traced by SM: ``_populate_opening_directive_on_
chargen_complete`` had four ``return  # defensive`` paths that ALL silently
bailed out — and one of them (``OpeningResolutionError`` via
min_players=2 unmet on first commit) was the active cause. Sebastien's GM
panel had no signal that the canned opening was even *attempted*; the
warning ``opening.skipped_reason=...`` never existed.

These tests pin the watcher emissions so the next regression surfaces
immediately. Per CLAUDE.md OTEL principle: every subsystem decision must
be observable from the GM panel.

Sibling fix: deferral gate (``_should_fire_opening_narration``) below.
The gate stops first committers in MP from getting improvised narration
when the canned MP opening can't resolve until the second PC commits.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import sidequest.server.session_handler  # noqa: F401 — ordering side-effect
from sidequest.game.region_init import RegionInitError
from sidequest.server.websocket_session_handler import (
    _bind_current_region_from_opening,
    _populate_opening_directive_on_chargen_complete,
    _should_fire_opening_narration,
)


@pytest.fixture
def captured_events(monkeypatch) -> list[tuple[str, dict, dict]]:
    """Capture every ``_watcher_publish`` call made during the test."""
    captured: list[tuple[str, dict, dict]] = []

    def fake_publish(
        event_type: str,
        fields: dict[str, Any],
        *,
        component: str = "",
        severity: str = "info",
    ) -> None:
        captured.append((event_type, fields, {"component": component, "severity": severity}))

    monkeypatch.setattr(
        "sidequest.server.websocket_session_handler._watcher_publish",
        fake_publish,
    )
    return captured


def _session_data(opening_directive: object | None = None) -> SimpleNamespace:
    """Minimal duck-typed _SessionData stand-in.

    The populator only reads ``opening_directive`` and writes
    ``opening_seed`` / ``opening_directive`` / ``_resolved_opening_id``.
    SimpleNamespace is sufficient — the real _SessionData has many more
    fields the populator never touches.
    """
    return SimpleNamespace(
        opening_directive=opening_directive,
        opening_seed=None,
        _resolved_opening_id=None,
        genre_slug="test_genre",
        world_slug="test_world",
        player_name="Tester",
    )


def _empty_snapshot() -> SimpleNamespace:
    return SimpleNamespace(characters=[])


def _snapshot_with_pc() -> SimpleNamespace:
    pc = SimpleNamespace(
        core=SimpleNamespace(name="Itchy"),
        background="Far Landing Raised Me",
        drive="vengeance",
        first_name="Itchy",
        last_name="Vasquez",
        nickname="",
    )
    return SimpleNamespace(characters=[pc])


def _pack_with_world(world: object | None) -> SimpleNamespace:
    """A pack whose .worlds.get(world_slug) returns ``world``."""
    return SimpleNamespace(worlds={"test_world": world} if world is not None else {})


def _world_with_openings(openings: list) -> SimpleNamespace:
    return SimpleNamespace(
        openings=openings,
        chassis_instances=[],
        authored_npcs=[],
        magic_register="",
    )


# ---- loud-fail watcher events ----------------------------------------


def test_populate_emits_skip_event_on_empty_snapshot(captured_events) -> None:
    """Empty snapshot at populator entry must emit
    ``opening.skipped_reason=empty_snapshot`` (was a silent ``return
    # defensive``).
    """
    sd = _session_data()
    _populate_opening_directive_on_chargen_complete(
        session_data=sd,
        snapshot=_empty_snapshot(),
        pack=_pack_with_world(_world_with_openings([])),
        world_slug="test_world",
        mode="multiplayer",
    )

    skip_events = [
        (fields, meta) for et, fields, meta in captured_events if et == "opening.skipped"
    ]
    assert skip_events, (
        "expected opening.skipped watcher event on empty-snapshot bail; "
        f"captured: {captured_events}"
    )
    fields, meta = skip_events[0]
    assert fields["reason"] == "empty_snapshot"
    assert meta["component"] == "opening_hook"
    assert meta["severity"] == "warning"
    # Populator did NOT populate (preserves prior return-without-side-effect).
    assert sd.opening_directive is None
    assert sd.opening_seed is None


def test_populate_emits_skip_event_when_world_missing(captured_events) -> None:
    """Pack with no matching world → ``reason=world_or_openings_missing``.

    Validator-7 should make this unreachable at load time, but the
    populator still has the defensive branch — it should now be loud.
    """
    sd = _session_data()
    _populate_opening_directive_on_chargen_complete(
        session_data=sd,
        snapshot=_snapshot_with_pc(),
        pack=_pack_with_world(None),
        world_slug="test_world",
        mode="multiplayer",
    )
    skip_events = [
        (fields, meta) for et, fields, meta in captured_events if et == "opening.skipped"
    ]
    assert skip_events
    fields, meta = skip_events[0]
    assert fields["reason"] == "world_or_openings_missing"
    assert meta["severity"] == "warning"


def test_populate_emits_skip_event_when_world_has_no_openings(captured_events) -> None:
    """World present but ``openings=[]`` → ``reason=world_or_openings_missing``."""
    sd = _session_data()
    _populate_opening_directive_on_chargen_complete(
        session_data=sd,
        snapshot=_snapshot_with_pc(),
        pack=_pack_with_world(_world_with_openings([])),
        world_slug="test_world",
        mode="multiplayer",
    )
    skip_events = [
        (fields, meta) for et, fields, meta in captured_events if et == "opening.skipped"
    ]
    assert skip_events
    fields, meta = skip_events[0]
    assert fields["reason"] == "world_or_openings_missing"


def test_populate_emits_skip_event_on_resolution_failed(captured_events, monkeypatch) -> None:
    """The Kestrel-skip bug's *actual* trigger: opening bank exists but
    ``_resolve_opening_post_chargen`` raises ``OpeningResolutionError``
    because the only matching opening has ``min_players=2`` and only
    one PC has committed yet. Must emit
    ``opening.skipped_reason=resolution_failed`` so Sebastien sees
    "the resolver tried, no opening matched, here's why" instead of
    silence + improvised customs prose.
    """
    from sidequest.server.dispatch.opening import OpeningResolutionError

    fake_opening = SimpleNamespace(id="mp_galley_jumprest")  # not actually returned

    def boom(*args, **kwargs):
        raise OpeningResolutionError("no opening matches mode=multiplayer player_count=1")

    monkeypatch.setattr(
        "sidequest.server.websocket_session_handler._resolve_opening_post_chargen",
        boom,
    )

    sd = _session_data()
    _populate_opening_directive_on_chargen_complete(
        session_data=sd,
        snapshot=_snapshot_with_pc(),
        pack=_pack_with_world(_world_with_openings([fake_opening])),
        world_slug="test_world",
        mode="multiplayer",
    )

    skip_events = [
        (fields, meta) for et, fields, meta in captured_events if et == "opening.skipped"
    ]
    assert skip_events
    fields, meta = skip_events[0]
    assert fields["reason"] == "resolution_failed"
    # Error detail surfaces so Sebastien can read why the resolver gave up.
    assert "no opening matches" in fields.get("error", "")
    assert meta["severity"] == "warning"
    # Populator did NOT populate.
    assert sd.opening_directive is None


def test_populate_already_populated_is_silent(captured_events) -> None:
    """Idempotency: when ``opening_directive`` is already set (double
    confirmation, replay), the populator returns silently — no skip
    event, no resolved event. Prevents log spam on repeated commits.
    """
    sd = _session_data(opening_directive=object())
    _populate_opening_directive_on_chargen_complete(
        session_data=sd,
        snapshot=_snapshot_with_pc(),
        pack=_pack_with_world(_world_with_openings([])),
        world_slug="test_world",
        mode="multiplayer",
    )
    assert captured_events == []


# ---- deferral gate ---------------------------------------------------


def test_should_fire_opening_solo_no_room() -> None:
    """Solo path with no MP room — opening always fires on first commit."""
    sd = SimpleNamespace(
        opening_directive=None,
        snapshot=SimpleNamespace(characters=[object()]),
    )
    assert _should_fire_opening_narration(sd, room=None) is True


def test_should_fire_opening_when_directive_already_resolved() -> None:
    """If the populator successfully built a directive (party complete
    OR solo opening matched), opening narration should fire — even if
    the room bookkeeping somehow says otherwise. Directive-presence is
    the strong signal.
    """
    room = SimpleNamespace(non_abandoned_player_count=lambda: 2)
    sd = SimpleNamespace(
        opening_directive=object(),
        snapshot=SimpleNamespace(characters=[object()]),
    )
    assert _should_fire_opening_narration(sd, room=room) is True


def test_should_defer_opening_mp_first_committer_no_directive() -> None:
    """The bug shape: MP, 2 seats expected, only 1 PC committed,
    populator failed (no directive) — defer.
    """
    room = SimpleNamespace(non_abandoned_player_count=lambda: 2)
    sd = SimpleNamespace(
        opening_directive=None,
        snapshot=SimpleNamespace(characters=[object()]),  # only 1 PC committed
    )
    assert _should_fire_opening_narration(sd, room=room) is False


def test_should_fire_opening_mp_last_committer() -> None:
    """The reverse: MP 2 seats, 2 PCs committed (last committer's call) —
    fire even when this particular sd has no directive yet (the caller
    populates immediately before the gate check, so the directive
    should be set; this test pins the count-matches branch as a
    belt-and-suspenders fallback).
    """
    room = SimpleNamespace(non_abandoned_player_count=lambda: 2)
    sd = SimpleNamespace(
        opening_directive=None,
        snapshot=SimpleNamespace(characters=[object(), object()]),
    )
    assert _should_fire_opening_narration(sd, room=room) is True


def test_should_fire_opening_room_reports_one_player() -> None:
    """Edge case: room reports ``non_abandoned_player_count=1`` (solo via
    MP-room-of-one — pre-MP saves loaded into a fresh room). Treat as
    solo: fire immediately.
    """
    room = SimpleNamespace(non_abandoned_player_count=lambda: 1)
    sd = SimpleNamespace(
        opening_directive=None,
        snapshot=SimpleNamespace(characters=[object()]),
    )
    assert _should_fire_opening_narration(sd, room=room) is True


# ---- current_region binding from opening.setting.region_id ------------
#
# Playtest 2026-05-25 [BUG] flickering_reach: the active location never
# advanced from the spawn region because region_init seeds current_region
# from cartography.starting_region, but the opening anchors the party at a
# different cartography node and the narrator only emits a free-text label.
# The opening now declares an authored setting.region_id; the binding helper
# rebinds current_region to it (emitting a state_patch.current_region span)
# and fails loud on a dangling region_id.


def _region_pack(regions: list[str]) -> SimpleNamespace:
    """A pack whose test_world cartography declares ``regions`` as nodes."""
    cartography = SimpleNamespace(regions={r: object() for r in regions})
    world = SimpleNamespace(cartography=cartography)
    return SimpleNamespace(worlds={"test_world": world})


def _opening_with_region(region_id: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        id="either_blind_reach_dusk_frogs",
        setting=SimpleNamespace(region_id=region_id),
    )


def _region_snapshot(current_region: str) -> SimpleNamespace:
    # Movement subsystem §Q0: _bind_current_region_from_opening now seeds the
    # per-PC region map after binding the anchor. Fit the lightweight mock to
    # the new shape — empty seats/pc_regions + a no-op seed stub (this test
    # exercises the anchor rebind + watcher event, not per-PC seeding, which is
    # covered in tests/game/test_pc_regions.py).
    snap = SimpleNamespace(
        current_region=current_region,
        discovered_regions=[current_region],
        player_seats={},
        pc_regions={},
    )
    snap.seed_pc_regions = lambda region_id, **_: 0
    return snap


def test_bind_region_rebinds_and_emits_patch_span(captured_events) -> None:
    """Opening declares a region_id that differs from the spawn region →
    current_region is rebound, discovered_regions gains it, and a
    ``state_patch.current_region`` watcher event fires (GM-panel lie
    detector per CLAUDE.md OTEL principle). The helper returns the bound id.
    """
    snap = _region_snapshot("toods_dome")
    bound = _bind_current_region_from_opening(
        snap,
        _region_pack(["toods_dome", "blind_reach"]),
        "test_world",
        _opening_with_region("blind_reach"),
    )
    assert bound == "blind_reach"
    assert snap.current_region == "blind_reach"
    assert "blind_reach" in snap.discovered_regions

    patches = [
        (fields, meta) for et, fields, meta in captured_events if et == "state_patch.current_region"
    ]
    assert patches, f"expected state_patch.current_region span; captured: {captured_events}"
    fields, meta = patches[0]
    assert fields["current_region"] == "blind_reach"
    assert fields["prior_current_region"] == "toods_dome"
    assert fields["source"] == "opening.setting.region_id"
    assert meta["component"] == "opening_hook"


def test_bind_region_noop_when_already_bound(captured_events) -> None:
    """Re-binding to the region current_region already holds is a silent
    no-op — no redundant patch (the task forbids redundant patches)."""
    snap = _region_snapshot("blind_reach")
    bound = _bind_current_region_from_opening(
        snap,
        _region_pack(["toods_dome", "blind_reach"]),
        "test_world",
        _opening_with_region("blind_reach"),
    )
    assert bound is None
    assert snap.current_region == "blind_reach"
    assert captured_events == []


def test_bind_region_noop_when_no_region_id(captured_events) -> None:
    """An opening with no region_id (chassis-anchored / unbound location)
    leaves current_region untouched and emits nothing."""
    snap = _region_snapshot("toods_dome")
    bound = _bind_current_region_from_opening(
        snap,
        _region_pack(["toods_dome", "blind_reach"]),
        "test_world",
        _opening_with_region(None),
    )
    assert bound is None
    assert snap.current_region == "toods_dome"
    assert captured_events == []


def test_bind_region_fails_loud_on_dangling_region_id(captured_events) -> None:
    """A region_id that is NOT a cartography node is a pack-authoring bug:
    raise RegionInitError + emit a ``current_region.bind_failed`` ERROR span.
    No silent fallback, no fuzzy free-text match (CLAUDE.md No-Silent-Fallbacks).
    """
    snap = _region_snapshot("toods_dome")
    with pytest.raises(RegionInitError, match="not a declared cartography region"):
        _bind_current_region_from_opening(
            snap,
            _region_pack(["toods_dome", "blind_reach"]),
            "test_world",
            _opening_with_region("nonexistent_node"),
        )
    fails = [
        (fields, meta) for et, fields, meta in captured_events if et == "current_region.bind_failed"
    ]
    assert fails, f"expected current_region.bind_failed ERROR span; captured: {captured_events}"
    fields, meta = fails[0]
    assert fields["declared_region_id"] == "nonexistent_node"
    assert meta["severity"] == "error"
    # current_region untouched — no half-applied rebind.
    assert snap.current_region == "toods_dome"
