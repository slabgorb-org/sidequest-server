"""Lifecycle-aware scope predicates — ADR-118 §A2 (Story 84-5, WI-2).

A quest / trope is either ACTIVE pressure (it applies this scene whether or not the
player names it → it rides its EXISTING floor path: the ``state_summary`` quest dump
and the trope-foreground section) or a DORMANT note (a completed quest, a resolved
trope → indexed for recall-by-pertinence, surfaced only when the player references
it). These pure predicates ROUTE each item; the routing gate that consumes them
lives in :mod:`sidequest.game.entity_sync` (dormant → projected into the index;
active → NOT indexed, so it never double-renders).

THE LOAD-BEARING INVARIANT (the inverted 84-3 trap): active quests/tropes ALREADY
reach the narrator prompt today. So 84-5 indexes ONLY the dormant ones. An active
item that got indexed would double-render — the gate must keep it out.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sidequest.game.session import QuestEntry, TropeState

# The ONE active status for each type. Everything else is dormant.
_QUEST_DORMANT_STATUS = "completed"  # ADR-137: a completed quest is a past note.
_TROPE_ACTIVE_STATUS = "progressing"  # ADR-128: the governor caps progressing at 3.


def quest_is_dormant(entry: QuestEntry) -> bool:
    """True when a quest is a DORMANT note (index it), False when ACTIVE (floor).

    ADR-137: a ``status == "completed"`` quest is dormant — a finished thread
    recalled by pertinence. ANY other status (active / progressing / …) is active
    pressure that already rides the ``state_summary`` floor, so it is NOT dormant
    and must NOT be indexed (double-render guard)."""
    return entry.status == _QUEST_DORMANT_STATUS


def trope_is_dormant(state: TropeState) -> bool:
    """True when a trope is a DORMANT note (index it), False when ACTIVE (floor).

    ADR-128: only ``status == "progressing"`` is active (the temporal governor caps
    progressing tropes at 3, so the active set is bounded and rides the existing
    trope-foreground floor). ``"dormant"`` and ``"resolved"`` are dormant notes —
    indexed for callback recall. Anything that is not progressing is dormant."""
    return state.status != _TROPE_ACTIVE_STATUS
