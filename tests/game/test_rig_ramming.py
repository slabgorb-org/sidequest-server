"""Story 86-2 (AC6): Ramming — opposed Dex/Drive, max-HP damage + Trauma.

CWN §2.4.8.4 (design doc §4.1):

    Ramming is an opposed Dex/Drive vs Dex/Drive check. On a win the
    target takes the *ramming vehicle's max HP* in damage, with Trauma
    1d12 / ×3; the ramming vehicle also takes damage as if rammed back
    (mutual damage).

This is the stateless opposed-resolution core: the server rolls each
side's d20 + Dex mod + Drive elsewhere (matching the OpponentAttackOutcome
"caller supplies the d20" pattern) and passes the totals in. The helper
decides the winner and the damage figures. RED until Dev adds
``resolve_ramming`` to ``sidequest/game/vehicle_combat.py``.

Proposed seam (TEA contract, open to refinement):
    resolve_ramming(*, attacker_total, defender_total,
                    attacker_max_hp, defender_max_hp) -> RammingResult
      .attacker_wins: bool          # higher opposed total wins; tie ≠ win
      .defender_damage: int         # ramming vehicle's max HP on a win
      .attacker_damage: int         # mutual: rammer takes damage back (>0 on win)
      .delivers_trauma: bool        # the ram carries Trauma 1d12/×3 on a win
      .trauma_die: str              # "1d12"  (the carried trauma spec)
      .trauma_rating: int           # 3       (×3 multiplier)
"""

from __future__ import annotations

from sidequest.game.vehicle_combat import resolve_ramming


def test_higher_opposed_total_wins_the_ram() -> None:
    """The side with the higher Dex/Drive total wins; on a win the target
    takes the ramming vehicle's max HP in damage."""
    r = resolve_ramming(
        attacker_total=18,
        defender_total=12,
        attacker_max_hp=9,
        defender_max_hp=6,
    )
    assert r.attacker_wins is True
    assert r.defender_damage == 9  # ramming vehicle's max HP


def test_ram_is_mutual_attacker_also_takes_damage() -> None:
    """'The ramming vehicle also takes damage as if rammed back' — a
    winning ram must leave the attacker with non-zero damage too."""
    r = resolve_ramming(
        attacker_total=18,
        defender_total=12,
        attacker_max_hp=9,
        defender_max_hp=6,
    )
    assert r.attacker_damage > 0


def test_winning_ram_delivers_trauma_1d12_x3() -> None:
    """A winning ram carries Trauma 1d12 / ×3 onto the target (the actual
    trauma roll is resolved downstream by the CWN trauma pipeline; the
    ram only *carries* the spec)."""
    r = resolve_ramming(
        attacker_total=18,
        defender_total=12,
        attacker_max_hp=9,
        defender_max_hp=6,
    )
    assert r.delivers_trauma is True
    assert r.trauma_die == "1d12"
    assert r.trauma_rating == 3


def test_losing_ram_deals_no_ram_damage() -> None:
    """If the attacker's opposed total is lower, the ram fails — no max-HP
    hit, no trauma. (The defender evaded/out-drove the ram.)"""
    r = resolve_ramming(
        attacker_total=10,
        defender_total=15,
        attacker_max_hp=9,
        defender_max_hp=6,
    )
    assert r.attacker_wins is False
    assert r.defender_damage == 0
    assert r.delivers_trauma is False


def test_tie_is_not_a_win() -> None:
    """A tie does not connect the ram — the attacker must beat the
    defender's opposed total, not merely match it."""
    r = resolve_ramming(
        attacker_total=14,
        defender_total=14,
        attacker_max_hp=9,
        defender_max_hp=6,
    )
    assert r.attacker_wins is False
    assert r.defender_damage == 0
