"""Story 97-4: Scratch sweep fires on same-region drift.

Same root-cause family as #739 (region-drift encounter abandonment),
deliberately left out of that story's scope (2026-06-07 ping-pong FIXER
notes). The scene-bounded status sweep — ``clear_scratch_on_scene_end`` —
runs at the ``location_update`` gate in ``narration_apply`` keyed on the raw
``old_loc != result.location`` string. So a same-region scene-title drift in a
region-mode world (e.g. 'New Kowloon, Yula' -> 'New Kowloon — Transit
Promenade') wipes every Scratch/Boon on every PC even though the party never
left the scene. #739 fixed the *encounter* ladder for exactly this drift; the
scratch sweep is the sibling gate that still keys on the raw string.

Fix under test: the scratch-sweep gate consults the ``_same_region_drift``
signal that ``narration_apply`` already computes for #739 (heading resolves to
the CURRENT region; heading is an unresolved POI within a region-mode world;
rejected heading in a region-mode world). On a same-region drift the sweep is
SKIPPED and a ``scratch_sweep_skipped_same_region_drift`` watcher event fires
(OTEL lie-detector — the GM panel sees the engine CHOSE to keep scene-bounded
status, per CLAUDE.md OTEL principle). A genuine region change leaves the flag
False and the sweep runs unchanged — scene-boundary semantics preserved.

Contract for Dev (RED phase, TEA):
  * Wrap ONLY the ``clear_scratch_on_scene_end`` call in
    ``if not _same_region_drift:`` — the encounter abandon ladder below it
    already handles drift internally (#739) and must keep running.
  * On the skip, emit ``_watcher_publish("scratch_sweep_skipped_same_region_drift",
    {...}, component="encounter")`` from ``narration_apply`` (where
    ``_same_region_drift`` is in scope), with at least these fields:
    ``current_region``, ``old_location``, ``new_location``, ``player_name``,
    ``turn_number``. Emitting from ``narration_apply`` (not ``status_clear``)
    is what makes the keep decision visible to the harness below, which
    monkeypatches ``narration_apply._watcher_publish``.

Fixtures, not live packs (project memory: no content-coupled unit tests).
Harness mirrors tests/server/test_region_drift_encounter_continue.py (#739).
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.game.status import Status, StatusSeverity
from sidequest.genre.models.world import CartographyConfig, NavigationMode, Region
from sidequest.server import narration_apply
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from tests._helpers.session_room import room_for

# ── fixtures / builders ─────────────────────────────────────────────────────


def _oz_regions() -> dict[str, Region]:
    return {
        "munchkin_country": Region(
            name="The Munchkin Country",
            summary="The blue East.",
            description="The blue East.",
            adjacent=["the_emerald_city"],
        ),
        "the_emerald_city": Region(
            name="The Emerald City",
            summary="The green hub.",
            description="The green hub.",
            adjacent=["munchkin_country"],
        ),
    }


def _region_mode_pack(pack, *, mode: NavigationMode = NavigationMode.region):
    """Attach a synthetic cartography world keyed "oz" onto the (mock) pack."""
    world_obj = SimpleNamespace(
        cartography=CartographyConfig(navigation_mode=mode, regions=_oz_regions())
    )
    pack.worlds = {"oz": world_obj}
    return pack


def _add_status(char, text: str, severity: StatusSeverity) -> None:
    char.core.statuses.append(
        Status(
            text=text,
            severity=severity,
            absorbed_shifts=0,
            created_turn=0,
            created_in_encounter=None,
        ),
    )


def _status_texts(char) -> list[str]:
    return [s.text for s in char.core.statuses]


@pytest.fixture
def captured_watcher_events(monkeypatch) -> Iterator[list[dict[str, Any]]]:
    """Capture every ``_watcher_publish`` call on the narration-apply path —
    the canonical OTEL capture pattern (mirrors #739's
    test_region_drift_encounter_continue.py). The keep decision the fix emits
    must come through this seam to be visible to the GM panel."""
    captured: list[dict[str, Any]] = []

    def _capture(event_type, fields, *, component="sidequest-server", severity="info"):
        captured.append(
            {
                "event_type": event_type,
                "fields": fields,
                "component": component,
                "severity": severity,
            }
        )

    monkeypatch.setattr(narration_apply, "_watcher_publish", _capture)
    yield captured


def _keep_events(captured: list[dict]) -> list[dict]:
    return [e for e in captured if e["event_type"] == "scratch_sweep_skipped_same_region_drift"]


def _apply(snap, pack, *, location: str) -> None:
    result = NarrationTurnResult(
        narration="Blaster fire scatters the crowd across the promenade.",
        location=location,
    )
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=result,
        pack=pack,
        world="oz",
        player_name="Susan",
        room=room_for(snapshot=snap),
    )


def _stage(snap, pack, character, *, current_region: str, old_location: str):
    """Seat a PC carrying scene-bounded + persistent statuses in a region-mode
    world, ready for a location_update that drifts the scene title."""
    _region_mode_pack(pack)
    snap.current_region = current_region
    snap.character_locations["Susan"] = old_location
    snap.characters.append(character)
    _add_status(character, "Choked", StatusSeverity.Scratch)
    _add_status(character, "Heightened Perception (3 rounds)", StatusSeverity.Boon)
    _add_status(character, "Bruised Ribs", StatusSeverity.Wound)


# ── keep: unresolved POI within a region-mode world ─────────────────────────


def test_sub_location_drift_keeps_scene_bounded_status(
    snapshot_with_pack,
    character_named_sam,
    captured_watcher_events,
):
    """The perseus repro shape applied to status: an active scene + a narrator
    scene title that resolves to NO cartography region (a POI within the
    current region — ``region.entry_skipped_sub_location``). The party never
    left the scene, so Scratch AND Boon must SURVIVE, and the keep decision
    must be visible to the GM panel (OTEL lie-detector)."""
    snap, pack = snapshot_with_pack
    _stage(
        snap,
        pack,
        character_named_sam,
        current_region="munchkin_country",
        old_location="The Munchkin Country",
    )

    _apply(snap, pack, location="The Munchkin Country — Transit Promenade")

    remaining = _status_texts(character_named_sam)
    assert "Choked" in remaining, (
        "a same-region sub-location drift must NOT sweep Scratch — the party "
        f"never left the scene; got statuses={remaining!r}"
    )
    assert "Heightened Perception (3 rounds)" in remaining, (
        "Boon is scene-bounded too and must survive a same-region drift"
    )
    assert "Bruised Ribs" in remaining, "Wound persists regardless (control)"

    events = _keep_events(captured_watcher_events)
    assert len(events) == 1, (
        "the keep decision must emit exactly one "
        "scratch_sweep_skipped_same_region_drift watcher event; "
        f"got {captured_watcher_events!r}"
    )
    fields = events[0]["fields"]
    assert events[0]["component"] == "encounter"
    assert fields["current_region"] == "munchkin_country"
    assert fields["old_location"] == "The Munchkin Country"
    assert fields["new_location"] == "The Munchkin Country — Transit Promenade"
    assert fields["player_name"] == "Susan"


# ── keep: heading resolves to the CURRENT region ────────────────────────────


def test_heading_resolving_to_current_region_keeps_scratch(
    snapshot_with_pack,
    character_named_sam,
    captured_watcher_events,
):
    """A heading whose leading segment resolves to the region the party is
    ALREADY in (e.g. 'The Emerald City — The Palace Steps' while in
    the_emerald_city) is the other same-region drift shape. Scratch survives;
    the region must not churn."""
    snap, pack = snapshot_with_pack
    _stage(
        snap,
        pack,
        character_named_sam,
        current_region="the_emerald_city",
        old_location="The Emerald City",
    )

    _apply(snap, pack, location="The Emerald City — The Palace Steps")

    assert "Choked" in _status_texts(character_named_sam), (
        "a heading resolving to the CURRENT region must NOT sweep Scratch"
    )
    assert len(_keep_events(captured_watcher_events)) == 1
    assert snap.current_region == "the_emerald_city", "region must not churn"


# ── guard: a GENUINE region change still sweeps ─────────────────────────────


def test_genuine_region_change_still_sweeps_scratch(
    snapshot_with_pack,
    character_named_sam,
    captured_watcher_events,
):
    """Scene-boundary semantics preserved: a heading that resolves to a
    DIFFERENT cartography region is a real scene change — the scene-bounded
    sweep still runs (Scratch + Boon cleared, Wound persists) and the keep
    event must NOT fire."""
    snap, pack = snapshot_with_pack
    _stage(
        snap,
        pack,
        character_named_sam,
        current_region="munchkin_country",
        old_location="The Munchkin Country",
    )

    _apply(snap, pack, location="The Emerald City — The Green Street")

    assert snap.current_region == "the_emerald_city", "region must genuinely advance"
    remaining = _status_texts(character_named_sam)
    assert "Choked" not in remaining, (
        "a genuine region change is a scene boundary — Scratch must still sweep"
    )
    assert "Heightened Perception (3 rounds)" not in remaining, (
        "Boon must still sweep on a genuine region change"
    )
    assert "Bruised Ribs" in remaining, "Wound persists across the scene boundary"
    assert _keep_events(captured_watcher_events) == [], (
        f"the keep event must NOT fire on a genuine region change; got {captured_watcher_events!r}"
    )


# ── guard: room-graph worlds keep sweep-on-leave ────────────────────────────


def test_room_graph_world_unresolved_heading_still_sweeps_scratch(
    snapshot_with_pack,
    character_named_sam,
    captured_watcher_events,
):
    """The drift flag is gated to region-mode worlds: in a room-graph world an
    unresolved heading is a real scene change (those worlds legitimately grow
    their graph from narrator inventions, Story 45-17) — the sweep must run
    unchanged and the keep event must NOT fire."""
    snap, pack = snapshot_with_pack
    _region_mode_pack(pack, mode=NavigationMode.room_graph)
    snap.current_region = "munchkin_country"
    snap.character_locations["Susan"] = "The Munchkin Country"
    snap.characters.append(character_named_sam)
    _add_status(character_named_sam, "Choked", StatusSeverity.Scratch)

    _apply(snap, pack, location="The Sealed Maintenance Hatch")

    assert "Choked" not in _status_texts(character_named_sam), (
        "a room-graph world must keep sweep-on-leave for an unresolved heading"
    )
    assert _keep_events(captured_watcher_events) == []


# ── guard: session-start location set neither sweeps nor emits keep ─────────


def test_first_location_set_does_not_emit_keep_event(
    snapshot_with_pack,
    character_named_sam,
    captured_watcher_events,
):
    """``old_loc`` is None at session start — there is no prior scene to leave,
    so the sweep gate is not entered at all and the keep event must not fire.
    Guards the ``old_loc and ...`` half of the gate so the drift branch can't
    spuriously emit on the first location bind."""
    snap, pack = snapshot_with_pack
    _region_mode_pack(pack)
    snap.current_region = "munchkin_country"
    # No prior character_locations["Susan"] — old_loc resolves to None.
    snap.characters.append(character_named_sam)
    _add_status(character_named_sam, "Choked", StatusSeverity.Scratch)

    _apply(snap, pack, location="The Munchkin Country")

    assert "Choked" in _status_texts(character_named_sam), (
        "no scene to leave at session start — Scratch must be untouched"
    )
    assert _keep_events(captured_watcher_events) == []
