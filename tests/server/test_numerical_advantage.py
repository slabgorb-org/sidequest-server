"""numerical_advantage shift modifier (Step 3 of numerical-advantage design).

Pure-function math:
    raw = floor(sum(ally_edge_fractions) / 2)
    cap at +3
    if more than half of allies are broken (edge_fraction <= 0): flip sign
        and bump magnitude to at least 1 (broken sides never net to zero
        when the inversion fires — the swarm has visibly collapsed and
        the lone fighter gets a second wind).

Engaged-ally definition (initiator EXCLUDED):
    same side as initiator AND not withdrawn

Withdrawn allies count as edge_fraction = 0.0 (they're still bodies in
the room contributing nothing to the swarm).

Tuning rationale: the d20 shift bands (ADR-093) place Tie at [-1,+1]
and Success at >=+2. A +1 numerical-advantage modifier alone is tier-
neutral; +2 flips coin-flips into wins. The /2 divisor places the
playgroup's load-bearing scenarios at sensible thresholds:

    1 Hero vs 3 mooks:  each mook gets +1 (S=2 → raw=1)   — pressure
    3 PCs vs 1 brute:   each PC gets +1   (S=2 → raw=1)   — dogpile
    2 PCs vs 3 mooks:   PCs +0, mooks +1                  — outnumbered
    1 Hero vs 5 mooks:  each mook gets +2 (S=4 → raw=2)   — overwhelmed
"""

from __future__ import annotations

from sidequest.game.beat_kinds import (
    numerical_advantage_for,
    numerical_advantage_modifier,
)
from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
)

# ---------------------------------------------------------------------------
# Pure-function math
# ---------------------------------------------------------------------------


def test_no_allies_returns_zero():
    assert numerical_advantage_modifier([]) == 0


def test_single_full_health_ally_below_threshold():
    """One full-health ally → S=1.0 → raw = floor(1/2) = 0."""
    assert numerical_advantage_modifier([1.0]) == 0


def test_two_full_health_allies_yields_plus_one():
    """Two full-health allies → S=2.0 → raw = 1. Load-bearing playgroup case."""
    assert numerical_advantage_modifier([1.0, 1.0]) == 1


def test_four_full_health_allies_yields_plus_two():
    """Four full-health allies → S=4.0 → raw = 2 (flips Tie to Success on roll)."""
    assert numerical_advantage_modifier([1.0] * 4) == 2


def test_six_full_health_allies_yields_plus_three():
    assert numerical_advantage_modifier([1.0] * 6) == 3


def test_modifier_capped_at_three():
    """Cap at +3 even with massive ally counts."""
    assert numerical_advantage_modifier([1.0] * 50) == 3


def test_partial_ally_health_contributes_fractionally():
    """Two half-health allies → S=1.0 → raw = 0."""
    assert numerical_advantage_modifier([0.5, 0.5]) == 0


def test_four_half_health_allies_yields_plus_one():
    """Four half-health allies → S=2.0 → raw = 1."""
    assert numerical_advantage_modifier([0.5] * 4) == 1


# ---------------------------------------------------------------------------
# Broken-side inversion
# ---------------------------------------------------------------------------


def test_majority_broken_flips_to_negative():
    """3 of 4 allies broken (75% > 50%) → swarm collapses → negative mod."""
    fractions = [0.0, 0.0, 0.0, 1.0]
    result = numerical_advantage_modifier(fractions)
    assert result < 0


def test_majority_broken_minimum_magnitude_is_one():
    """Even when raw modifier is 0, broken-side inversion guarantees |mod| >= 1.
    A collapsing side is never neutral — the lone fighter feels the shift."""
    fractions = [0.0, 0.0]  # all broken, raw = 0
    result = numerical_advantage_modifier(fractions)
    assert result == -1


def test_exactly_half_broken_does_not_flip():
    """50% broken is not a majority — modifier stays positive."""
    fractions = [0.0, 0.0, 1.0, 1.0]  # raw = floor(2/2) = 1, exactly half broken
    result = numerical_advantage_modifier(fractions)
    assert result == 1  # no inversion


def test_minority_broken_no_inversion():
    """Less than half broken → no inversion, normal positive modifier."""
    fractions = [0.0, 1.0, 1.0, 1.0, 1.0, 1.0]  # 1 of 6 broken
    result = numerical_advantage_modifier(fractions)
    assert result == 2  # floor(5.0 / 2) = 2, no flip


# ---------------------------------------------------------------------------
# numerical_advantage_for (encounter helper) — playgroup scenarios
# ---------------------------------------------------------------------------


def _enc(actors: list[tuple[str, str]]) -> StructuredEncounter:
    """Build encounter from (name, side) tuples."""
    return StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        actors=[EncounterActor(name=n, role="combatant", side=s) for n, s in actors],
    )


def _core(name: str, *, current: int = 10, max_: int = 10) -> CreatureCore:
    return CreatureCore(
        name=name,
        description="x",
        personality="x",
        hp=HpPool(current=current, max=max_, base_max=max_),
    )


def test_scenario_a_three_pcs_versus_one_brute():
    """3 PCs + 1 Brute. Each PC has 2 allies → +1. Brute has 0 → 0."""
    enc = _enc(
        [
            ("Keith", "player"),
            ("James", "player"),
            ("Alex", "player"),
            ("Brute", "opponent"),
        ]
    )
    cores = {a.name: _core(a.name) for a in enc.actors}
    assert numerical_advantage_for(enc.find_actor("Keith"), enc, cores.get) == 1
    assert numerical_advantage_for(enc.find_actor("Brute"), enc, cores.get) == 0


def test_scenario_b_one_hero_versus_three_mooks():
    """1 Hero + 3 Mooks. Each mook has 2 allies → +1. Hero has 0 → 0.

    Combined with action economy (3 mook beats per round vs 1 hero),
    this delivers the "outnumbered" feel the playgroup wanted."""
    enc = _enc(
        [
            ("Hero", "player"),
            ("Mook0", "opponent"),
            ("Mook1", "opponent"),
            ("Mook2", "opponent"),
        ]
    )
    cores = {a.name: _core(a.name) for a in enc.actors}
    assert numerical_advantage_for(enc.find_actor("Hero"), enc, cores.get) == 0
    assert numerical_advantage_for(enc.find_actor("Mook0"), enc, cores.get) == 1


def test_scenario_c_two_pcs_versus_three_mooks():
    """2 PCs + 3 Mooks. PCs have 1 ally each → 0. Mooks have 2 allies → +1."""
    enc = _enc(
        [
            ("PC0", "player"),
            ("PC1", "player"),
            ("Mook0", "opponent"),
            ("Mook1", "opponent"),
            ("Mook2", "opponent"),
        ]
    )
    cores = {a.name: _core(a.name) for a in enc.actors}
    assert numerical_advantage_for(enc.find_actor("PC0"), enc, cores.get) == 0
    assert numerical_advantage_for(enc.find_actor("Mook0"), enc, cores.get) == 1


def test_one_hero_versus_five_mooks_mook_side_gets_plus_two():
    """5 mooks → each has 4 allies → +2 (flips Tie to Success on average roll)."""
    enc = _enc(
        [
            ("Hero", "player"),
            ("Mook0", "opponent"),
            ("Mook1", "opponent"),
            ("Mook2", "opponent"),
            ("Mook3", "opponent"),
            ("Mook4", "opponent"),
        ]
    )
    cores = {a.name: _core(a.name) for a in enc.actors}
    assert numerical_advantage_for(enc.find_actor("Mook0"), enc, cores.get) == 2


def test_withdrawn_ally_counts_as_broken_for_inversion():
    """Withdrawn allies count toward the broken majority."""
    enc = _enc(
        [
            ("Hero", "player"),
            ("Mook0", "opponent"),
            ("Mook1", "opponent"),
            ("Mook2", "opponent"),
            ("Mook3", "opponent"),
        ]
    )
    enc.actors[1].withdrawn = True
    enc.actors[2].withdrawn = True
    enc.actors[3].withdrawn = True
    cores = {a.name: _core(a.name) for a in enc.actors}
    # From Mook3's POV: 3 allies, all withdrawn (=0.0). 100% broken → inversion.
    # raw = floor(0/2) = 0 → -max(0,1) = -1.
    assert numerical_advantage_for(enc.find_actor("Mook3"), enc, cores.get) == -1


def test_initiator_excluded_from_own_count():
    """The initiator's edge does NOT contribute to their side's modifier
    (the rule measures *allies*, not the initiator themselves)."""
    enc = _enc([("A", "player"), ("B", "player"), ("Foe", "opponent")])
    cores = {
        "A": _core("A", current=1),
        "B": _core("B"),
        "Foe": _core("Foe"),
    }
    # A's side: 1 ally (B at 1.0) → S=1.0 → raw=0.
    assert numerical_advantage_for(enc.find_actor("A"), enc, cores.get) == 0
    # B's side: 1 ally (A at 0.1) → S=0.1 → raw=0.
    assert numerical_advantage_for(enc.find_actor("B"), enc, cores.get) == 0
