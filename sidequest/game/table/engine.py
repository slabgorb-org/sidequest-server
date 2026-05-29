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
    table_accuse_span,
    table_cheat_span,
    table_fold_span,
    table_read_span,
    table_showdown_span,
)

# "bluff" is a pot action in both poker (a bluff is still a bet) and auction
# (feigning deeper pockets still advances your standing bid; framing is in narration).
_POT_ACTIONS = {"bet", "raise", "call", "bluff", "raise_bid"}

# Accuse opposed-check tuning (content could later override via cdef).
# DC = 10 + concealment - round(trace*8); trace=1.0,conceal=0 → DC 2 (near-certain
# catch); trace=0,conceal=0 → DC 10 (~50% at zero perception stat).
_ACCUSE_BASE_DC = 10
_ACCUSE_TRACE_WEIGHT = 8  # cheat_trace (0..1) contributes 0..8 toward catchability


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
    read_results: dict = {}

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
        _apply_commit(
            state,
            seat_id,
            commit,
            game=game,
            rng=rng,
            read_results=read_results,
        )

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
            read_results=read_results,
        )
    return _showdown(state, game=game, rng=rng, read_results=read_results)


def _apply_commit(state, seat_id, commit, *, game, rng, read_results) -> None:
    seat = state.find_seat(seat_id)
    beat = commit.beat_id
    if beat in ("fold", "withdraw"):
        seat.status = "folded"
        with table_fold_span(seat=seat_id, decision_point=state.decision_point):
            pass
        return
    if beat in _POT_ACTIONS:
        state.pot.contributions[seat_id] = state.pot.contributions.get(seat_id, 0) + max(
            0, commit.amount
        )
        # Mirror standing bid into private_state so AuctionTableGame.strength() can
        # read it without knowing the pot directly. Harmless for poker — poker's
        # strength() reads private_state["strength"], not _current_bid.
        seat.private_state["_current_bid"] = state.pot.contributions[seat_id]
        return
    _apply_signature_beat(
        state,
        seat_id,
        commit,
        game=game,
        rng=rng,
        read_results=read_results,
    )


def _apply_signature_beat(state, seat_id, commit, *, game, rng, read_results) -> None:
    beat = commit.beat_id
    seat = state.find_seat(seat_id)
    if beat == "cheat":
        result = game.cheat(seat, rng)  # mutates seat.private_state
        with table_cheat_span(
            seat=seat_id,
            strength_before=result.strength_before,
            strength_after=result.strength_after,
            new_trace=result.new_trace,
        ):
            pass
        return
    if beat in ("read_table", "read_room"):
        target = _require_target(state, seat_id, commit)
        reader_stat = int(seat.private_state.get("perception", 0))
        read = game.read(seat, target, reader_stat=reader_stat)
        read_results[seat_id] = read
        with table_read_span(
            reader=seat_id,
            target=target.seat_id,
            info_returned=str(read.info.get("strength_band", "")),
        ):
            pass
        return
    if beat == "accuse":
        target = _require_target(state, seat_id, commit)
        # Accusations are RECORDED at apply but ROLLED at showdown against the
        # FINAL cheat_trace — a cheat in a later decision point is still catchable.
        # The opposed check and table.accuse span both fire in _showdown.
        state.pending_accusations.append((seat_id, target.seat_id))
        return
    raise ValueError(
        f"unsupported table beat {commit.beat_id!r} for seat {seat_id!r} "
        f"(game_kind={state.game_kind!r})"
    )


def _require_target(state, seat_id, commit):
    if commit.target_seat is None:
        raise ValueError(f"beat {commit.beat_id!r} from {seat_id!r} requires a target_seat")
    target = state.find_seat(commit.target_seat)
    if target is None:
        raise ValueError(f"beat {commit.beat_id!r} targets unknown seat {commit.target_seat!r}")
    return target


def _showdown(state, *, game, rng, read_results) -> TableResolutionOutcome:
    forfeited: list[str] = []
    for accuser_id, target_id in state.pending_accusations:
        accuser = state.find_seat(accuser_id)
        target = state.find_seat(target_id)
        if accuser is None or target is None:
            continue  # a seat that left the table; accusation lapses
        accuser_stat = int(accuser.private_state.get("perception", 0))
        concealment = int(target.private_state.get("concealment", 0))
        trace_val = float(target.private_state.get("cheat_trace", 0.0))
        accuser_total = rng.randint(1, 20) + accuser_stat
        dc = _ACCUSE_BASE_DC + concealment - round(trace_val * _ACCUSE_TRACE_WEIGHT)
        landed = accuser_total >= dc
        with table_accuse_span(
            accuser=accuser_id,
            target=target_id,
            accuser_total=accuser_total,
            dc=dc,
            landed=landed,
        ):
            pass
        if landed:
            if target_id not in forfeited:
                forfeited.append(target_id)  # exposed cheat forfeits regardless of strength
        else:
            if accuser_id not in forfeited:
                forfeited.append(accuser_id)  # slandered an honest seat: accuser eats the cost
    state.pending_accusations = []  # resolved; hand is ending

    contenders = [s for s in state.seats if s.status == "active" and s.seat_id not in forfeited]
    if not contenders:
        raise ValueError("showdown with zero eligible contenders — all seats folded/forfeited")

    # Uniform fail-loud strength guard (single- and multi-contender alike) — never a
    # coin-flip default. Call game.strength() to validate; each kind computes from
    # its own private_state keys (poker: "strength"; auction: "_current_bid").
    # A missing key inside strength() will propagate as a KeyError here — loud, not silent.
    for s in contenders:
        try:
            game.strength(s)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"showdown: seat {s.seat_id!r} has no readable strength — "
                "fail loud, never a coin-flip default"
            ) from exc
    revealed = ",".join(f"{s.seat_id}:{game.strength(s)}" for s in contenders)
    winner = (
        contenders[0] if len(contenders) == 1 else max(contenders, key=lambda s: game.strength(s))
    )

    state.resolved_winner = winner.seat_id
    hint = f"{winner.party_name} takes {state.pot.stake_descriptor}"
    if forfeited:
        hint += f" (forfeits: {', '.join(forfeited)})"
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
        read_results=read_results,
    )
