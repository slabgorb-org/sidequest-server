"""Test helper replacing the retired narrator-driven encounter creation path.

Story 59-4 removed the ``result.confrontation`` → ``instantiate_encounter_from_trigger``
consumer from ``_apply_narration_result_to_snapshot``. Tests that need an active encounter
on the snapshot now call the lifecycle function directly via this helper.
"""

from __future__ import annotations

from sidequest.game.session import GameSnapshot
from sidequest.genre.models.pack import GenrePack
from sidequest.server.dispatch.encounter_lifecycle import (
    instantiate_encounter_from_trigger,
)


def trigger_encounter(
    snap: GameSnapshot,
    pack: GenrePack,
    encounter_type: str,
    player_name: str,
    *,
    npcs_present: list | None = None,
    additional_player_names: list[str] | None = None,
) -> None:
    """Set up an encounter on the snapshot the same way the old narration_apply path did."""
    instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type=encounter_type,
        player_name=player_name,
        npcs_present=npcs_present or [],
        genre_slug=snap.genre_slug,
        additional_player_names=additional_player_names,
    )
