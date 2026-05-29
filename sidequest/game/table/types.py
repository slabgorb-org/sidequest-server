"""Data shapes for the free-for-all N-seat table model.

The pydantic models are the *general* free-for-all: seats, a pot, and a
resolution order. ``private_state`` is an opaque dict the kind-specific
resolver interprets (poker hand vs auction valuation) — this is what lets
poker and auction share one model. Ephemeral: a TableState is a single
dramatic hand, never persisted across hands.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel


class TableNeedsOthersError(ValueError):
    """Raised when a table would seat fewer than 2 parties (generalizes ADR-116).

    No solitaire table — fail loud rather than instantiate a one-seat hand.
    """


class TableSeat(BaseModel):
    model_config = {"extra": "forbid"}

    seat_id: str  # stable: "seat_1"…
    party_name: str  # maps to an EncounterActor (PC or NPC)
    is_pc: bool
    status: Literal["active", "folded", "out"]
    private_state: dict[str, Any]
    #   poker   → {"cards": [...], "strength": int, "strength_band": str, "cheat_trace": float}
    #   auction → {"valuation": int, "max_bid": int}


class TablePot(BaseModel):
    model_config = {"extra": "forbid"}

    stake_kind: str  # "money" | "item" | "information" | "favor" (content-declared)
    stake_descriptor: str  # "the deed to the Bar-T" — narration label
    contributions: dict[str, int]  # seat_id → abstract chips (money); current high bid (auction)


class TableState(BaseModel):
    model_config = {"extra": "forbid"}

    game_kind: str  # "poker" | "auction" — resolver dispatch
    seats: list[TableSeat]
    pot: TablePot
    order: list[str]  # seat_ids in resolution/narration order
    dealer_seat: str
    decision_point: int = 0
    max_decision_points: int  # abstracted betting — small (e.g. 3), content-declared
    resolved_winner: str | None = None

    def find_seat(self, seat_id: str) -> TableSeat | None:
        for s in self.seats:
            if s.seat_id == seat_id:
                return s
        return None

    def active_seat_ids(self) -> list[str]:
        """Seat ids still in the hand, in resolution order."""
        active = {s.seat_id for s in self.seats if s.status == "active"}
        return [sid for sid in self.order if sid in active]


@dataclass(frozen=True)
class TableCommit:
    """One seat's sealed action for a decision point.

    Generalizes the dogfight sealed-letter commit from "keyed by role" to
    "keyed by seat". Rides the existing ``beat_selections`` channel: ``seat_id``
    is the actor's party→seat mapping, ``beat_id`` the authored action,
    ``amount`` the raise/bet chips, ``target_seat`` the Read/Accuse target.
    """

    seat_id: str
    beat_id: str  # "bet"|"raise"|"call"|"fold"|"bluff"|"cheat"|"read_table"|"accuse" (poker)
    amount: int = 0
    target_seat: str | None = None


@dataclass(frozen=True)
class CheatResult:
    strength_before: int
    strength_after: int
    new_trace: float


@dataclass(frozen=True)
class ReadResult:
    target_seat: str
    # injected into the reader's next private frame. NOTE: frozen=True prevents
    # rebinding this attribute but NOT mutation of the dict's contents, so callers
    # must treat info as read-only after construction — it flows into the per-seat
    # private narration frame and Task 13's perception firewall depends on it not
    # being mutated post-commit.
    info: dict[str, Any]


@dataclass
class TableResolutionOutcome:
    """Result of resolving ONE decision point (and showdown, when it triggers)."""

    showdown: bool
    resolved_winner: str | None
    pot_awarded_to: str | None
    stake_kind: str | None
    stake_descriptor: str | None
    narration_hint: str
    forfeited_seats: list[str] = field(default_factory=list)
    # Per-reader Read results to inject into the next private frame, keyed by seat_id.
    read_results: dict[str, ReadResult] = field(default_factory=dict)
