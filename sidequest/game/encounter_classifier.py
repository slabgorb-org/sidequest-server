"""Shared encounter-outcome classifier — Story 59-32.

Single source of truth for "does this ``StructuredEncounter.outcome`` count as a
player victory for reward/credit purposes?" Consolidates the credit-victory
mapping that was scattered/hardcoded across the resolution paths (59-31).

Each producer records its **mechanical-truth** label (``opponent_yielded``,
``surrender``, ``rout``, ``player_victory``, ...); the credit mapping lives here,
in one place, so a new outcome label is classified once rather than re-mapped at
every reward site.

**Fail-safe default (load-bearing).** Any unrecognized label classifies ``False``
— a future outcome nobody mapped must NOT silently grant victory credit. A
fabricated victory is the Illusionism / cry-wolf this epic fights (No Silent
Fallbacks). Matching is exact and case-sensitive: dynamic-prefix labels
(``resolution_beat:*``, ``composure_break:*``, ``table_winner:*``,
``resolved_by_trope:*``), substring near-misses (``victory``, ``player_victoryx``),
and case variants (``PLAYER_VICTORY``) all fall to ``False``.
"""

from __future__ import annotations

from typing import Literal

# The EXACT set of outcome labels that count as a player victory for credit.
# - player_victory  — dial-threshold / hp-depletion win
# - opponent_yielded — the opponent backed down (59-31 mechanical-truth label)
# - surrender / rout — opponent morale-break outcomes (narration_apply.py:540,546)
_PLAYER_VICTORY_OUTCOMES: frozenset[str] = frozenset(
    {
        "player_victory",
        "opponent_yielded",
        "surrender",
        "rout",
    }
)


def is_player_victory(outcome: str) -> bool:
    """Return ``True`` iff ``outcome`` is a credit-bearing player victory.

    Exact, case-sensitive membership against the canonical victory set; every
    other string — dynamic prefixes, near-misses, case variants, ``""``, and any
    unknown label — returns ``False`` (fail-safe default). Always a real
    ``bool`` (callers gate rewards on it).
    """
    return outcome in _PLAYER_VICTORY_OUTCOMES


# Outcome labels where an actor yielded, mapped to WHICH side yielded — Story 59-33.
# Orthogonal to is_player_victory: a PLAYER yield is a LOSS (side "player",
# is_player_victory False); the opponent-side yields are WINS (side "opponent").
_OPPONENT_YIELD_OUTCOMES: frozenset[str] = frozenset(
    {
        "opponent_yielded",
        "surrender",
        "rout",
    }
)


def yield_side_for(outcome: str) -> Literal["player", "opponent"] | None:
    """Return which side yielded for ``outcome``, or ``None`` if it is not a yield.

    - ``"yielded"`` → ``"player"`` (the player side yielded — a LOSS)
    - ``"opponent_yielded"`` / ``"surrender"`` / ``"rout"`` → ``"opponent"`` (a WIN)
    - everything else (dial wins, ``mutual_destruction``, dynamic-prefix labels,
      unknown, ``""``) → ``None``

    Exact, case-sensitive matching. This is a DIFFERENT axis from
    ``is_player_victory`` — it answers "which side yielded", not "did the player
    win" — so the two must not be implemented in terms of each other.
    """
    if outcome == "yielded":
        return "player"
    if outcome in _OPPONENT_YIELD_OUTCOMES:
        return "opponent"
    return None


__all__ = ["is_player_victory", "yield_side_for"]
