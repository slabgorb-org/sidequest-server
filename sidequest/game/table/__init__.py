"""Free-for-all N-seat table resolution (poker / auction).

The general model (seats + pot + order) lives in ``types``; per-game-kind
behavior (deal, strength, cheat, read) is registered in ``registry`` and
implemented in ``poker`` / ``auction``; the decision-point loop is in
``engine``. See docs/superpowers/specs/2026-05-29-free-for-all-n-seat-table-design.md.
"""

from __future__ import annotations

from sidequest.game.table.types import (
    TableNeedsOthersError,
    TablePot,
    TableResolutionOutcome,
    TableSeat,
    TableState,
)

__all__ = [
    "TableNeedsOthersError",
    "TablePot",
    "TableResolutionOutcome",
    "TableSeat",
    "TableState",
]
