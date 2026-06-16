"""Ping-pong 2026-06-07 (carried from 2026-06-06): "Region-mode location drift
kills combat" — same-region scene-title drift must NOT abandon an active
encounter.

Perseus repro: PC fires on armed NPCs → combat encounter seats — then
``encounter.deactivated_on_location_change`` fires the SAME turn because the
narrator drifted the scene title ('New Kowloon, Yula' → 'New Kowloon — Transit
Promenade'). The abandon-on-location-change ladder keyed off the raw location
STRING and ignored the same-region signal the code had already computed
(``region.entry_skipped_sub_location`` / heading-resolves-to-current-region).
Net: ``beat_selections=0 confrontation=None`` forever; the narrator free-hands
hit/miss in prose.

Fix under test: ``_same_region_drift`` is set by both region-resolution branches
(heading resolves to the CURRENT region; heading is an unresolved POI within a
region-mode world) and consumed at the TOP of the encounter ladder — drift →
encounter CONTINUES (``confrontation_continued_same_region_drift`` watcher
event); a genuine region change leaves the flag False and the existing
won/yield/mobile/abandon ladder runs unchanged (negotiation-walk-out semantics
2026-04-30 preserved).

Fixtures, not live packs (project memory: no content-coupled unit tests).
Harness cribbed from tests/server/test_region_advance_on_location.py; watcher
capture cribbed from tests/server/test_opponent_yield_resolution.py.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
)
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


def _active_combat() -> StructuredEncounter:
    """An anchored, unfinished combat: dials BELOW threshold (no dial win), no
    opponent yield, ``category="combat"`` (explicitly non-mobile so the chase
    exemption never masks the branch under test). The ONLY way this survives a
    location change is the same-region-drift continue branch."""
    return StructuredEncounter(
        encounter_type="combat",
        category="combat",
        player_metric=EncounterMetric(name="momentum", current=2, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="menace", current=1, starting=0, threshold=10),
        actors=[
            EncounterActor(name="Susan", role="combatant", side="player"),
            EncounterActor(name="Enforcer", role="aggressor", side="opponent", withdrawn=False),
        ],
    )


@pytest.fixture
def captured_watcher_events(monkeypatch) -> Iterator[list[dict[str, Any]]]:
    """Capture every ``_watcher_publish`` call on the narration-apply path —
    the canonical confrontation-OTEL capture pattern (mirrors
    tests/server/test_opponent_yield_resolution.py)."""
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


def _continue_events(captured: list[dict]) -> list[dict]:
    return [
        e
        for e in captured
        if e["event_type"] == "confrontation_continued_same_region_drift"
        and e["component"] == "confrontation"
    ]


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


# ── continue: unresolved POI within a region-mode world ─────────────────────


def test_sub_location_drift_keeps_combat_active(
    snapshot_with_pack,
    character_named_sam,
    captured_watcher_events,
):
    """The perseus repro shape: an active combat + a narrator scene title that
    resolves to NO cartography region (a POI within the current region —
    ``region.entry_skipped_sub_location``). The encounter must CONTINUE, with
    the continue decision visible to the GM panel (OTEL lie-detector)."""
    snap, pack = snapshot_with_pack
    _region_mode_pack(pack)
    snap.current_region = "munchkin_country"
    snap.character_locations["Susan"] = "The Munchkin Country"
    snap.characters.append(character_named_sam)
    snap.encounter = _active_combat()

    _apply(snap, pack, location="The Munchkin Country — Transit Promenade")

    assert snap.encounter is not None and not snap.encounter.resolved, (
        "a same-region sub-location drift must NOT resolve an active combat; "
        f"got resolved={snap.encounter.resolved!r} outcome={snap.encounter.outcome!r}"
    )
    events = _continue_events(captured_watcher_events)
    assert len(events) == 1, (
        "the continue decision must emit exactly one "
        "confrontation_continued_same_region_drift watcher event; "
        f"got {captured_watcher_events!r}"
    )
    fields = events[0]["fields"]
    assert fields["encounter_type"] == "combat"
    assert fields["current_region"] == "munchkin_country"
    assert fields["new_location"] == "The Munchkin Country — Transit Promenade"
    assert fields["player_name"] == "Susan"


# ── continue: heading resolves to the CURRENT region ────────────────────────


def test_heading_resolving_to_current_region_keeps_combat_active(
    snapshot_with_pack,
    character_named_sam,
    captured_watcher_events,
):
    """A heading whose leading segment resolves to the region the party is
    ALREADY in (e.g. 'The Emerald City — The Palace Steps' while in
    the_emerald_city) is the other same-region drift shape. Combat continues."""
    snap, pack = snapshot_with_pack
    _region_mode_pack(pack)
    snap.current_region = "the_emerald_city"
    snap.character_locations["Susan"] = "The Emerald City"
    snap.characters.append(character_named_sam)
    snap.encounter = _active_combat()

    _apply(snap, pack, location="The Emerald City — The Palace Steps")

    assert snap.encounter is not None and not snap.encounter.resolved, (
        "a heading resolving to the CURRENT region must NOT resolve an active "
        f"combat; got resolved={snap.encounter.resolved!r} "
        f"outcome={snap.encounter.outcome!r}"
    )
    assert len(_continue_events(captured_watcher_events)) == 1
    assert snap.current_region == "the_emerald_city", "region must not churn"


# ── guard: a GENUINE region change still abandons ───────────────────────────


def test_heading_resolving_to_different_region_still_abandons(
    snapshot_with_pack,
    character_named_sam,
    captured_watcher_events,
):
    """Negotiation-walk-out semantics (2026-04-30) preserved: a heading that
    resolves to a DIFFERENT cartography region is a real scene boundary —
    an unfinished anchored encounter still abandons, and the continue event
    must NOT fire."""
    snap, pack = snapshot_with_pack
    _region_mode_pack(pack)
    snap.current_region = "munchkin_country"
    snap.character_locations["Susan"] = "The Munchkin Country"
    snap.characters.append(character_named_sam)
    snap.encounter = _active_combat()

    _apply(snap, pack, location="The Emerald City — The Green Street")

    assert snap.current_region == "the_emerald_city", "region must genuinely advance"
    assert snap.encounter is not None and snap.encounter.resolved, (
        "a genuine region change must still resolve an unfinished anchored "
        "encounter (walk-away semantics)"
    )
    assert snap.encounter.outcome == "abandoned_on_location_change"
    assert _continue_events(captured_watcher_events) == [], (
        "the same-region-drift continue event must NOT fire on a genuine "
        f"region change; got {captured_watcher_events!r}"
    )


# ── guard: room-graph worlds keep abandon-on-leave ──────────────────────────


def test_room_graph_world_unresolved_heading_still_abandons(
    snapshot_with_pack,
    character_named_sam,
    captured_watcher_events,
):
    """The drift flag is gated to region-mode worlds: in a room-graph world an
    unresolved heading is a real scene change (those worlds legitimately grow
    their graph from narrator inventions, Story 45-17) — the abandon ladder
    must run unchanged."""
    snap, pack = snapshot_with_pack
    _region_mode_pack(pack, mode=NavigationMode.room_graph)
    snap.current_region = "munchkin_country"
    snap.character_locations["Susan"] = "The Munchkin Country"
    snap.characters.append(character_named_sam)
    snap.encounter = _active_combat()

    _apply(snap, pack, location="The Sealed Maintenance Hatch")

    assert snap.encounter is not None and snap.encounter.resolved, (
        "a room-graph world must keep abandon-on-leave for an unresolved heading"
    )
    assert snap.encounter.outcome == "abandoned_on_location_change"
    assert _continue_events(captured_watcher_events) == []
