"""End-to-end wiring for the story 45-52 NPC subsystem cleanup.

Three Reviewer asks from 45-47 surface as live OTEL signals here — each
test exercises the production wire and asserts the typed
``state_transition`` (or relevant attribute) reaches the GM panel
through the routing pipeline. Per CLAUDE.md "Verify Wiring, Not Just
Existence": this file is the dedicated wiring guard the story's "add
dedicated wiring test" AC calls for.

Covered seams:

1. ``SPAN_NPC_EDGE_PUBLISHED`` — fires at encounter handshake when the
   dial-derived edge pool is written onto ``Npc.core.edge``. Replaces
   the pre-Wave-2A ``npc_registry.hp_set`` seam.

2. ``location_available`` attribute on
   ``encounter.no_opponent_available`` — discriminates "the player had
   no resolved location" from "no NPCs at this location" (both shapes
   silently produced an empty fallback pre-fix; only the latter is a
   legitimate empty-scene).

3. ``s2_malformed_npcs_skipped`` / ``s2_nameless_entries_dropped``
   attributes on the ``snapshot.canonicalize`` span — silent-skip
   counters on the legacy-save migration so corrupt registry entries
   are visible on the GM panel rather than eaten on the floor.

These three OTEL channels are the lie-detector contract for the Wave-2A
cleanup: if any silently fails, Sebastien's GM panel goes dark on the
exact seams the cleanup touched.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.game.creature_core import (
    CreatureCore,
    HpPool,
    Inventory,
)
from sidequest.game.migrations import migrate_legacy_snapshot
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.turn import TurnManager
from sidequest.genre.loader import load_genre_pack
from sidequest.server.watcher import WatcherSpanProcessor
from sidequest.telemetry import spans as spans_module
from sidequest.telemetry.watcher_hub import watcher_hub
from tests._helpers.trigger_encounter import trigger_encounter

_FIXTURE_PACK = Path(__file__).resolve().parents[1] / "fixtures" / "packs" / "test_genre"


@pytest.fixture
def otel_capture():
    """SDK provider + in-memory exporter, mirrors the pattern in
    test_npc_registry_combat_stats.py.
    """
    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider), (
        f"expected SDK TracerProvider, got {type(provider)!r}"
    )
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


async def _hub_setup(monkeypatch: pytest.MonkeyPatch, label: str) -> list[dict]:
    """Install a local watcher hub + monkeypatch tracer — same shape as
    tests/integration/test_npc_wiring.py."""
    watcher_hub.bind_loop(asyncio.get_running_loop())
    async with watcher_hub._lock:  # noqa: SLF001
        watcher_hub._subscribers.clear()  # noqa: SLF001

    captured: list[dict] = []

    class _Sock:
        async def send_json(self, data: dict) -> None:
            captured.append(data)

    await watcher_hub.subscribe(_Sock())  # type: ignore[arg-type]

    provider = TracerProvider()
    provider.add_span_processor(WatcherSpanProcessor(watcher_hub))
    local_tracer = provider.get_tracer(label)
    monkeypatch.setattr(spans_module, "tracer", lambda: local_tracer)

    return captured


def _make_npc(name: str, *, location: str | None = None, turn: int = 0) -> Npc:
    return Npc(
        core=CreatureCore(
            name=name,
            description="An NPC.",
            personality="Neutral.",
            level=1,
            xp=0,
            inventory=Inventory(),
            statuses=[],
            hp=HpPool(current=10, max=10, base_max=10),
        ),
        npc_role_id="hostile",
        last_seen_location=location,
        last_seen_turn=turn,
    )


# ---------------------------------------------------------------------------
# 1) npc.edge_published — production handshake reaches the routed event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_npc_edge_published_reaches_hub_via_span_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The encounter handshake's call to ``_publish_combat_edge_to_npcs``
    must reach the watcher hub as a routed ``state_transition``
    (component=npcs, op=edge_published). Proves production opens the
    helper rather than publishing directly, AND that the renamed span
    survives the SPAN_ROUTES extract.
    """
    captured = await _hub_setup(monkeypatch, "test-npc-edge-published-wiring")

    pack = load_genre_pack(_FIXTURE_PACK)
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=4),
    )
    snap.character_locations["Orin"] = "Mawdeep Caverns"
    snap.npcs.append(_make_npc("Crawling Scavenger", location="Mawdeep Caverns", turn=3))

    trigger_encounter(snap, pack, "combat", "Orin", npcs_present=[])
    await asyncio.sleep(0.05)

    edge_events = [
        e
        for e in captured
        if e["event_type"] == "state_transition" and e["fields"].get("op") == "edge_published"
    ]
    assert len(edge_events) >= 1, (
        "no edge_published state_transition reached the hub — production "
        "handshake regressed off the renamed seam. "
        f"captured={[e.get('event_type') for e in captured]!r}"
    )
    fields = edge_events[0]["fields"]
    assert fields["name"] == "Crawling Scavenger"
    assert int(fields["current"]) > 0
    assert int(fields["max"]) > 0
    # The post-Wave-2A canonical field is npcs (not npc_registry).
    assert fields["field"] == "npcs"


# ---------------------------------------------------------------------------
# 2) location_available — silent-failure detector on the no-opponent path
# ---------------------------------------------------------------------------


def test_no_opponent_span_carries_location_available_false(otel_capture):
    """When the location-scoped fallback returns empty because the player
    had NO resolved location (party_location returns None), the
    encounter.no_opponent_available span must fire with
    ``location_available=False``. Pre-story-45-52 this silent-skip path
    was indistinguishable on the GM panel from "no NPCs at the location"
    (location_available=True). The attribute is the Reviewer-asked
    discriminator.
    """
    from sidequest.server.dispatch.encounter_lifecycle import (
        NoOpponentAvailableError,
        instantiate_encounter_from_trigger,
    )

    pack = load_genre_pack(_FIXTURE_PACK)
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=1),
    )
    # No character_locations entry for Orin → party_location returns None.
    # An NPC exists at "Somewhere Else" — irrelevant; the fallback can't
    # even ask "which NPCs are here" without a location.
    snap.npcs.append(_make_npc("Unrelated", location="Somewhere Else", turn=0))

    with pytest.raises(NoOpponentAvailableError):
        instantiate_encounter_from_trigger(
            snapshot=snap,
            pack=pack,
            encounter_type="combat",
            player_name="Orin",
            npcs_present=[],
            genre_slug="caverns_and_claudes",
        )

    no_opp_spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "encounter.no_opponent_available"
    ]
    assert len(no_opp_spans) == 1, (
        "guard didn't fire encounter.no_opponent_available; "
        f"finished spans = {[s.name for s in otel_capture.get_finished_spans()]!r}"
    )
    attrs = dict(no_opp_spans[0].attributes or {})
    assert "location_available" in attrs, (
        "encounter.no_opponent_available span missing the location_available "
        f"silent-failure detector attribute; attrs={sorted(attrs)!r}"
    )
    assert attrs["location_available"] is False, (
        "player had no resolved location but the span reports "
        f"location_available={attrs['location_available']!r} — the "
        "silent-failure discriminator regressed."
    )


def test_no_opponent_span_carries_location_available_true(otel_capture):
    """Counterpart to the False case: when the player HAS a resolved
    location but no NPCs share it, the span must report
    ``location_available=True``. This branch is the legitimate empty-scene
    shape — distinct from the "no location at all" bug above.
    """
    from sidequest.server.dispatch.encounter_lifecycle import (
        NoOpponentAvailableError,
        instantiate_encounter_from_trigger,
    )

    pack = load_genre_pack(_FIXTURE_PACK)
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=1),
    )
    snap.character_locations["Orin"] = "Mawdeep Caverns"
    # NPC exists at a different location — fallback returns [] but
    # location_available stays True because the player did have a location.
    snap.npcs.append(_make_npc("Distant Hostile", location="Sunken Hall", turn=0))

    with pytest.raises(NoOpponentAvailableError):
        instantiate_encounter_from_trigger(
            snapshot=snap,
            pack=pack,
            encounter_type="combat",
            player_name="Orin",
            npcs_present=[],
            genre_slug="caverns_and_claudes",
        )

    no_opp_spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "encounter.no_opponent_available"
    ]
    assert len(no_opp_spans) == 1
    attrs = dict(no_opp_spans[0].attributes or {})
    assert attrs.get("location_available") is True, (
        "player had a resolved location but the span reports "
        f"location_available={attrs.get('location_available')!r}"
    )


# ---------------------------------------------------------------------------
# 3) Migration silent-skip counters — surveyed by test_npc_pool_migration
# ---------------------------------------------------------------------------


def test_migration_silent_skip_counters_reach_canonicalize_span(otel_capture):
    """Story 45-52 silent-failure findings: malformed entries (non-dict)
    and nameless entries were silently dropped by the s2 migration. Both
    paths now produce per-counter attributes on the
    ``snapshot.canonicalize`` span. Without these attributes, the legacy
    save fixture would migrate cleanly even when half its registry was
    corrupt — Reviewer's exact concern.
    """
    legacy = {
        "npcs": [],
        "npc_registry": [
            "not-a-dict",  # malformed
            42,  # malformed
            {"role": "merchant"},  # nameless (no name key)
            {"name": "", "role": "blank"},  # nameless (blank name)
            {"name": "Valid"},  # passes
        ],
    }
    out = migrate_legacy_snapshot(legacy)
    assert len(out["npc_pool"]) == 1
    assert out["npc_pool"][0]["name"] == "Valid"

    spans = [s for s in otel_capture.get_finished_spans() if s.name == "snapshot.canonicalize"]
    assert len(spans) == 1
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("s2_malformed_npcs_skipped") == 2
    assert attrs.get("s2_nameless_entries_dropped") == 2
