"""GameSnapshot.next_turn_directives — shared-world directive queue.

Spec 2026-05-20 confrontation-intent-validator step 4. Soft_suggest
dispatch (Task 5) appends here; orchestrator prompt assembly (Task 7)
consumes + clears here.
"""

from __future__ import annotations

from sidequest.game.session import GameSnapshot


def test_default_is_empty_list() -> None:
    snap = GameSnapshot()
    assert snap.next_turn_directives == []


def test_directives_round_trip_through_model_dump() -> None:
    snap = GameSnapshot(next_turn_directives=["Last turn suggested negotiation. Open it if true."])
    dumped = snap.model_dump()
    assert dumped["next_turn_directives"] == ["Last turn suggested negotiation. Open it if true."]
    restored = GameSnapshot.model_validate(dumped)
    assert restored.next_turn_directives == snap.next_turn_directives


def test_legacy_save_without_field_loads() -> None:
    """Old saves on disk don't have this field — model_validate must default it."""
    legacy_dump = {
        "genre_slug": "spaghetti_western",
        "world_slug": "dust_and_lead",
    }
    snap = GameSnapshot.model_validate(legacy_dump)
    assert snap.next_turn_directives == []


def test_directives_mutable_for_append() -> None:
    """Soft_suggest dispatch (Task 5) appends in-place to the live snapshot list."""
    snap = GameSnapshot()
    snap.next_turn_directives.append("first")
    snap.next_turn_directives.append("second")
    assert snap.next_turn_directives == ["first", "second"]
