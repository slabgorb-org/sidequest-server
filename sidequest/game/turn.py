"""Turn management — phase tracking, round counting, and barrier semantics.

Two-tier turn model:
- interaction (granular): increments every player-narrator exchange.
  Powers fact/item discovery chronology. Monotonic, never resets.
- round (display): advances in lockstep with ``interaction`` via
  ``record_interaction()`` (Story 45-11, Strategy A). Shown to the
  player. The earlier "advances on narrative beats" model was never
  wired into the resolution pipeline — ``advance_round()`` had zero
  callers and the counter froze, drifting from
  ``MAX(narrative_log.round_number)`` over long sessions
  (Felix's Playtest 3: round=65 / max=72).

ADR-006: Both counters always increment, never reset.
Persisted across sessions — loading a save restores exact counts.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class TurnPhase(StrEnum):
    """The phases of a game turn (ADR-006)."""

    InputCollection = "InputCollection"
    IntentRouting = "IntentRouting"
    AgentExecution = "AgentExecution"
    StatePatch = "StatePatch"
    Broadcast = "Broadcast"


_PHASE_TRANSITIONS: dict[TurnPhase, TurnPhase] = {
    TurnPhase.InputCollection: TurnPhase.IntentRouting,
    TurnPhase.IntentRouting: TurnPhase.AgentExecution,
    TurnPhase.AgentExecution: TurnPhase.StatePatch,
    TurnPhase.StatePatch: TurnPhase.Broadcast,
    TurnPhase.Broadcast: TurnPhase.Broadcast,  # stays at last phase
}


class TurnManager(BaseModel):
    """Tracks current turn round, phase, and player input barrier.

    Two-tier model:
    - round: display counter for meaningful narrative beats.
    - interaction: monotonic counter for every player-narrator exchange.

    Both counters always increment, never reset. Persisted across sessions.

    The ``submitted`` set is runtime-only and skipped in serialization —
    populated as players submit and cleared on phase transitions.
    """

    model_config = {"extra": "forbid"}

    round: int = Field(default=1)
    interaction: int = Field(default=1)
    phase: TurnPhase = TurnPhase.InputCollection
    player_count: int = 1
    # submitted is runtime-only, not persisted.
    # We use a separate attribute, not a pydantic field, so it won't round-trip
    # through model_dump/validate_json.

    def model_post_init(self, __context: object) -> None:
        object.__setattr__(self, "_submitted", set())

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def get_round(self) -> int:
        return self.round

    def get_interaction(self) -> int:
        return self.interaction

    def get_phase(self) -> TurnPhase:
        return self.phase

    def set_player_count(self, count: int) -> None:
        self.player_count = count

    # ------------------------------------------------------------------
    # Mutation methods
    # ------------------------------------------------------------------

    def submit_input(self, player_id: str) -> None:
        """Submit input for a player. Advances to IntentRouting when all players submitted."""
        if self.phase != TurnPhase.InputCollection:
            return
        submitted: set[str] = object.__getattribute__(self, "_submitted")
        submitted.add(player_id)
        if len(submitted) >= self.player_count:
            self.phase = TurnPhase.IntentRouting
            submitted.clear()

    def recheck_barrier(self, effective_count: int) -> bool:
        """Re-evaluate the InputCollection barrier against a reduced denominator.

        Story 67-1: a crashed client is dropped from the awaited count without
        ever submitting. After that drop the barrier may already be satisfied
        by the players who DID submit, but ``submit_input`` only re-checks on a
        submission — and the remaining players have already submitted, so no
        further submission is coming. This recomputes the predicate against
        ``effective_count`` (PLAYING peers minus crash-released awaiters) and
        fires the barrier if the existing submissions satisfy it.

        Returns ``True`` if the barrier fired (phase advanced to IntentRouting),
        ``False`` otherwise. A no-op when not in InputCollection or when
        ``effective_count <= 0`` (never release a turn with nobody left to act).
        """
        if self.phase != TurnPhase.InputCollection:
            return False
        if effective_count <= 0:
            return False
        submitted: set[str] = object.__getattribute__(self, "_submitted")
        if len(submitted) >= effective_count:
            self.phase = TurnPhase.IntentRouting
            submitted.clear()
            return True
        return False

    def record_interaction(self) -> None:
        """Record a player-narrator interaction. Resets phase to InputCollection.

        Story 45-11 (Strategy A — turn_manager authoritative): advances
        ``round`` in lockstep with ``interaction``. Felix's Playtest 3 ended
        round=65 / max(narrative_log.round_number)=72 because the legacy
        ``advance_round()`` was never called from the live resolution
        pipeline — round froze while interaction kept ticking. The narrative
        log is written keyed by ``interaction`` (see write site in
        websocket_session_handler._execute_narration_turn), so for
        ``turn_manager.round`` to track ``MAX(narrative_log.round_number)``
        it must advance every time an interaction completes.
        """
        self.interaction += 1
        self.round += 1
        self.phase = TurnPhase.InputCollection
        submitted: set[str] = object.__getattribute__(self, "_submitted")
        submitted.clear()

    def advance_round(self) -> None:
        """Advance the display round (call on meaningful narrative beats)."""
        self.round += 1

    def advance(self) -> None:
        """Legacy: increment display round and reset phase."""
        self.round += 1
        self.phase = TurnPhase.InputCollection
        submitted: set[str] = object.__getattribute__(self, "_submitted")
        submitted.clear()

    def advance_phase(self) -> None:
        """Advance to the next phase within the current round."""
        self.phase = _PHASE_TRANSITIONS[self.phase]


class PreprocessedAction(BaseModel):
    """Player action after STT cleanup and perspective rewriting.

    Produced by the action preprocessor before being handed to agents.
    """

    model_config = {"frozen": True}

    you: str
    named: str
    intent: str
