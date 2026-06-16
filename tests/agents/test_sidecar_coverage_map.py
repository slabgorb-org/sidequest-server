"""Sidecar coverage map — enforces Phase C definition-of-done.

Each row maps a former sidecar field to its successor tool. Phase D
cannot proceed until every row has a successor.

Flipped to passing post-Task 27 (apply_world_patch ticks the final row,
``patches_other``). This is now a Phase D gate: it must stay passing or
Phase D's sidecar parser deletion regresses.
"""

from __future__ import annotations

# Each entry: sidecar_field -> successor tool name. All entries must be
# non-None for Phase D to proceed.
COVERAGE_MAP: dict[str, str | None] = {
    "dice_roll": "roll_dice",
    "patches_hp": "apply_damage",
    "patches_status": "apply_status",
    "patches_resource_pool": "update_resource_pool",
    "patches_disposition": "update_npc_disposition",
    "patches_other": "apply_world_patch",
    "journal_entries": "commit_known_fact",
    "scenario_advances": "advance_scene_clue",
    "encounter_advances": "advance_encounter_beat",
    "magic_effects": "apply_spell_effect",
    "trope_tick": "tick_tropes",
    "confrontation_advances": "advance_confrontation",
}


def test_phase_c_complete() -> None:
    """Phase D gate — every sidecar field has a successor tool."""
    unmigrated = [k for k, v in COVERAGE_MAP.items() if v is None]
    assert not unmigrated, (
        f"Sidecar fields without tool successors: {unmigrated!r}. Phase D cannot proceed."
    )
