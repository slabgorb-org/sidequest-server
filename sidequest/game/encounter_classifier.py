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


__all__ = ["is_player_victory"]
