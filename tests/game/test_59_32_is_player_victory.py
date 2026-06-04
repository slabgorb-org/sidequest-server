"""RED tests — Story 59-32: shared ``is_player_victory(outcome)`` classifier.

Single source of truth for "does this ``enc.outcome`` count as a player victory
for reward/credit purposes?" Consolidates the credit-victory mapping that was
scattered/hardcoded across the resolution paths (59-31).

Contract (Keith's reframe ruling, relayed via SM — credit-victory SoT):
  TRUE  set: player_victory, opponent_yielded, surrender, rout
  FALSE set: everything else — opponent_victory, yielded,
             abandoned_on_location_change, mutual_destruction,
             dynamic ``resolution_beat:*``, dynamic ``composure_break:*``,
             and any unknown/unrecognized string (fail-safe default).

The TRUE set is grounded in the live-outcome inventory (TEA prep, session
Delivery Findings): ``surrender``/``rout`` are opponent morale-break outcomes
(``narration_apply.py:540,546``) — opponent yields = player victories — and were
MISSING from the original AC's 2-label set. ``opponent_yielded`` is the
narrative-precise opponent-yield label (``narration_apply.py:4560``).

Fail-safe default (unrecognized → False) is load-bearing: a future outcome label
nobody mapped must NOT silently grant victory credit (No Silent Fallbacks — a
fabricated victory is the cry-wolf this epic fights).

Import home: the Architect/AC leaves it open ("StructuredEncounter or
game/encounter_classifier.py"). The shim prefers the dedicated decomposition
module and falls back to the encounter module — both fail RED until Dev creates
the classifier.
"""

from __future__ import annotations

from typing import Any

import pytest

_TRUE_OUTCOMES = [
    "player_victory",
    "opponent_yielded",
    "surrender",
    "rout",
]

_FALSE_OUTCOMES = [
    # documented non-victories
    "opponent_victory",
    "yielded",  # player-side loss
    "abandoned_on_location_change",
    "mutual_destruction",
    # dynamic-prefix outcomes — NOT victories (fail-safe; none is, by itself, a
    # credit-victory label). resolution_beat:* is the fallthrough elif at
    # beat_kinds.py:1054 (fires only when no victory condition resolved first —
    # never a missed win); table_winner:*/resolved_by_trope:* per the White
    # Queen's final verified ruling.
    "resolution_beat:press",
    "resolution_beat:closing_argument",
    "composure_break:rattled",
    "table_winner:Dorothy",
    "resolved_by_trope:the_cavalry_arrives",
    # unknown / fail-safe-default cases
    "",
    "victory",  # substring trap — must NOT match
    "win",
    "PLAYER_VICTORY",  # case-sensitive — must NOT match
    "player_victoryx",  # near-miss — must NOT match
    "banana",
]


def _is_player_victory() -> Any:
    """Resolve the classifier from its (yet-to-exist) canonical module.

    Prefers ``sidequest.game.encounter_classifier`` (the project decomposition
    pattern named first in the technical approach); falls back to
    ``sidequest.game.encounter`` (the StructuredEncounter module). Both raise in
    RED — the classifier does not exist yet."""
    try:
        from sidequest.game.encounter_classifier import is_player_victory

        return is_player_victory
    except ImportError:
        from sidequest.game.encounter import is_player_victory

        return is_player_victory


@pytest.mark.parametrize("outcome", _TRUE_OUTCOMES)
def test_is_player_victory_true_outcomes(outcome: str) -> None:
    """Every credit-victory label classifies True."""
    is_player_victory = _is_player_victory()
    assert is_player_victory(outcome) is True, (
        f"{outcome!r} is a credit-victory outcome and must classify True"
    )


@pytest.mark.parametrize("outcome", _FALSE_OUTCOMES)
def test_is_player_victory_false_outcomes(outcome: str) -> None:
    """Every non-victory, dynamic-prefix, and unknown label classifies False
    (fail-safe default)."""
    is_player_victory = _is_player_victory()
    assert is_player_victory(outcome) is False, (
        f"{outcome!r} is NOT a credit-victory outcome and must classify False "
        "(fail-safe default for unrecognized labels)"
    )


def test_is_player_victory_returns_bool_not_truthy() -> None:
    """Returns a real ``bool`` (True/False), not a truthy/falsy proxy — callers
    gate rewards on it, so the type contract matters."""
    is_player_victory = _is_player_victory()
    assert is_player_victory("player_victory") is True
    assert is_player_victory("opponent_victory") is False
    assert isinstance(is_player_victory("anything"), bool)
