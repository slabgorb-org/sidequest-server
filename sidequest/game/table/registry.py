"""Per-game-kind dispatch for the N-seat table model.

A TableGame implements the kind-specific bits the generic engine can't know:
how to deal, how strong a hand is, and (optionally) Cheat / Read. Kinds
register themselves at import time; an unknown game_kind fails loud
(UnknownTableGameError) — no silent default game.
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from sidequest.game.table.types import CheatResult, ReadResult, TablePot, TableSeat

if TYPE_CHECKING:
    from sidequest.game.table.types import TableCommit, TableState


class UnknownTableGameError(ValueError):
    """Raised when a confrontation names a table_game with no registered resolver."""


class TableGame(ABC):
    """Authority for one game_kind's dealing, strength, and signature beats."""

    #: registry key, also the value authors write in rules.yaml `table_game:`
    kind: str

    @abstractmethod
    def deal(self, seats: list[TableSeat], pot: TablePot, rng: random.Random) -> None:
        """Populate each seat's private_state and seed pot antes (mutates in place)."""

    @abstractmethod
    def strength(self, seat: TableSeat) -> int:
        """Showdown comparison value for this seat (higher wins)."""

    def cheat(self, seat: TableSeat, rng: random.Random) -> CheatResult:
        """Manipulate the real hand + raise cheat_trace. Override per kind."""
        raise NotImplementedError(f"table_game {self.kind!r} does not support Cheat")

    def read(self, reader: TableSeat, target: TableSeat, *, reader_stat: int) -> ReadResult:
        """Return REAL info about a target into the reader's frame. Override per kind."""
        raise NotImplementedError(f"table_game {self.kind!r} does not support Read")

    def custom_beat(
        self,
        state: TableState,
        seat: TableSeat,
        commit: TableCommit,
        *,
        rng: random.Random,
    ) -> None:
        """Resolve a kind-specific beat the generic engine can't (Story 86-6).

        The engine handles pot actions (bet/raise/call/bluff), fold, and the
        signature beats (cheat/read/accuse). Anything else — e.g. the War Rig's
        station verbs (steer/shoot/repair/scan) — is dispatched here so a kind
        can own its own resolution without the engine hardcoding every verb.

        The default fails loud, preserving No Silent Fallbacks: a kind that does
        NOT register a custom beat must not silently swallow an unknown beat
        (poker + ``shoot`` still raises). Override per kind to handle station
        verbs; emit a ``table.*`` span for each so the GM panel stays the lie
        detector.
        """
        raise ValueError(
            f"unsupported table beat {commit.beat_id!r} for seat {seat.seat_id!r} "
            f"(game_kind={state.game_kind!r}); kind {self.kind!r} registers no custom beat"
        )


_REGISTRY: dict[str, TableGame] = {}


def register_table_game(game: TableGame) -> None:
    """Register a table-game kind. Fails loud on a duplicate kind."""
    if game.kind in _REGISTRY:
        raise ValueError(f"table_game {game.kind!r} already registered")
    _REGISTRY[game.kind] = game


def get_table_game(kind: str) -> TableGame:
    """Resolve a registered table-game kind. Fails loud — never a default."""
    game = _REGISTRY.get(kind)
    if game is None:
        known = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise UnknownTableGameError(f"Unknown table_game {kind!r}; registered: {known}")
    return game
