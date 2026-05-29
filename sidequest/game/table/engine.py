"""The generic N-seat decision-point loop.

resolve_table() resolves ONE decision point: it applies each seat's committed
action in resolution order, then decides whether the hand goes to showdown
(≤1 active seat, or max_decision_points reached) or advances to the next
decision point. Kind-specific bits (deal, strength, cheat, read) are dispatched
through the registry. Every seat action emits one OTEL span — the GM panel's
lie detector. The dual dials are never touched; this reads/writes table_state.
"""

from __future__ import annotations

import random

from sidequest.game.table.registry import get_table_game
from sidequest.game.table.types import (
    TableCommit,
    TableNeedsOthersError,
    TableResolutionOutcome,
    TableState,
)
from sidequest.telemetry.spans import (
    table_fold_span,
    table_showdown_span,
)

_POT_ACTIONS = {"bet", "raise", "call", "bluff", "raise_bid"}


def deal_table(state: TableState, *, rng: random.Random) -> None:
    """Seat-deal: enforce ≥2 seats, dispatch the kind's deal(). Mutates state."""
    if len(state.seats) < 2:
        raise TableNeedsOthersError(
            f"table {state.game_kind!r} needs >= 2 seats, got {len(state.seats)}"
        )
    game = get_table_game(state.game_kind)  # raises UnknownTableGameError if unregistered
    game.deal(state.seats, state.pot, rng)


def resolve_table(
    state: TableState,
    *,
    commits: dict[str, TableCommit],
    rng: random.Random,
) -> TableResolutionOutcome:
    """Resolve one decision point. See module docstring."""
    game = get_table_game(state.game_kind)

    # Reject commits naming seats not on the table BEFORE applying anything —
    # fail loud before mutating, so a ghost commit can't leave a half-applied hand.
    for seat_id in commits:
        if state.find_seat(seat_id) is None:
            raise ValueError(f"commit for unknown seat {seat_id!r}")

    # Apply each committed action in resolution order (folded/out seats ignored).
    for seat_id in state.order:
        commit = commits.get(seat_id)
        if commit is None:
            continue
        seat = state.find_seat(seat_id)
        if seat is None:
            continue
        if seat.status != "active":
            continue
        _apply_commit(state, seat_id, commit, game=game, rng=rng)

    active = state.active_seat_ids()
    is_showdown = len(active) <= 1 or state.decision_point >= state.max_decision_points - 1
    if not is_showdown:
        state.decision_point += 1
        return TableResolutionOutcome(
            showdown=False,
            resolved_winner=None,
            pot_awarded_to=None,
            stake_kind=None,
            stake_descriptor=None,
            narration_hint="",
        )
    return _showdown(state, game=game, rng=rng)


def _apply_commit(state, seat_id, commit, *, game, rng) -> None:
    seat = state.find_seat(seat_id)
    beat = commit.beat_id
    if beat == "fold":
        seat.status = "folded"
        with table_fold_span(seat=seat_id, decision_point=state.decision_point):
            pass
        return
    if beat in _POT_ACTIONS:
        state.pot.contributions[seat_id] = state.pot.contributions.get(seat_id, 0) + max(
            0, commit.amount
        )
        return
    # cheat / read_table / accuse handled in Task 8's _apply_signature_beat
    _apply_signature_beat(state, seat_id, commit, game=game, rng=rng)


def _apply_signature_beat(state, seat_id, commit, *, game, rng) -> None:
    # Filled in Task 8. Until then, an unauthored beat must fail loud.
    raise ValueError(
        f"unsupported table beat {commit.beat_id!r} for seat {seat_id!r} "
        f"(game_kind={state.game_kind!r})"
    )


def _showdown(state, *, game, rng) -> TableResolutionOutcome:
    """Compare strengths among non-folded seats; award the pot. Forfeit logic
    (exposed cheats) is layered in Task 8 — here, no forfeits yet."""
    forfeited: list[str] = []
    contenders = [s for s in state.seats if s.status == "active"]
    if not contenders:
        # everyone folded except possibly one already-out seat — shouldn't happen
        # because resolve_table triggers showdown at ≤1 active; guard anyway.
        raise ValueError("showdown with zero active seats — engine invariant broken")

    if len(contenders) == 1:
        winner = contenders[0]
        revealed = ",".join(f"{s.seat_id}:{game.strength(s)}" for s in contenders)
    else:
        for s in contenders:
            if "strength" not in s.private_state:
                raise ValueError(
                    f"showdown: seat {s.seat_id!r} has no readable strength — "
                    "fail loud, never a coin-flip default"
                )
        revealed = ",".join(f"{s.seat_id}:{game.strength(s)}" for s in contenders)
        winner = max(contenders, key=lambda s: game.strength(s))

    state.resolved_winner = winner.seat_id
    hint = f"{winner.party_name} takes {state.pot.stake_descriptor}"
    with table_showdown_span(
        winner=winner.seat_id,
        forfeits=forfeited,
        pot_awarded=winner.seat_id,
        revealed_strengths=revealed,
    ):
        pass
    return TableResolutionOutcome(
        showdown=True,
        resolved_winner=winner.seat_id,
        pot_awarded_to=winner.seat_id,
        stake_kind=state.pot.stake_kind,
        stake_descriptor=state.pot.stake_descriptor,
        narration_hint=hint,
        forfeited_seats=forfeited,
    )
