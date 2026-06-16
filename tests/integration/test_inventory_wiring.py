"""End-to-end wiring for the inventory Phase 2 bundle.

Drives ``_apply_narration_result_to_snapshot`` with an
``items_gained`` / ``items_lost`` payload through a real
``TracerProvider`` + ``WatcherSpanProcessor`` and asserts the typed
``state_transition`` event with ``component=inventory`` reaches the hub
via ``SPAN_ROUTES[SPAN_INVENTORY_NARRATOR_EXTRACTED]`` — i.e. the
production code path actually opens the span (not the prior direct
``publish_event`` call this PR replaced).

Per ``CLAUDE.md`` "Verify Wiring, Not Just Existence": the unit test in
``tests/server/test_watcher_events.py`` proves the route extracts the
right fields from a fake span; this proves a real narration apply opens
that span.

Uses the same ``spans_module.tracer`` monkeypatch shape as
``test_state_patch_wiring.py`` and ``test_npc_wiring.py`` — OTEL refuses
to replace an already-installed global provider mid-suite, so patching
the function the helper actually calls is the order-independent seam.
"""

from __future__ import annotations

import asyncio

import pytest
from opentelemetry.sdk.trace import TracerProvider

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.session import GameSnapshot, TurnManager
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from sidequest.server.watcher import WatcherSpanProcessor
from sidequest.telemetry import spans as spans_module
from sidequest.telemetry.watcher_hub import watcher_hub
from tests._helpers.session_room import room_for


def _make_character(name: str, *, items: list[dict] | None = None) -> Character:
    inv = Inventory()
    if items:
        inv.items = list(items)
    return Character(
        core=CreatureCore(
            name=name,
            description=f"{name}, test hero",
            personality="stoic",
            inventory=inv,
            statuses=[],
        ),
        char_class="Fighter",
        race="Human",
        backstory=f"{name} wanders the integration suite.",
    )


async def _setup(monkeypatch: pytest.MonkeyPatch, label: str) -> list[dict]:
    """Bind the module hub to this loop, install a local TracerProvider
    with the ``WatcherSpanProcessor``, and monkeypatch
    ``spans_module.tracer`` so the production helper resolves to it."""
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


@pytest.mark.asyncio
async def test_items_gained_emits_state_transition_via_span_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A NarrationTurnResult with ``items_gained`` must reach the hub
    as a routed ``state_transition`` (component=inventory,
    op=narrator_extracted), proving ``narration_apply.py`` opens
    ``inventory_narrator_extracted_span`` rather than publishing
    directly."""
    captured = await _setup(monkeypatch, "test-inventory-gained-wiring")

    snapshot = GameSnapshot(
        genre_slug="mutant_wasteland",
        world_slug="flickering_reach",
        location="Tood's Dome",
        discovered_regions=["Tood's Dome"],
        quest_log={},
        lore_established=[],
        characters=[_make_character("Rux")],
        turn_manager=TurnManager(),
    )
    snapshot.turn_manager.record_interaction()

    result = NarrationTurnResult(
        narration="Vex hands you a battered tool.",
        items_gained=[
            {"name": "Rusty Spanner", "description": "Quietly competent.", "category": "tool"},
        ],
    )
    _apply_narration_result_to_snapshot(
        snapshot, result, player_name="Rux", room=room_for(snapshot)
    )
    await asyncio.sleep(0.05)

    # Snapshot must have been mutated — the new item is in the inventory.
    item_names = [str(it.get("name", "")) for it in snapshot.characters[0].core.inventory.items]
    assert "Rusty Spanner" in item_names

    typed = [
        e
        for e in captured
        if e["event_type"] == "state_transition"
        and e["component"] == "inventory"
        and e["fields"].get("op") == "narrator_extracted"
    ]
    assert len(typed) == 1, (
        "expected exactly one narrator_extracted state_transition "
        f"(got {len(typed)}: {[e['fields'] for e in typed]})"
    )
    fields = typed[0]["fields"]
    assert fields["field"] == "inventory"
    assert fields["gained_count"] == 1
    assert fields["lost_count"] == 0
    # JSON-encoded — OTEL drops list/dict attributes silently otherwise.
    assert fields["gained"] == '["Rusty Spanner"]'
    assert fields["lost"] == "[]"
    assert fields["player_name"] == "Rux"
    assert fields["turn_number"] == snapshot.turn_manager.interaction
    assert typed[0]["severity"] == "info"


@pytest.mark.asyncio
async def test_items_lost_emits_state_transition_with_matched_names_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``items_lost`` does case-insensitive matching against existing
    inventory; only names that actually matched and were removed must
    appear in the routed event's ``lost`` field. Proves the span
    attributes reflect the post-mutation outcome (not the input)."""
    captured = await _setup(monkeypatch, "test-inventory-lost-wiring")

    pre_existing = [
        {
            "id": "narrator:torch",
            "name": "Torch",
            "description": "Lit.",
            "category": "tool",
            "value": 0,
            "weight": 0.0,
            "rarity": "common",
            "narrative_weight": 0.5,
            "tags": [],
            "equipped": False,
            "quantity": 1,
            "uses_remaining": None,
            "state": "Carried",
        },
    ]
    snapshot = GameSnapshot(
        genre_slug="mutant_wasteland",
        world_slug="flickering_reach",
        location="Tood's Dome",
        discovered_regions=["Tood's Dome"],
        quest_log={},
        lore_established=[],
        characters=[_make_character("Rux", items=pre_existing)],
        turn_manager=TurnManager(),
    )
    snapshot.turn_manager.record_interaction()

    result = NarrationTurnResult(
        narration="The torch sputters out; your phantom dagger never existed.",
        items_lost=[
            {"name": "TORCH"},  # matches case-insensitively
            {"name": "Phantom Dagger"},  # no match — must NOT appear in payload
        ],
    )
    _apply_narration_result_to_snapshot(
        snapshot, result, player_name="Rux", room=room_for(snapshot)
    )
    await asyncio.sleep(0.05)

    # Mutation: torch removed, phantom dagger ignored.
    item_names = [
        str(it.get("name", "")).lower() for it in snapshot.characters[0].core.inventory.items
    ]
    assert "torch" not in item_names

    typed = [
        e
        for e in captured
        if e["event_type"] == "state_transition"
        and e["component"] == "inventory"
        and e["fields"].get("op") == "narrator_extracted"
    ]
    assert len(typed) == 1, (
        "expected exactly one narrator_extracted state_transition "
        f"(got {len(typed)}: {[e['fields'] for e in typed]})"
    )
    fields = typed[0]["fields"]
    # Only the matched (lower-cased) name appears.
    assert fields["lost"] == '["torch"]'
    assert fields["lost_count"] == 1
    assert fields["gained"] == "[]"
    assert fields["gained_count"] == 0


@pytest.mark.asyncio
async def test_items_discarded_emits_state_transition_via_span_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Story 45-14 wiring: a NarrationTurnResult with ``items_discarded``
    must (a) flip the matched item's state out of "Carried" in inventory
    and (b) reach the hub as a routed ``state_transition`` event with the
    discarded name surfaced in the route's ``discarded`` field. Closes
    the Playtest 3 Blutka gap where the narrator's "abandons the spear"
    prose left the spear at state=Carried because the discard verb had
    no apply seam.
    """
    captured = await _setup(monkeypatch, "test-inventory-discarded-wiring")

    pre_existing = [
        {
            "id": "narrator:bone_spear",
            "name": "Bone Spear",
            "description": "A scavenger's barbed shaft.",
            "category": "weapon",
            "value": 0,
            "weight": 2.0,
            "rarity": "common",
            "narrative_weight": 0.5,
            "tags": [],
            "equipped": True,
            "quantity": 1,
            "uses_remaining": None,
            "state": "Carried",
        },
    ]
    snapshot = GameSnapshot(
        genre_slug="mutant_wasteland",
        world_slug="flickering_reach",
        location="Scavenger Pit",
        discovered_regions=["Scavenger Pit"],
        quest_log={},
        lore_established=[],
        characters=[_make_character("Blutka", items=pre_existing)],
        turn_manager=TurnManager(),
    )
    snapshot.turn_manager.record_interaction()

    result = NarrationTurnResult(
        narration="Blutka abandons the spear where it stands.",
        items_discarded=[{"name": "Bone Spear"}],
    )
    _apply_narration_result_to_snapshot(
        snapshot, result, player_name="Blutka", room=room_for(snapshot)
    )
    await asyncio.sleep(0.05)

    # Mutation: spear remains in inventory but state has transitioned out
    # of "Carried" — the production state-transition AC1 demands.
    items = snapshot.characters[0].core.inventory.items
    assert len(items) == 1
    assert items[0]["name"] == "Bone Spear"
    assert items[0]["state"] == "Discarded"
    assert items[0]["equipped"] is False

    typed = [
        e
        for e in captured
        if e["event_type"] == "state_transition"
        and e["component"] == "inventory"
        and e["fields"].get("op") == "narrator_extracted"
    ]
    assert len(typed) == 1, (
        "expected exactly one narrator_extracted state_transition "
        f"(got {len(typed)}: {[e['fields'] for e in typed]})"
    )
    fields = typed[0]["fields"]
    assert fields["discarded"] == '["bone spear"]'
    assert fields["discarded_count"] == 1
    assert fields["lost_count"] == 0
    assert fields["gained_count"] == 0


@pytest.mark.asyncio
async def test_items_consumed_emits_state_transition_via_span_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Story 45-15 wiring: a NarrationTurnResult with ``items_consumed``
    must (a) remove the matched item from inventory and (b) reach the
    hub as a routed ``state_transition`` event with the consumed name
    surfaced in the route's ``consumed`` field.

    Closes Playtest 3 Felix gap: ``maintenance_kit`` lingered at
    ``state=Consumed, quantity=1`` after patch-foam use because the
    consume verb had no apply seam. Fix: consume lane removes outright;
    no item ever sits in state=Consumed.
    """
    captured = await _setup(monkeypatch, "test-inventory-consumed-wiring")

    pre_existing = [
        {
            "id": "narrator:maintenance_kit",
            "name": "Maintenance Kit",
            "description": "Patch-foam, foil strips, dust.",
            "category": "consumable",
            "value": 0,
            "weight": 0.5,
            "rarity": "common",
            "narrative_weight": 0.4,
            "tags": [],
            "equipped": False,
            "quantity": 1,
            "uses_remaining": None,
            "state": "Carried",
        },
    ]
    snapshot = GameSnapshot(
        genre_slug="mutant_wasteland",
        world_slug="flickering_reach",
        location="Felix's Workshop",
        discovered_regions=["Felix's Workshop"],
        quest_log={},
        lore_established=[],
        characters=[_make_character("Felix", items=pre_existing)],
        turn_manager=TurnManager(),
    )
    snapshot.turn_manager.record_interaction()

    result = NarrationTurnResult(
        narration="Felix sprays the last of the patch-foam over the gash.",
        items_consumed=[{"name": "Maintenance Kit"}],
    )
    _apply_narration_result_to_snapshot(
        snapshot, result, player_name="Felix", room=room_for(snapshot)
    )
    await asyncio.sleep(0.05)

    # AC1: kit is gone from inventory — no item left at state=Consumed
    # because the consume lane removes outright.
    items = snapshot.characters[0].core.inventory.items
    assert items == []

    typed = [
        e
        for e in captured
        if e["event_type"] == "state_transition"
        and e["component"] == "inventory"
        and e["fields"].get("op") == "narrator_extracted"
    ]
    assert len(typed) == 1, (
        "expected exactly one narrator_extracted state_transition "
        f"(got {len(typed)}: {[e['fields'] for e in typed]})"
    )
    fields = typed[0]["fields"]
    assert fields["consumed"] == '["maintenance kit"]'
    assert fields["consumed_count"] == 1
    assert fields["lost_count"] == 0
    assert fields["gained_count"] == 0


@pytest.mark.asyncio
async def test_inventory_route_is_single_source_no_double_emission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §6.6 dedupe rule: the aggregate ``narrator_extracted``
    inventory route must fire exactly once — the prior direct
    ``_watcher_publish`` it replaced must NOT also fire, or the
    dashboard double-counts. The route is the single source for the
    turn-level rollup.

    ADR-108 adds a *separate, deliberate* per-applied-item
    ``item_recipient_resolved`` ``inventory`` event (recipient
    attribution, one per entry) — that is new signal, not a double
    emission of the rollup, so the dedupe assertion scopes to
    ``op == "narrator_extracted"``."""
    captured = await _setup(monkeypatch, "test-inventory-single-source")

    snapshot = GameSnapshot(
        genre_slug="mutant_wasteland",
        world_slug="flickering_reach",
        location="Tood's Dome",
        discovered_regions=["Tood's Dome"],
        quest_log={},
        lore_established=[],
        characters=[_make_character("Rux")],
        turn_manager=TurnManager(),
    )
    snapshot.turn_manager.record_interaction()

    result = NarrationTurnResult(
        narration="A small treasure.",
        items_gained=[{"name": "Lucky Coin", "category": "treasure"}],
    )
    _apply_narration_result_to_snapshot(
        snapshot, result, player_name="Rux", room=room_for(snapshot)
    )
    await asyncio.sleep(0.05)

    rollup_events = [
        e
        for e in captured
        if e["event_type"] == "state_transition"
        and e["component"] == "inventory"
        and e["fields"].get("op") == "narrator_extracted"
    ]
    assert len(rollup_events) == 1, (
        "expected exactly one narrator_extracted (rollup) state_transition "
        f"for inventory (got {len(rollup_events)}: {rollup_events})"
    )
    # ADR-108: exactly one per-applied-item recipient event for the lone
    # gained entry — proves the new signal is present and not duplicated.
    resolved_events = [
        e
        for e in captured
        if e["component"] == "inventory" and e["fields"].get("op") == "item_recipient_resolved"
    ]
    assert len(resolved_events) == 1, (
        "expected exactly one item_recipient_resolved event for the single "
        f"gained item (got {len(resolved_events)}: {resolved_events})"
    )
