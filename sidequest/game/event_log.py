"""Monotonic event log for a single game slug.

Every narrator-originated mutation (NARRATION, STATE_UPDATE, COMBAT_EVENT,
etc.) is appended here before fan-out. Peers catch up on reconnect via
read_since. Backed by a SaveRepository (ADR-115, Phase 0).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sidequest.game.repository import SaveRepository


@dataclass
class EventRow:
    seq: int
    kind: str
    payload_json: str
    created_at: str


class EventLog:
    def __init__(self, repository: SaveRepository) -> None:
        self._repo = repository

    @property
    def repository(self) -> SaveRepository:
        return self._repo

    @property
    def store(self):
        """TRANSITIONAL (ADR-115 P0): scrapbook persist in emitters.py still
        reaches store._conn. Removed when that slice lands. Do not add callers."""
        return self._repo.store  # type: ignore[attr-defined]

    def append(self, *, kind: str, payload_json: str) -> EventRow:
        """Append an event, committing its own transaction.

        Fan-out uses ``repository.transaction()`` directly so the event
        insert + cache writes share one transaction (see emitters.emit_event).
        """
        return self._repo.append_event(kind=kind, payload_json=payload_json)

    def read_since(self, *, since_seq: int) -> list[EventRow]:
        return self._repo.read_events_since(since_seq=since_seq)

    def latest_seq(self) -> int:
        return self._repo.latest_event_seq()
