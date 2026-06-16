"""Persistence repository interface (ADR-115, Phase 0).

Decouples save-store consumers from the concrete storage engine. The
``transaction()`` context manager is the unit-of-work seam: multiple
writes inside one ``with`` block commit atomically or roll back together,
replacing the prior ``*_in_transaction(conn=...)`` raw-connection passing.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from sidequest.game.event_log import EventRow
    from sidequest.game.projection.cache import CachedDecision
    from sidequest.game.projection_filter import FilterDecision


@runtime_checkable
class SaveTransaction(Protocol):
    """A unit of work. Operations do NOT commit individually; the owning
    ``transaction()`` context manager commits on clean exit and rolls back
    on exception."""

    def append_event(self, *, kind: str, payload_json: str) -> EventRow: ...

    def write_projection(
        self, *, event_seq: int, player_id: str, decision: FilterDecision
    ) -> None: ...


@runtime_checkable
class SaveRepository(Protocol):
    """Storage-engine-agnostic save store. SQLite today; Postgres later."""

    def transaction(self) -> AbstractContextManager[SaveTransaction]: ...

    # Events ----------------------------------------------------------------
    def append_event(self, *, kind: str, payload_json: str) -> EventRow: ...

    def read_events_since(self, *, since_seq: int) -> list[EventRow]: ...

    def latest_event_seq(self) -> int: ...

    # Projection cache ------------------------------------------------------
    def write_projection(
        self, *, event_seq: int, player_id: str, decision: FilterDecision
    ) -> None: ...

    def read_projection_since(self, *, player_id: str, since_seq: int) -> list[CachedDecision]: ...
