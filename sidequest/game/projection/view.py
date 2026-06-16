"""GameStateView — narrow, read-only projection of session state the filter reads.

Implemented by SessionHandler (SessionGameStateView, added in Task 3).
Filter never mutates. Every method returns None where state is unknown
rather than raising — the filter treats unknown relationships conservatively.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class GameStateView(Protocol):
    def is_gm(self, player_id: str) -> bool: ...
    def seat_of(self, player_id: str) -> str | None: ...
    def character_of(self, player_id: str) -> str | None: ...
    def zone_of(self, character_id: str) -> str | None: ...
    def visible_to(self, viewer_character_id: str, target_character_id: str) -> bool: ...
    def owner_of_item(self, item_id: str) -> str | None: ...
    def party_of(self, player_id: str) -> str | None: ...


@dataclass
class SessionGameStateView:
    """Conservative GameStateView implementation.

    Zone tracking populated by
    ``WebSocketSessionHandler._build_game_state_view`` from
    per-character ``snapshot.character_locations[name]`` (Wave 2B,
    story 45-48), with party-frame fallback via
    ``snapshot.party_location()``. NPCs report their per-entity
    ``Npc.location``. ``hidden_characters``
    populated from stealth-flavored tokens on ``CreatureCore.statuses``
    (whole-token match; see ``WebSocketSessionHandler._HIDDEN_STATUS_TOKENS``).
    ``player_id_to_character`` maps the session's active ``player_id`` to
    the first entry in ``snapshot.characters`` — single-player today;
    MP seat-assignment (sprint 2) will feed the multi-player case.
    """

    gm_player_id: str | None
    player_id_to_character: dict[str, str] = field(default_factory=dict)
    party_id: str | None = None
    seat_assignments: dict[str, str] = field(default_factory=dict)
    character_zones: dict[str, str] = field(default_factory=dict)
    hidden_characters: set[str] = field(default_factory=set)

    def is_gm(self, player_id: str) -> bool:
        return self.gm_player_id is not None and player_id == self.gm_player_id

    def seat_of(self, player_id: str) -> str | None:
        return self.seat_assignments.get(player_id)

    def character_of(self, player_id: str) -> str | None:
        return self.player_id_to_character.get(player_id)

    def zone_of(self, character_id: str) -> str | None:
        return self.character_zones.get(character_id)

    def visible_to(self, viewer_character_id: str, target_character_id: str) -> bool:
        if target_character_id in self.hidden_characters:
            return False
        viewer_zone = self.character_zones.get(viewer_character_id)
        target_zone = self.character_zones.get(target_character_id)
        if viewer_zone is None or target_zone is None:
            return False
        return viewer_zone == target_zone

    def owner_of_item(self, item_id: str) -> str | None:
        return None  # Not yet tracked — stays conservative.

    def party_of(self, player_id: str) -> str | None:
        if player_id not in self.player_id_to_character:
            return None
        return self.party_id
