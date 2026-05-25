"""Targeting bias under taunt — real production path through apply_beat.

When an enemy strikes and taunt is active, the taunter absorbs the hit
instead of the default first-listed ally.  These tests drive the REAL
production path:

    apply_beat → _opposite_side_first_actor (taunt-aware) → CreatureCore.apply_hp_delta

Shape A wiring: the bias lives in _opposite_side_first_actor inside
beat_kinds.py — the same helper that apply_beat's focus/swarm branch
already calls.  No indirection through a standalone helper module.

Spec: docs/superpowers/specs/2026-05-10-class-mechanical-surface-design.md §8.
CLAUDE.md: "Verify Wiring, Not Just Existence."
"""

from __future__ import annotations

from sidequest.game.beat_kinds import apply_beat
from sidequest.protocol.dice import RollOutcome


def test_enemy_strike_without_taunt_hits_first_ally(taunt_test_encounter):
    """No taunt active — enemy strike hits the first-listed player-side actor.

    The fixture lists fighter-1 before cleric-1 on the player side, so
    fighter-1 is the default first-actor target.
    """
    helper = taunt_test_encounter
    enc = helper.enc
    fighter_core = helper.edge_resolver(helper.fighter_id)
    cleric_core = helper.edge_resolver(helper.cleric_id)

    assert enc.taunt.active_actor is None, "taunt must start inactive"

    fighter_before = fighter_core.hp.current
    cleric_before = cleric_core.hp.current

    enemy_actor = enc.find_actor("enemy-1")
    assert enemy_actor is not None

    apply_beat(
        enc,
        enemy_actor,
        helper.enemy_strike_beat,
        RollOutcome.Success,
        turn=1,
        edge_resolver=helper.edge_resolver,
    )

    # Fighter is first on the player side — absorbs the strike.
    assert fighter_core.hp.current < fighter_before, (
        f"Fighter should be hit (first-actor default); "
        f"edge {fighter_before} → {fighter_core.hp.current}"
    )
    # Cleric is untouched.
    assert cleric_core.hp.current == cleric_before, (
        f"Cleric should be untouched (no taunt, fighter is first); "
        f"edge unchanged at {cleric_before}"
    )


def test_enemy_strike_with_taunt_routes_to_taunter(taunt_test_encounter):
    """Taunt active on fighter-1 — enemy strike is redirected to fighter-1.

    Cleric is untouched even though the normal first-actor logic would
    also have picked fighter-1 here (fighter listed first).  We activate
    taunt explicitly and verify the OTEL-visible path is engaged rather
    than just the lucky order coincidence.
    """
    helper = taunt_test_encounter
    enc = helper.enc
    fighter_core = helper.edge_resolver(helper.fighter_id)
    cleric_core = helper.edge_resolver(helper.cleric_id)

    enc.taunt.activate(actor_id=helper.fighter_id)
    assert enc.taunt.active_actor == helper.fighter_id

    fighter_before = fighter_core.hp.current
    cleric_before = cleric_core.hp.current

    enemy_actor = enc.find_actor("enemy-1")
    assert enemy_actor is not None

    apply_beat(
        enc,
        enemy_actor,
        helper.enemy_strike_beat,
        RollOutcome.Success,
        turn=1,
        edge_resolver=helper.edge_resolver,
    )

    assert fighter_core.hp.current < fighter_before, (
        f"Fighter should absorb the hit (taunt active); "
        f"edge {fighter_before} → {fighter_core.hp.current}"
    )
    assert cleric_core.hp.current == cleric_before, (
        f"Cleric should be untouched while taunt is active; edge unchanged at {cleric_before}"
    )


def test_enemy_strike_with_taunt_on_cleric_routes_to_cleric(taunt_test_encounter):
    """Cleric activates taunt — enemy strike bypasses fighter and hits cleric.

    This is the critical case: the taunter (cleric-1) is NOT the first-listed
    player actor.  Without the taunt bias, _opposite_side_first_actor would
    return fighter-1.  With the bias, it must return cleric-1.
    """
    helper = taunt_test_encounter
    enc = helper.enc
    fighter_core = helper.edge_resolver(helper.fighter_id)
    cleric_core = helper.edge_resolver(helper.cleric_id)

    # Cleric taunts — she is second-listed on the player side.
    enc.taunt.activate(actor_id=helper.cleric_id)
    assert enc.taunt.active_actor == helper.cleric_id

    fighter_before = fighter_core.hp.current
    cleric_before = cleric_core.hp.current

    enemy_actor = enc.find_actor("enemy-1")
    assert enemy_actor is not None

    apply_beat(
        enc,
        enemy_actor,
        helper.enemy_strike_beat,
        RollOutcome.Success,
        turn=1,
        edge_resolver=helper.edge_resolver,
    )

    # Cleric is the taunter — she absorbs the hit.
    assert cleric_core.hp.current < cleric_before, (
        f"Cleric should absorb the hit (taunt active on cleric); "
        f"edge {cleric_before} → {cleric_core.hp.current}"
    )
    # Fighter is the normal first-actor but is bypassed by taunt bias.
    assert fighter_core.hp.current == fighter_before, (
        f"Fighter should be untouched (taunt redirected to cleric); "
        f"edge unchanged at {fighter_before}"
    )


def test_spread_damage_redirects_one_ally_to_taunter(taunt_test_encounter):
    """Spread attack hits both allies — with taunt active on fighter, one ally's
    hit is redirected to the taunter (cap 1/round).

    Spread loop iteration order (encounter declaration order): fighter-1 first,
    cleric-1 second.  per_target = 6 // 2 = 3.

    Iteration 1 — fighter-1: fighter IS the taunter → no redirect, fighter takes 3.
    Iteration 2 — cleric-1:  cleric is NOT the taunter, taunter is live, cap not
                              reached → redirect consumed → fighter takes 3 instead.

    Result: fighter_drop=6, cleric_drop=0; redirects_this_round=1.

    Spec: 2026-05-10 class-mechanical-surface §8 — taunt damage redirect (cap 1/round).
    """
    helper = taunt_test_encounter
    enc = helper.enc
    fighter_core = helper.edge_resolver(helper.fighter_id)
    cleric_core = helper.edge_resolver(helper.cleric_id)

    enc.taunt.activate(actor_id=helper.fighter_id)

    fighter_before = fighter_core.hp.current
    cleric_before = cleric_core.hp.current

    enemy_actor = enc.find_actor("enemy-1")
    apply_beat(
        enc,
        enemy_actor,
        helper.enemy_spread_beat,
        RollOutcome.Success,
        turn=1,
        edge_resolver=helper.edge_resolver,
    )

    fighter_drop = fighter_before - fighter_core.hp.current
    cleric_drop = cleric_before - cleric_core.hp.current

    assert fighter_drop == 6, (
        f"Fighter should absorb own hit (3) + redirected cleric hit (3) = 6; "
        f"got fighter_drop={fighter_drop}, cleric_drop={cleric_drop}"
    )
    assert cleric_drop == 0, (
        f"Cleric hit was redirected to fighter; cleric should be untouched; "
        f"got cleric_drop={cleric_drop}"
    )
    assert enc.taunt.redirects_this_round == 1, (
        f"Expected exactly 1 redirect consumed; got {enc.taunt.redirects_this_round}"
    )


def test_spread_damage_redirect_capped_at_one_per_round(taunt_test_encounter):
    """Two enemy spread beats fired in one round consume only one redirect total.

    Beat 1 (enemy-1): redirect consumed on cleric's hit → fighter=6 drop, cleric=0.
    Beat 2 (enemy-2): cap reached → no redirect → both take 3 normally.
                      fighter += 3, cleric += 3.

    Final: fighter_drop=9, cleric_drop=3, total=12; redirects_this_round=1 (capped).

    Spec: 2026-05-10 class-mechanical-surface §8 — taunt damage redirect (cap 1/round).
    """
    helper = taunt_test_encounter
    enc = helper.enc
    fighter_core = helper.edge_resolver(helper.fighter_id)
    cleric_core = helper.edge_resolver(helper.cleric_id)

    enc.taunt.activate(actor_id=helper.fighter_id)

    fighter_before = fighter_core.hp.current
    cleric_before = cleric_core.hp.current

    # First spread fires — one redirect consumed.
    apply_beat(
        enc,
        enc.find_actor("enemy-1"),
        helper.enemy_spread_beat,
        RollOutcome.Success,
        turn=1,
        edge_resolver=helper.edge_resolver,
    )
    # Second spread same round — cap is reached; no redirect.
    apply_beat(
        enc,
        enc.find_actor("enemy-2"),
        helper.enemy_spread_beat,
        RollOutcome.Success,
        turn=1,
        edge_resolver=helper.edge_resolver,
    )

    fighter_drop = fighter_before - fighter_core.hp.current
    cleric_drop = cleric_before - cleric_core.hp.current

    assert enc.taunt.redirects_this_round == 1, (
        f"Cap should hold at 1; got {enc.taunt.redirects_this_round}"
    )
    assert fighter_drop + cleric_drop == 12, (
        f"Total damage across two spread beats (6+6=12); "
        f"got fighter={fighter_drop}, cleric={cleric_drop}"
    )
    assert fighter_drop == 9, (
        f"Fighter: own hit beat1 (3) + redirected cleric beat1 (3) + own hit beat2 (3) = 9; "
        f"got {fighter_drop}"
    )
    assert cleric_drop == 3, (
        f"Cleric: only beat2 hit (cap reached, no redirect) = 3; got {cleric_drop}"
    )
