"""Wiring test — bind_rig_pools walks a snapshot and binds every character.

Story 53-2, Epic 53. AC5: the materializer-level integration entry point
``bind_rig_pools(snap)`` walks ``snap.characters``, calls the per-character
binder, and leaves the snapshot with bound pools wherever a vessel item was
present in inventory.

Per CLAUDE.md "Every Test Suite Needs a Wiring Test": this asserts the
function is not only reachable but actually does what the chargen flow
needs — given a GameSnapshot whose character carries a rig in inventory,
after ``bind_rig_pools`` the snapshot's character has a populated
``rig_pool``.

This file also asserts the helper is wired into a *production* module
(not just tests) so the binding actually fires in the live server. The
"only callers are tests" wiring failure has bitten 53-class subsystems
before — the importer-grep test is the lie detector.

These tests are RED until Dev wires the binder and adds a production
call site.
"""

from __future__ import annotations

import pathlib

import pytest


def _vessel_item_dict(
    *,
    item_id: str = "rig_tier_1_prospect",
    composure: int = 4,
    composure_max: int = 4,
) -> dict:
    return {
        "id": item_id,
        "name": "Prospect Rig",
        "category": "vessel",
        "tags": [
            "vessel",
            "rig",
            "tier-1",
            f"composure:{composure}",
            f"composure_max:{composure_max}",
        ],
    }


def _snapshot_with_rig_character(*, name: str = "Mira", item_id: str = "rig_tier_1_prospect"):
    """Build a fresh GameSnapshot containing one character with a vessel item.

    Goes through the same models that the chargen / world-materialization
    paths use — Character → CreatureCore → Inventory.
    """
    from sidequest.game import Character, CreatureCore, Inventory
    from sidequest.game.session import GameSnapshot

    core = CreatureCore(
        name=name,
        description="A driver.",
        personality="Watchful.",
        level=1,
        xp=0,
        inventory=Inventory(items=[_vessel_item_dict(item_id=item_id)]),
        statuses=[],
        acquired_advancements=[],
    )
    char = Character(
        core=core,
        backstory="From the road.",
        narrative_state="",
        hooks=[],
        char_class="Wheelman",
        race="Human",
        pronouns="",
        stats={},
        abilities=[],
        affinities=[],
        is_friendly=True,
        known_facts=[],
        resolved_archetype=None,
        archetype_provenance=None,
    )
    snap = GameSnapshot()
    snap.characters.append(char)
    return snap


# ---------------------------------------------------------------------------
# Snapshot-walker behavior — AC5 end-to-end.
# ---------------------------------------------------------------------------


def test_bind_rig_pools_binds_every_character_with_a_vessel_item() -> None:
    """Wiring: snapshot in → snapshot's characters carry bound rig_pools out."""
    from sidequest.game import bind_rig_pools

    snap = _snapshot_with_rig_character(name="Mira", item_id="rig_tier_1_prospect")

    bind_rig_pools(snap)

    assert len(snap.characters) == 1
    bound = snap.characters[0].core.rig_pool
    assert bound is not None
    assert bound.character_id == "Mira"
    assert bound.chassis_id == "rig_tier_1_prospect"
    assert bound.current == 4
    assert bound.max == 4


def test_bind_rig_pools_uses_character_core_name_as_character_id() -> None:
    """The snapshot-walker derives ``character_id`` from ``core.name``.

    Mirrors the convention in
    :func:`sidequest.game.chassis.rebind_chassis_bonds_to_character`,
    which is called with ``character.core.name`` from the websocket
    session handler.
    """
    from sidequest.game import bind_rig_pools

    snap = _snapshot_with_rig_character(name="Ash", item_id="rig_tier_1_prospect")

    bind_rig_pools(snap)

    assert snap.characters[0].core.rig_pool is not None
    assert snap.characters[0].core.rig_pool.character_id == "Ash"


def test_bind_rig_pools_leaves_non_rig_characters_unbound() -> None:
    """Characters with no vessel item end up with ``rig_pool is None``."""
    from sidequest.game import Character, CreatureCore, Inventory, bind_rig_pools
    from sidequest.game.session import GameSnapshot

    non_rig_core = CreatureCore(
        name="Pilgrim",
        description="A walker.",
        personality="Tired.",
        level=1,
        xp=0,
        inventory=Inventory(items=[]),
        statuses=[],
        acquired_advancements=[],
    )
    char = Character(
        core=non_rig_core,
        backstory="On foot.",
        narrative_state="",
        hooks=[],
        char_class="Wanderer",
        race="Human",
        pronouns="",
        stats={},
        abilities=[],
        affinities=[],
        is_friendly=True,
        known_facts=[],
        resolved_archetype=None,
        archetype_provenance=None,
    )
    snap = GameSnapshot()
    snap.characters.append(char)

    bind_rig_pools(snap)

    assert snap.characters[0].core.rig_pool is None


def test_bind_rig_pools_is_idempotent_on_reload() -> None:
    """Calling ``bind_rig_pools`` twice does NOT clobber damaged pools.

    Real flow: chargen confirm → bind. Snapshot save → reload (pool is
    already populated from save). Session-start handler may call bind
    again (defensive); the damaged pool must be preserved.
    """
    from sidequest.game import bind_rig_pools

    snap = _snapshot_with_rig_character()
    bind_rig_pools(snap)
    assert snap.characters[0].core.rig_pool is not None
    # Simulate combat damage.
    snap.characters[0].core.rig_pool.apply_delta(-3)
    assert snap.characters[0].core.rig_pool.current == 1

    bind_rig_pools(snap)  # second call — must not reset

    assert snap.characters[0].core.rig_pool.current == 1


def test_bind_rig_pools_no_op_on_empty_snapshot() -> None:
    """A snapshot with no characters is a hard no-op (no errors, no spans)."""
    from sidequest.game import bind_rig_pools
    from sidequest.game.session import GameSnapshot

    snap = GameSnapshot()
    assert snap.characters == []

    bind_rig_pools(snap)  # must not raise

    assert snap.characters == []


def test_bind_rig_pools_propagates_invalid_vessel_tags_loudly() -> None:
    """A character with a malformed vessel item: ``bind_rig_pools`` raises.

    No silent skip — the chargen flow needs to know it failed so the
    operator can fix the content. (Story 53-3 may upgrade this to a
    per-character error policy; 53-2's job is to fail loud.)
    """
    from sidequest.game import (
        Character,
        CreatureCore,
        InvalidVesselTagsError,
        Inventory,
        bind_rig_pools,
    )
    from sidequest.game.session import GameSnapshot

    malformed_item = {
        "id": "rig_broken",
        "name": "Broken Rig",
        "category": "vessel",
        "tags": ["vessel", "rig", "composure_max:4"],  # missing composure:N
    }
    core = CreatureCore(
        name="Mira",
        description="A driver.",
        personality="Watchful.",
        level=1,
        xp=0,
        inventory=Inventory(items=[malformed_item]),
        statuses=[],
        acquired_advancements=[],
    )
    char = Character(
        core=core,
        backstory="From the road.",
        narrative_state="",
        hooks=[],
        char_class="Wheelman",
        race="Human",
        pronouns="",
        stats={},
        abilities=[],
        affinities=[],
        is_friendly=True,
        known_facts=[],
        resolved_archetype=None,
        archetype_provenance=None,
    )
    snap = GameSnapshot()
    snap.characters.append(char)

    with pytest.raises(InvalidVesselTagsError):
        bind_rig_pools(snap)


# ---------------------------------------------------------------------------
# Production wiring — the binder must be imported from a non-test module.
# ---------------------------------------------------------------------------


def test_bind_rig_pools_imported_by_production_module() -> None:
    """CLAUDE.md ``Verify Wiring, Not Just Existence``: at least one
    non-test module under ``sidequest/`` imports ``bind_rig_pools`` (or
    ``bind_rig_pool_from_inventory``) so the live server actually calls
    it.

    Mirrors the wiring discipline that
    :func:`sidequest.game.chassis.rebind_chassis_bonds_to_character`
    follows via ``sidequest/server/websocket_session_handler.py``.

    Dev picks the call site (chargen-loadout completion, websocket
    session start, etc.); this test only enforces that *some* production
    module references the helper.
    """
    server_root = pathlib.Path(__file__).resolve().parents[2] / "sidequest"
    assert server_root.is_dir(), f"expected production package at {server_root}"

    callers: list[pathlib.Path] = []
    for path in server_root.rglob("*.py"):
        # Skip the binder's own definition module and any test files.
        if "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        # Skip the file that defines the binder (importing yourself is
        # not a wiring proof).
        if "def bind_rig_pools" in text or "def bind_rig_pool_from_inventory" in text:
            continue
        if "bind_rig_pools" in text or "bind_rig_pool_from_inventory" in text:
            callers.append(path)

    assert callers, (
        "bind_rig_pools / bind_rig_pool_from_inventory has no production "
        "caller — the helper exists but the materializer never calls it. "
        "Wire it into the chargen-complete / session-start flow."
    )
