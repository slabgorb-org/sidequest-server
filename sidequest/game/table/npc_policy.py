"""NPC seat auto-commit — a real basis, not narration whim.

A ruleset-module policy over (own strength_band, pot size, OCEAN/disposition)
returns a TableCommit. Confident/low-neuroticism → slow-play strong or bluff
weak; anxious → fold early; larcenous disposition → likelier to Cheat. Testable
in isolation, deterministic under a seeded rng. Emits table.npc_commit.
"""

from __future__ import annotations

import random

from sidequest.game.table.types import TableCommit, TableSeat, TableState
from sidequest.telemetry.spans import table_npc_commit_span

_BAND_RANK = {"weak": 0, "marginal": 1, "decent": 2, "strong": 3, "monster": 4}

# --- Policy knobs (a content author can tune these without touching the logic) ---
# Probabilities are p(action) GIVEN the guard condition.
_P_CHEAT_LARCENOUS = 0.35  # larcenous NPC cheats when rank <= 2 (mediocre hand)
_N_FOLD_ANXIOUS = 0.6  # neuroticism >= this → anxious enough to fold a weak hand
_P_BLUFF_WEAK_CALM = 0.40  # weak but calm: p(bluff) instead of fold
_N_CALM = 0.40  # neuroticism < this → "calm" for strong-hand slow-play
_P_CALL_STRONG_CALM = 0.50  # strong + calm: p(slow-play call) instead of raise
_P_RAISE_MIDDLING = 0.25  # middling hand: p(raise) instead of call
_POT_PRESSURE_CHIPS = 10  # pot >= this pressures a weak hand to fold (commitment pressure)


def decide_npc_commit(state: TableState, npc: TableSeat, *, rng: random.Random) -> TableCommit:
    band = str(npc.private_state.get("strength_band", "weak"))
    rank = _BAND_RANK.get(band, 0)
    ocean = npc.private_state.get("ocean", {}) or {}
    neuroticism = float(ocean.get("neuroticism", 0.5))
    disposition = str(npc.private_state.get("disposition", "neutral"))
    pot = sum(state.pot.contributions.values())

    beat = _choose_beat(
        rank=rank, neuroticism=neuroticism, disposition=disposition, pot=pot, rng=rng
    )
    amount = _bet_amount(beat, rank=rank, rng=rng)
    target = None
    if beat in ("read_table", "accuse"):
        target = _pick_opponent(state, npc)
        if target is None:
            beat, amount = "call", 0

    with table_npc_commit_span(seat=npc.seat_id, strength_band=band, pot=pot, chosen_beat=beat):
        pass
    return TableCommit(seat_id=npc.seat_id, beat_id=beat, amount=amount, target_seat=target)


def _choose_beat(
    *, rank: int, neuroticism: float, disposition: str, pot: int, rng: random.Random
) -> str:
    # larcenous NPCs reach for the deck when their hand is mediocre
    if disposition == "larcenous" and rank <= 2 and rng.random() < _P_CHEAT_LARCENOUS:
        return "cheat"
    if rank == 0:
        # weak hand
        if neuroticism >= _N_FOLD_ANXIOUS:
            return "fold"  # anxious + weak → fold
        if pot >= _POT_PRESSURE_CHIPS:
            return "fold"  # big pot + nothing → fold under commitment pressure
        # weak but calm into a small pot → bluff sometimes, else fold
        return "bluff" if rng.random() < _P_BLUFF_WEAK_CALM else "fold"
    if rank >= 3:
        # strong + calm → slow-play (call) or press (raise)
        # NOTE (V1): pot currently scales only the weak-hand fold decision above;
        # broader pot-scaling (e.g. pot-odds-driven calls here) is a deliberate
        # deferral to playtest tuning.
        if neuroticism < _N_CALM and rng.random() < _P_CALL_STRONG_CALM:
            return "call"
        return "raise"
    # middling hand: mostly call, occasional raise
    return "raise" if rng.random() < _P_RAISE_MIDDLING else "call"


def _bet_amount(beat: str, *, rank: int, rng: random.Random) -> int:
    if beat in ("raise", "bluff"):
        return 1 + rank + rng.randint(0, 2)
    if beat == "call":
        return 1
    return 0


def _pick_opponent(state: TableState, npc: TableSeat) -> str | None:
    for sid in state.order:
        s = state.find_seat(sid)
        if s is not None and s.seat_id != npc.seat_id and s.status == "active":
            return s.seat_id
    return None
