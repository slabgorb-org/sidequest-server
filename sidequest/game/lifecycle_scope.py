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

# The active/dormant status sets for each type.
# ADR-137: a quest that FINISHED in any way is a dormant, recall-able note. The
# canonical quest-status vocabulary is the narrator's ``record_quest`` tool field
# (``sidequest/agents/tools/record_quest.py``): "active / completed / failed /
# resolved". status is a free-form LLM-set string (no enum), so dormancy is a
# TERMINAL-status ALLOWLIST — "completed", "failed", and "resolved" are all
# finished threads; "active" and any non-terminal mid-flight status are live
# pressure riding the ``state_summary`` floor.
_QUEST_TERMINAL_STATUSES = frozenset({"completed", "failed", "resolved"})
_TROPE_ACTIVE_STATUS = "progressing"  # ADR-128: the governor caps progressing at 3.


def quest_is_dormant(entry: QuestEntry) -> bool:
    """True when a quest is a DORMANT note (index it), False when ACTIVE (floor).

    ADR-137: a quest in any TERMINAL status (``"completed"`` / ``"failed"`` /
    ``"resolved"``) is dormant — a finished thread recalled by pertinence. ANY
    other status (active / progressing / …) is active pressure that already rides
    the ``state_summary`` floor, so it is NOT dormant and must NOT be indexed
    (double-render guard)."""
    return entry.status in _QUEST_TERMINAL_STATUSES


def trope_is_dormant(state: TropeState) -> bool:
    """True when a trope is a DORMANT note (index it), False when ACTIVE (floor).

    ADR-128: only ``status == "progressing"`` is active (the temporal governor caps
    progressing tropes at 3, so the active set is bounded and rides the existing
    trope-foreground floor). ``"dormant"`` and ``"resolved"`` are dormant notes —
    indexed for callback recall. Anything that is not progressing is dormant."""
    return state.status != _TROPE_ACTIVE_STATUS
