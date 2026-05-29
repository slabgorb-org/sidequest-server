"""Per-game-kind dispatch for the N-seat table model.

A TableGame implements the kind-specific bits the generic engine can't know:
how to deal, how strong a hand is, and (optionally) Cheat / Read. Kinds
register themselves at import time; an unknown game_kind fails loud
(UnknownTableGameError) — no silent default game.
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod

from sidequest.game.table.types import CheatResult, ReadResult, TablePot, TableSeat


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
