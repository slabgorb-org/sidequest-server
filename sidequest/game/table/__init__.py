"""N-seat table resolution (poker / auction / war_rig_crew).

The general model (seats + pot + order) lives in ``types``; per-game-kind
behavior (deal, strength, cheat, read, custom_beat) is registered in ``registry``
and implemented in ``poker`` / ``auction`` (competitive free-for-all) and
``war_rig`` (cooperative crewed-vessel, Story 86-6); the decision-point loop is
in ``engine``. See docs/superpowers/specs/2026-05-29-free-for-all-n-seat-table-design.md
and docs/superpowers/specs/2026-06-09-road-warrior-war-rig-crew-spec.md.
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

# Register built-in kinds on package import (side effects).
# Python's import cache ensures each module executes once regardless of how many
# paths reach it — double-registration cannot occur via this mechanism.
from sidequest.game.table import auction as _auction  # noqa: E402,F401
from sidequest.game.table import poker as _poker  # noqa: E402,F401
from sidequest.game.table import war_rig as _war_rig  # noqa: E402,F401
