"""NPC seat auto-commit — a real basis, not narration whim.

A ruleset-module policy over (own strength_band, pot size, OCEAN/disposition)
returns a TableCommit. Confident/low-neuroticism → slow-play strong or bluff
weak; anxious → fold early; larcenous disposition → likelier to Cheat. Testable
in isolation, deterministic under a seeded rng. Emits table.npc_commit.

The policy is KIND-GENERAL: it maps abstract intents (drop-out / aggressive /
passive / bluff / cheat) onto authored beats from ``available_beats``. Only
beat_ids present in ``available_beats`` are ever returned, so an auction NPC
can never emit a poker-only beat such as "cheat" (which ``AuctionTableGame``
does not implement, causing a ``NotImplementedError``).
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


def _first_authored(prefs: tuple[str, ...], available: set[str], fallback: str) -> str:
    """Return the first preference present in ``available``, else ``fallback``.

    ``fallback`` MUST be a member of ``available`` (callers must guarantee this).
    This ensures the returned beat is always authored, which is the fundamental
    kind-safety invariant: the policy can NEVER emit a beat the confrontation
    did not declare.

    Content-author note: the ultimate ``next(iter(available))`` fallback some
    callers pass is order-nondeterministic across process restarts (set hash
    randomization). Any NEW table kind should author at least one of
    ("fold", "withdraw") so NPC drop-out resolves through the deterministic
    chain rather than that fallback. Latent today — poker and auction both
    author a drop-out beat — but worth keeping true for new kinds.
    """
    for p in prefs:
        if p in available:
            return p
    return fallback


def decide_npc_commit(
    state: TableState, npc: TableSeat, *, rng: random.Random, available_beats: set[str]
) -> TableCommit:
    """Choose an NPC commit beat, guaranteed to be ∈ ``available_beats``.

    ``available_beats`` is the set of beat ids authored by the confrontation
    definition (``{b.id for b in cdef.beats}``).  The policy maps abstract
    intents onto a beat the confrontation actually declared, so a larcenous
    NPC at an auction can never emit ``"cheat"`` when the auction hasn't
    authored it.

    Raises ``ValueError`` if ``available_beats`` is empty — that is a content
    error, not a recoverable state.  All other paths guarantee a return value
    ∈ ``available_beats``.
    """
    if not available_beats:
        raise ValueError(
            f"decide_npc_commit: available_beats is empty for seat {npc.seat_id!r} "
            f"— confrontation has no authored beats (content error)"
        )
    band = str(npc.private_state.get("strength_band", "weak"))
    rank = _BAND_RANK.get(band, 0)
    ocean = npc.private_state.get("ocean", {}) or {}
    neuroticism = float(ocean.get("neuroticism", 0.5))
    disposition = str(npc.private_state.get("disposition", "neutral"))
    pot = sum(state.pot.contributions.values())

    beat = _choose_beat(
        rank=rank,
        neuroticism=neuroticism,
        disposition=disposition,
        pot=pot,
        rng=rng,
        available=available_beats,
    )
    amount = _bet_amount(beat, rank=rank, rng=rng)
    target = None
    # Target-requiring beats: _choose_beat only ever returns an authored beat,
    # so membership in available_beats is already guaranteed here — no need to
    # re-check it. If no live opponent exists, fall back to a safe authored
    # drop-out beat (it never requires a target).
    if beat in ("read_table", "read_room", "accuse"):
        target = _pick_opponent(state, npc)
        if target is None:
            beat = _first_authored(
                ("fold", "withdraw"), available_beats, next(iter(available_beats))
            )
            amount = 0

    with table_npc_commit_span(seat=npc.seat_id, strength_band=band, pot=pot, chosen_beat=beat):
        pass
    return TableCommit(seat_id=npc.seat_id, beat_id=beat, amount=amount, target_seat=target)


def _choose_beat(
    *,
    rank: int,
    neuroticism: float,
    disposition: str,
    pot: int,
    rng: random.Random,
    available: set[str],
) -> str:
    """Map abstract intent to an authored beat id.

    All returned values are guaranteed ∈ ``available``.  The caller
    (``decide_npc_commit``) has already asserted that ``available`` is
    non-empty.

    Abstract intent → authored-beat fallback chains
    ────────────────────────────────────────────────
    drop-out  : ("fold", "withdraw")   — poker has fold; auction has withdraw
    aggressive: ("raise", "raise_bid", "bet")
    passive   : ("call", "raise_bid")  — auction has no call → middling → raise_bid
    bluff     : only if "bluff" ∈ available (both poker and auction author it)
    cheat     : only if "cheat" ∈ available; larcenous at auction → falls through
    read_table/read_room: only if authored
    """
    # Guaranteed-authored drop-out beat (used as ultimate fallback).
    drop_out = _first_authored(("fold", "withdraw"), available, next(iter(available)))

    # larcenous NPCs reach for the deck when their hand is mediocre —
    # but ONLY if "cheat" is an authored beat for this confrontation.
    # The nested if is deliberate (NOT collapsible with `and`): the outer guard
    # is the probability roll (consumes rng), the inner is authorship membership.
    # Collapsing would obscure the fall-through — when cheat ISN'T authored
    # (e.g. auction), the rng has already been spent and we must drop to the
    # normal hand-strength logic below, not skip the roll entirely.
    if disposition == "larcenous" and rank <= 2 and rng.random() < _P_CHEAT_LARCENOUS:  # noqa: SIM102
        if "cheat" in available:
            return "cheat"
        # cheat not authored (e.g. auction) → fall through to normal logic

    if rank == 0:
        # weak hand
        if neuroticism >= _N_FOLD_ANXIOUS:
            return drop_out  # anxious + weak → drop out
        if pot >= _POT_PRESSURE_CHIPS:
            return drop_out  # big pot + nothing → drop out under commitment pressure
        # weak but calm into a small pot → bluff sometimes (if authored), else drop out
        if rng.random() < _P_BLUFF_WEAK_CALM and "bluff" in available:
            return "bluff"
        return drop_out

    if rank >= 3:
        # strong + calm → slow-play or press
        # NOTE (V1): pot currently scales only the weak-hand fold decision above;
        # broader pot-scaling (e.g. pot-odds-driven calls here) is a deliberate
        # deferral to playtest tuning.
        aggressive = _first_authored(("raise", "raise_bid", "bet"), available, drop_out)
        if neuroticism < _N_CALM and rng.random() < _P_CALL_STRONG_CALM:
            # Slow-play: poker → call; auction has no call → minimal raise_bid
            passive = _first_authored(("call", "raise_bid"), available, drop_out)
            return passive
        return aggressive

    # middling hand: mostly stay-in, occasional raise
    aggressive = _first_authored(("raise", "raise_bid", "bet"), available, drop_out)
    passive = _first_authored(("call", "raise_bid"), available, drop_out)
    return aggressive if rng.random() < _P_RAISE_MIDDLING else passive


def _bet_amount(beat: str, *, rank: int, rng: random.Random) -> int:
    if beat in ("raise", "raise_bid", "bluff", "bet"):
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
