"""Per-player projection decision cache.

Backed by a SaveRepository (ADR-115, Phase 0). Written at fan-out time;
read at reconnect. The (event_seq, player_id) primary key means a re-fan
of the same event to the same player is idempotent (last write wins).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sidequest.game.projection_filter import FilterDecision

if TYPE_CHECKING:
    from sidequest.game.repository import SaveRepository


@dataclass(frozen=True)
class CachedDecision:
    event_seq: int
    include: bool
    payload_json: str | None


class ProjectionCache:
    def __init__(self, repository: SaveRepository) -> None:
        self._repo = repository

    def write(self, *, event_seq: int, player_id: str, decision: FilterDecision) -> None:
        """Write a cache row, committing its own transaction.

        Fan-out uses ``repository.transaction()`` directly so the event
        append and all cache writes share one transaction.
        """
        self._repo.write_projection(event_seq=event_seq, player_id=player_id, decision=decision)

    def read_since(self, *, player_id: str, since_seq: int) -> list[CachedDecision]:
        return self._repo.read_projection_since(player_id=player_id, since_seq=since_seq)
