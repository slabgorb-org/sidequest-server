"""Dedup inventory grants on the ``state.inventory_update`` apply path (126-33).

Playtest 2026-06-04 / 2026-06-20 (Oz, turn 7): the narrator re-emitted a
``gained`` of 'silver shoes' the character already held, and the apply path
appended a second entry -> ``inventory == ['silver shoes', 'silver shoes']``.
Every OTHER inventory lane already matches against the existing ledger before
mutating (``items_lost`` / ``items_discarded`` / ``items_consumed`` all scan
``core.inventory.items`` by case-folded name first), and the
container-retrieval gate (45-13) already blocks a re-emitted *container*
retrieval. The ``gained`` lane is the one outlier: it calls
``recipient_char.core.inventory.items.append(item_dict)`` with no identity
check. This is the bug.

These tests pin the contract for the fix:

1. **No duplicate stack (the bug).** A ``gained`` of an item whose name OR id
   already matches an inventory entry MUST NOT create a second entry. Holds
   both for a re-grant inside one session and for an item already present on a
   loaded snapshot.

2. **Observable dedup (AC #2, the lie-detector).** When the apply path
   suppresses a duplicate it MUST emit a watcher event so the GM panel can
   confirm the dedup engaged rather than the narrator silently winning. The
   contract: ``_watcher_publish("item_gain.deduped", {...},
   component="inventory")`` carrying the item ``name`` — the same
   ``component="inventory"`` family as the existing ``item_gain.catalog_resolved``
   / ``item_gain.narrator_minted`` emits. "No Silent Fallbacks" (CLAUDE.md): a
   dropped grant that fires an event is *loud*, not silent.

3. **No over-block (regression guard).** Distinct items still stack; a fresh
   first grant still lands. The dedup must not become a global "one item ever"
   gate.

4. **No-op semantics, not quantity-merge (chosen interpretation).** The
   observed bug is the narrator *re-narrating the same acquisition*, exactly
   like the container double-retrieval — so the duplicate is suppressed
   (quantity unchanged), NOT counted up. A genuine "you gained 3 more arrows"
   stack-merge is a different signal we don't have here. This is a documented
   design call (see the session Design Deviations + Delivery Findings); if the
   team picks merge-on-quantity instead, ``test_dedup_does_not_increment_quantity``
   is the single test to flip in the same PR.

The driver is the real ``_apply_narration_result_to_snapshot`` production
function (CLAUDE.md "Verify Wiring, Not Just Existence" — no helper-in-isolation
tests, and no source-text ``read_text()`` sentinels).
"""

from __future__ import annotations

import copy

import pytest

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.genre.models.inventory import InventoryConfig
from sidequest.server import narration_apply
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from tests._helpers.session_room import room_for

# The watcher event_type the fix must emit on a suppressed duplicate grant.
# Pinned here as the contract Dev implements (mirrors the existing
# ``item_gain.catalog_resolved`` / ``item_gain.narrator_minted`` family).
DEDUP_EVENT = "item_gain.deduped"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def pack_no_catalog(snapshot_with_pack):
    """The synthetic pack with an EMPTY item catalog + ``worlds={}`` so every
    gained item bare-mints (``narrator:<slug>``) instead of binding to authored
    mechanics. Mirrors the stubbing in
    ``test_item_gain_catalog_resolution.py`` (deepcopy drops the conftest
    ``worlds={}`` stub, so re-set it). Dedup is orthogonal to catalog
    resolution — we want the plain narrator-mint path under test.
    """
    _snap, base_pack = snapshot_with_pack
    pack = copy.deepcopy(base_pack)
    pack.inventory = InventoryConfig(item_catalog=[])
    pack.worlds = {}
    return pack


@pytest.fixture
def captured_events(monkeypatch):
    """Capture every ``_watcher_publish`` call made by the apply path.

    Patches the name WHERE USED (``narration_apply._watcher_publish``), not
    where defined (``watcher_hub.publish_event``) — python.md #6. Synchronous,
    so no watcher-hub event loop is needed for the lie-detector assertion.
    """
    events: list[dict] = []

    def _capture(event_type, fields, **kwargs):
        events.append({"event_type": event_type, "fields": fields, **kwargs})

    monkeypatch.setattr(narration_apply, "_watcher_publish", _capture)
    return events


def _gain(*names: str) -> NarrationTurnResult:
    """Build a ``NarrationTurnResult`` granting one bare item per name."""
    return NarrationTurnResult(
        narration="You pick something up.",
        items_gained=[{"name": n} for n in names],
    )


def _silver_shoes_dict() -> dict:
    """A pre-seeded inventory entry shaped like the apply path's own
    narrator mint, simulating an item already present on a loaded snapshot."""
    return {
        "id": "narrator:silver_shoes",
        "name": "silver shoes",
        "description": "A pair of enchanted silver shoes.",
        "category": "treasure",
        "value": 0,
        "weight": 0.0,
        "rarity": "common",
        "narrative_weight": 0.5,
        "tags": [],
        "equipped": False,
        "quantity": 1,
        "uses_remaining": None,
        "state": "Carried",
    }


def _names(char) -> list[str]:
    return [str(i.get("name", "")) for i in char.core.inventory.items]


def _dedup_events(events: list[dict]) -> list[dict]:
    return [e for e in events if e["event_type"] == DEDUP_EVENT]


def _apply(snap, result, pack):
    _apply_narration_result_to_snapshot(snap, result, "Sam", pack=pack, room=room_for(snap))


# ---------------------------------------------------------------------------
# 1. The bug: a re-grant must not create a duplicate stack.
# ---------------------------------------------------------------------------


def test_regrant_same_item_does_not_create_duplicate_stack(
    snapshot_with_pack, character_named_sam, pack_no_catalog
):
    """Oz turn-7 repro: grant 'silver shoes', then grant it again on the same
    snapshot. The second grant must be suppressed — exactly one entry."""
    snap, _ = snapshot_with_pack
    snap.characters.append(character_named_sam)

    _apply(snap, _gain("silver shoes"), pack_no_catalog)
    assert _names(character_named_sam) == ["silver shoes"], "first grant should land once"

    _apply(snap, _gain("silver shoes"), pack_no_catalog)

    assert _names(character_named_sam) == ["silver shoes"], (
        "re-grant created a duplicate stack — the gained lane appended without "
        "an identity check (the 126-33 bug)"
    )


def test_dedup_against_preexisting_inventory_item(
    snapshot_with_pack, character_named_sam, pack_no_catalog
):
    """Loaded-save path: the character ALREADY holds 'silver shoes' before any
    grant this turn. A fresh grant of the same item must dedup against the
    existing ledger entry, not just against items added in the same call."""
    snap, _ = snapshot_with_pack
    character_named_sam.core.inventory.items.append(_silver_shoes_dict())
    snap.characters.append(character_named_sam)

    _apply(snap, _gain("silver shoes"), pack_no_catalog)

    assert _names(character_named_sam) == ["silver shoes"], (
        "grant duplicated an item already present on the loaded snapshot"
    )


def test_dedup_is_case_insensitive_on_name(
    snapshot_with_pack, character_named_sam, pack_no_catalog
):
    """The existing ``items_lost`` / ``items_discarded`` lanes match by
    case-folded name; the gained-lane dedup must too. Existing 'silver shoes',
    grant 'Silver Shoes' -> still one entry."""
    snap, _ = snapshot_with_pack
    character_named_sam.core.inventory.items.append(_silver_shoes_dict())
    snap.characters.append(character_named_sam)

    _apply(snap, _gain("Silver Shoes"), pack_no_catalog)

    assert len(character_named_sam.core.inventory.items) == 1, (
        "case-different name ('Silver Shoes' vs 'silver shoes') leaked a "
        "duplicate — dedup must case-fold like the discard/lost lanes"
    )


def test_dedup_matches_by_id_when_names_differ(
    snapshot_with_pack, character_named_sam, pack_no_catalog
):
    """Identity match is id-first: an existing entry with id
    ``narrator:silver_shoes`` must dedup a grant that carries that same id even
    if the narrator spelled the display name differently."""
    snap, _ = snapshot_with_pack
    character_named_sam.core.inventory.items.append(_silver_shoes_dict())
    snap.characters.append(character_named_sam)

    result = NarrationTurnResult(
        narration="You notice the silver footwear again.",
        items_gained=[{"name": "the silver slippers", "id": "narrator:silver_shoes"}],
    )
    _apply(snap, result, pack_no_catalog)

    assert len(character_named_sam.core.inventory.items) == 1, (
        "grant with a matching id but different name should dedup by id"
    )


# ---------------------------------------------------------------------------
# 2. Observability — the dedup must be loud (AC #2, lie-detector).
# ---------------------------------------------------------------------------


def test_regrant_emits_inventory_dedup_watcher_event(
    snapshot_with_pack, character_named_sam, pack_no_catalog, captured_events
):
    """When the apply path suppresses a duplicate grant it MUST publish an
    ``item_gain.deduped`` watcher event (component=inventory) naming the item.
    Without it the GM panel can't distinguish 'dedup fired' from 'grant never
    happened' (No Silent Fallbacks)."""
    snap, _ = snapshot_with_pack
    snap.characters.append(character_named_sam)

    _apply(snap, _gain("silver shoes"), pack_no_catalog)
    # Only the second (duplicate) grant should emit a dedup event.
    assert _dedup_events(captured_events) == [], "first grant must not emit a dedup event"

    _apply(snap, _gain("silver shoes"), pack_no_catalog)

    dedups = _dedup_events(captured_events)
    assert len(dedups) == 1, (
        f"expected exactly one {DEDUP_EVENT} event on the duplicate grant; "
        f"got {[e['event_type'] for e in captured_events]}"
    )
    evt = dedups[0]
    assert evt.get("component") == "inventory", "dedup event must be component=inventory"
    assert str(evt["fields"].get("name", "")).strip().lower() == "silver shoes", (
        "dedup event must name the suppressed item so the GM panel can read it"
    )


# ---------------------------------------------------------------------------
# 3. No over-block — distinct items still stack; first grant still lands.
# ---------------------------------------------------------------------------


def test_distinct_items_are_not_over_blocked(
    snapshot_with_pack, character_named_sam, pack_no_catalog, captured_events
):
    """Regression guard: granting two DIFFERENT items must produce two
    entries, and must NOT emit a dedup event. The fix is identity-scoped, not
    a global one-item gate."""
    snap, _ = snapshot_with_pack
    snap.characters.append(character_named_sam)

    _apply(snap, _gain("silver shoes"), pack_no_catalog)
    _apply(snap, _gain("ruby slippers"), pack_no_catalog)

    assert sorted(_names(character_named_sam)) == ["ruby slippers", "silver shoes"], (
        "distinct items were over-blocked — dedup must key on item identity"
    )
    assert _dedup_events(captured_events) == [], (
        "a distinct second item must not trip the dedup event"
    )


def test_fresh_first_grant_lands_and_emits_no_dedup(
    snapshot_with_pack, character_named_sam, pack_no_catalog, captured_events
):
    """Happy-path guard: a single fresh grant into an empty inventory still
    appends normally and fires no dedup event."""
    snap, _ = snapshot_with_pack
    snap.characters.append(character_named_sam)

    _apply(snap, _gain("silver shoes"), pack_no_catalog)

    assert _names(character_named_sam) == ["silver shoes"]
    assert _dedup_events(captured_events) == [], "a first-time grant is not a dedup"


# ---------------------------------------------------------------------------
# 4. No-op semantics: the duplicate is suppressed, not counted up.
# ---------------------------------------------------------------------------


def test_dedup_does_not_increment_quantity(
    snapshot_with_pack, character_named_sam, pack_no_catalog
):
    """Chosen contract (see module docstring): a re-narration duplicate is a
    no-op — the surviving entry's quantity is UNCHANGED, not merged to 2. The
    Oz silver shoes are a single unique item the narrator mentioned twice, not
    a second pair acquired.

    NOTE (design call): if the team decides duplicate grants should merge
    quantities instead, this is the one test to flip — change the expected
    quantity to 2. The no-duplicate-ENTRY contract above is unaffected either
    way.
    """
    snap, _ = snapshot_with_pack
    character_named_sam.core.inventory.items.append(_silver_shoes_dict())
    snap.characters.append(character_named_sam)

    _apply(snap, _gain("silver shoes"), pack_no_catalog)

    items = character_named_sam.core.inventory.items
    assert len(items) == 1, "duplicate grant must not create a second entry"
    assert items[0]["quantity"] == 1, (
        "no-op dedup must leave quantity unchanged (chosen interpretation); a "
        "value of 2 means the fix merged quantities instead — confirm the "
        "design call before changing this assertion"
    )
