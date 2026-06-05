"""Story 84-5 (WI-2) — per-type active/dormant predicates (RED phase).

ADR-118 Amendment §A2 (DORMANT-ONLY): an ACTIVE quest/trope rides the floor (it
applies whether named or not); a DORMANT one is a note, INDEXED for recall by
pertinence. Each type declares an active/dormant predicate that ROUTES it.

THE CONTRACT (net-new pure predicates, e.g. sidequest.game.lifecycle_scope):

    def quest_is_dormant(entry: QuestEntry) -> bool   # status == "completed"
    def trope_is_dormant(state: TropeState) -> bool   # status != "progressing"

Quest predicate (ADR-137): a completed quest is a dormant note; any other status
(active/progressing/…) is active pressure → floor.
Trope predicate (ADR-128): only "progressing" is active (the governor caps it at 3);
"dormant" and "resolved" are dormant → index.

Pure, exhaustive over the known statuses. Synthetic fixtures only.
"""

from __future__ import annotations

from sidequest.game.session import QuestEntry, TropeState

# ===========================================================================
# AC-1 — quest predicate
# ===========================================================================


class TestQuestPredicate:
    def test_quest_completed_is_dormant(self) -> None:
        from sidequest.game.lifecycle_scope import quest_is_dormant

        assert quest_is_dormant(QuestEntry(title="Slay the dragon", status="completed")) is True

    def test_quest_active_is_not_dormant(self) -> None:
        from sidequest.game.lifecycle_scope import quest_is_dormant

        assert quest_is_dormant(QuestEntry(title="Slay the dragon", status="active")) is False

    def test_quest_progressing_is_not_dormant(self) -> None:
        """Any non-completed status is active pressure (rides the floor)."""
        from sidequest.game.lifecycle_scope import quest_is_dormant

        assert quest_is_dormant(QuestEntry(title="q", status="progressing")) is False


# ===========================================================================
# AC-1 — trope predicate
# ===========================================================================


class TestTropePredicate:
    def test_trope_progressing_is_active(self) -> None:
        from sidequest.game.lifecycle_scope import trope_is_dormant

        assert trope_is_dormant(TropeState(id="t1", status="progressing")) is False

    def test_trope_dormant_is_dormant(self) -> None:
        from sidequest.game.lifecycle_scope import trope_is_dormant

        assert trope_is_dormant(TropeState(id="t1", status="dormant")) is True

    def test_trope_resolved_is_dormant(self) -> None:
        """A resolved trope is a past note — dormant, indexed for callback recall."""
        from sidequest.game.lifecycle_scope import trope_is_dormant

        assert trope_is_dormant(TropeState(id="t1", status="resolved")) is True
