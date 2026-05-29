"""Auction table-game kind — proves the model is the general free-for-all.

Zero TableState changes: same seats + pot + order. private_state holds a secret
valuation / max_bid (no cards). Beats: raise_bid / bluff / read_room / withdraw.
pot.contributions is the current high bid; strength() = the seat's standing bid
(mirrored into _current_bid by the engine pot branch). Cheat/Accuse are NOT
registered for auction (optional content — a rigged auction could add them).
"""

from __future__ import annotations

import random

from sidequest.game.table.registry import TableGame, register_table_game
from sidequest.game.table.types import ReadResult, TablePot, TableSeat

_BANDS = ("weak", "marginal", "decent", "strong", "monster")


def _band_for_valuation(valuation: int, ceiling: int) -> str:
    frac = valuation / ceiling
    idx = min(len(_BANDS) - 1, int(frac * len(_BANDS)))
    return _BANDS[idx]


class AuctionTableGame(TableGame):
    kind = "auction"
    _CEILING = 100

    def deal(self, seats: list[TableSeat], pot: TablePot, rng: random.Random) -> None:
        for seat in seats:
            valuation = rng.randint(10, self._CEILING)
            seat.private_state["valuation"] = valuation
            seat.private_state["max_bid"] = valuation  # won't bid above own valuation
            seat.private_state["strength_band"] = _band_for_valuation(valuation, self._CEILING)
            seat.private_state["_current_bid"] = 0

    def strength(self, seat: TableSeat) -> int:
        # showdown: highest standing bid (≤ own max_bid) wins
        bid = int(seat.private_state.get("_current_bid", 0))
        max_bid = int(seat.private_state.get("max_bid", 0))
        return bid if bid <= max_bid else -1  # an over-max bid is invalid → loses

    def read(self, reader: TableSeat, target: TableSeat, *, reader_stat: int) -> ReadResult:
        info = {
            "target_seat": target.seat_id,
            "strength_band": target.private_state.get("strength_band", "unknown"),
        }
        return ReadResult(target_seat=target.seat_id, info=info)


register_table_game(AuctionTableGame())
