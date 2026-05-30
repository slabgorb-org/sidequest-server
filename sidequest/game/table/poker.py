"""Poker table-game kind: real dealt 5-card hands, genuine strength.

Honest crunch where it's dramatic (Sebastien/Jade can see real card math).
A 52-card deck is dealt without replacement; strength is a coarse but real
hand ranking (high-card → pair → two-pair → trips → straight → flush →
full-house → quads → straight-flush) packed into a single comparable int.
Cheat/Read act on the REAL hand. Betting is abstracted by the engine.
"""

from __future__ import annotations

import random
from collections import Counter

from sidequest.game.table.registry import TableGame, register_table_game
from sidequest.game.table.types import CheatResult, ReadResult, TablePot, TableSeat

_RANKS = "23456789TJQKA"
_RANK_VALUE = {r: i for i, r in enumerate(_RANKS, start=2)}  # 2..14
_SUITS = "SHDC"
_FULL_DECK = [r + s for r in _RANKS for s in _SUITS]

_ANTE = 1  # abstract chips each seat antes at deal

# Coarse strength bands derived from the packed hand strength. Ordered low→high.
POKER_BANDS = ("weak", "marginal", "decent", "strong", "monster")

_PACK_BASE = 100  # each tiebreak (rank 2..14) is one base-100 digit
_TIEBREAK_SLOTS = 5  # a 5-card hand has at most 5 tiebreak values


def _categorize(cards: list[str]) -> tuple[int, list[int]]:
    """Return (category_rank, tiebreak_values_desc). Higher category wins."""
    values = sorted((_RANK_VALUE[c[0]] for c in cards), reverse=True)
    suits = [c[1] for c in cards]
    counts = Counter(values)
    # group by (count, value) so quads/trips/pairs sort to the front
    by_count = sorted(counts.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
    grouped_vals = [v for v, _ in by_count]
    count_shape = sorted(counts.values(), reverse=True)
    is_flush = len(set(suits)) == 1
    distinct = sorted(set(values))
    is_straight = len(distinct) == 5 and distinct[-1] - distinct[0] == 4
    # wheel straight A-2-3-4-5
    if set(values) == {14, 2, 3, 4, 5}:
        is_straight = True
        grouped_vals = [5, 4, 3, 2, 1]

    if is_straight and is_flush:
        return 8, grouped_vals
    if count_shape == [4, 1]:
        return 7, grouped_vals
    if count_shape == [3, 2]:
        return 6, grouped_vals
    if is_flush:
        return 5, grouped_vals
    if is_straight:
        return 4, grouped_vals
    if count_shape == [3, 1, 1]:
        return 3, grouped_vals
    if count_shape == [2, 2, 1]:
        return 2, grouped_vals
    if count_shape == [2, 1, 1, 1]:
        return 1, grouped_vals
    return 0, grouped_vals


def _hand_strength(cards: list[str]) -> int:
    """Pack (category, tiebreaks) into a single comparable int.

    Category always occupies the fixed most-significant slot by padding
    tiebreaks to exactly ``_TIEBREAK_SLOTS`` entries — this guarantees that any
    category-N hand beats any category-(N-1) hand regardless of kicker values
    (pair always > high-card, etc.). Pack and unpack share ``_PACK_BASE`` /
    ``_TIEBREAK_SLOTS`` so ``_band_for`` can recover the category by a fixed-width
    divide and the two can't drift.
    """
    category, tiebreaks = _categorize(cards)
    padded = (tiebreaks + [0] * _TIEBREAK_SLOTS)[:_TIEBREAK_SLOTS]
    strength = category
    for v in padded:
        strength = strength * _PACK_BASE + v
    return strength


def _band_for(strength: int) -> str:
    category = strength // (_PACK_BASE**_TIEBREAK_SLOTS)
    # category 0..8 → 5 bands
    if category >= 7:
        return "monster"
    if category >= 4:
        return "strong"
    if category == 3:
        return "decent"
    if category in (1, 2):
        return "marginal"
    return "weak"


class PokerTableGame(TableGame):
    kind = "poker"

    def deal(self, seats: list[TableSeat], pot: TablePot, rng: random.Random) -> None:
        deck = list(_FULL_DECK)
        rng.shuffle(deck)
        for seat in seats:
            hand = [deck.pop() for _ in range(5)]
            strength = _hand_strength(hand)
            seat.private_state["cards"] = hand
            seat.private_state["strength"] = strength
            seat.private_state["strength_band"] = _band_for(strength)
            seat.private_state["cheat_trace"] = 0.0
            pot.contributions[seat.seat_id] = pot.contributions.get(seat.seat_id, 0) + _ANTE

    def strength(self, seat: TableSeat) -> int:
        return int(seat.private_state["strength"])

    def cheat(self, seat: TableSeat, rng: random.Random) -> CheatResult:
        """Swap the weakest card for a better one drawn fresh; raise cheat_trace.

        Real advantage (strength recomputed), real evidence (trace climbs and
        compounds with repeated cheats).
        """
        before = int(seat.private_state["strength"])
        hand: list[str] = list(seat.private_state["cards"])
        held = set(hand)
        weakest = min(hand, key=lambda c: _RANK_VALUE[c[0]])
        candidates = [c for c in _FULL_DECK if c not in held]
        replacement = max(candidates, key=lambda c: _RANK_VALUE[c[0]])
        swapped = list(hand)
        swapped[swapped.index(weakest)] = replacement
        after_swapped = _hand_strength(swapped)
        # A cheat must never sabotage the cheater: swapping into a made straight/
        # flush would break it. Keep the swap only if it genuinely helps; the
        # attempt still leaves a trace either way.
        if after_swapped > before:
            new_hand, after = swapped, after_swapped
        else:
            new_hand, after = hand, before
        seat.private_state["cards"] = new_hand
        seat.private_state["strength"] = after
        seat.private_state["strength_band"] = _band_for(after)
        # trace climbs; repeated cheats compound (0.3 base, +0.15 jitter, additive)
        prior = float(seat.private_state.get("cheat_trace", 0.0))
        new_trace = round(min(1.0, prior + 0.3 + rng.random() * 0.15), 4)
        seat.private_state["cheat_trace"] = new_trace
        return CheatResult(strength_before=before, strength_after=after, new_trace=new_trace)

    def read(self, reader: TableSeat, target: TableSeat, *, reader_stat: int) -> ReadResult:
        """Return the target's REAL strength_band; flag a suspicious trace when
        it exceeds a read threshold scaled by the reader's relevant stat.
        """
        trace_val = float(target.private_state.get("cheat_trace", 0.0))
        # higher reader_stat → lower threshold → easier to notice a cheat
        read_threshold = max(0.1, 0.6 - 0.03 * reader_stat)
        info = {
            "target_seat": target.seat_id,
            "strength_band": target.private_state.get("strength_band", "unknown"),
            "suspicious_trace": trace_val >= read_threshold,
        }
        return ReadResult(target_seat=target.seat_id, info=info)


register_table_game(PokerTableGame())
