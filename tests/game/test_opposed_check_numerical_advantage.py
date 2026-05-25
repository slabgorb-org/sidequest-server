"""resolve_opposed_check numerical-advantage integration.

Verifies the side-aggregate modifier from numerical_advantage_for is
folded into the opposed-roll shift when an edge_resolver is provided.

The shift was previously:
    shift = (player_roll + player_mod) - (opponent_roll + opponent_mod)

With numerical advantage:
    shift = (player_roll + player_mod + player_num_adv)
          - (opponent_roll + opponent_mod + opponent_num_adv)

Without an edge_resolver, both numerical_advantage values default to 0
— back-compat for tests that don't care about side composition.
"""

from __future__ import annotations

from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.game.opposed_check import resolve_opposed_check
from sidequest.genre.models.rules import BeatDef


class _Cdef:
    """Minimal ConfrontationDef stand-in carrying opponent_default_stats."""

    def __init__(self, stats: dict[str, int]) -> None:
        self.opponent_default_stats = stats


def _beat(stat: str = "STR") -> BeatDef:
    return BeatDef.model_validate(
        {
            "id": "attack",
            "label": "attack",
            "kind": "strike",
            "base": 1,
            "stat_check": stat,
        }
    )


def _enc_with(actors: list[tuple[str, str]]) -> StructuredEncounter:
    encounter_actors = []
    for name, side in actors:
        encounter_actors.append(
            EncounterActor(
                name=name,
                role="combatant",
                side=side,
                per_actor_state={"stats": {"STR": 10}},  # mod 0
            )
        )
    return StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        actors=encounter_actors,
    )


def _core(name: str, *, current: int = 10, max_: int = 10) -> CreatureCore:
    return CreatureCore(
        name=name,
        description="x",
        personality="x",
        hp=HpPool(current=current, max=max_, base_max=max_),
    )


# ---------------------------------------------------------------------------
# Back-compat: omitting edge_resolver behaves exactly as before
# ---------------------------------------------------------------------------


def test_no_resolver_shift_matches_legacy_math():
    enc = _enc_with([("Hero", "player"), ("Foe", "opponent")])
    cdef = _Cdef({"STR": 10})
    result = resolve_opposed_check(
        player_actor=enc.find_actor("Hero"),
        opponent_actor=enc.find_actor("Foe"),
        player_beat=_beat(),
        opponent_beat=_beat(),
        cdef=cdef,
        player_roll=15,
        opponent_roll=10,
        encounter=enc,
    )
    # Both mods 0, no numerical advantage applied.
    assert result.shift == 5
    assert result.player_num_advantage == 0
    assert result.opponent_num_advantage == 0


# ---------------------------------------------------------------------------
# Numerical-advantage applied to shift
# ---------------------------------------------------------------------------


def test_three_pcs_versus_one_brute_player_side_gets_plus_one_to_shift():
    """Scenario A. PC initiator's side has 2 healthy allies → +1 shift."""
    enc = _enc_with(
        [
            ("Keith", "player"),
            ("James", "player"),
            ("Alex", "player"),
            ("Brute", "opponent"),
        ]
    )
    cdef = _Cdef({"STR": 10})
    cores = {a.name: _core(a.name) for a in enc.actors}

    result = resolve_opposed_check(
        player_actor=enc.find_actor("Keith"),
        opponent_actor=enc.find_actor("Brute"),
        player_beat=_beat(),
        opponent_beat=_beat(),
        cdef=cdef,
        player_roll=10,
        opponent_roll=10,
        encounter=enc,
        edge_resolver=cores.get,
    )
    # Player side: Keith excluded; James + Alex healthy → S=2.0 → raw=1.
    # Opponent side: zero allies → 0.
    # Shift = (10 + 0 + 1) - (10 + 0 + 0) = 1.
    assert result.player_num_advantage == 1
    assert result.opponent_num_advantage == 0
    assert result.shift == 1


def test_one_hero_versus_three_mooks_opponent_side_gets_plus_one_to_shift():
    """Scenario B. Mook initiator has 2 ally mooks → +1 to opponent side.

    From the player's POV (rolling against the lead mook), this means the
    Hero's effective shift is reduced by 1 — the swarm presses."""
    enc = _enc_with(
        [
            ("Hero", "player"),
            ("Mook0", "opponent"),
            ("Mook1", "opponent"),
            ("Mook2", "opponent"),
        ]
    )
    cdef = _Cdef({"STR": 10})
    cores = {a.name: _core(a.name) for a in enc.actors}

    result = resolve_opposed_check(
        player_actor=enc.find_actor("Hero"),
        opponent_actor=enc.find_actor("Mook0"),  # lead mook attacking
        player_beat=_beat(),
        opponent_beat=_beat(),
        cdef=cdef,
        player_roll=15,
        opponent_roll=10,
        encounter=enc,
        edge_resolver=cores.get,
    )
    # Hero side: 0 allies → 0.
    # Mook0 side: Mook1 + Mook2 healthy → S=2.0 → raw=1.
    # Shift = (15 + 0 + 0) - (10 + 0 + 1) = 4.
    assert result.player_num_advantage == 0
    assert result.opponent_num_advantage == 1
    assert result.shift == 4


def test_broken_swarm_inversion_helps_lone_fighter():
    """Hero stands over fallen mooks. 3 mooks broken (edge=0) on opponent
    side → opponent num_adv flips negative → Hero's shift improves."""
    enc = _enc_with(
        [
            ("Hero", "player"),
            ("Mook0", "opponent"),
            ("Mook1", "opponent"),
            ("Mook2", "opponent"),
            ("Mook3", "opponent"),
        ]
    )
    cdef = _Cdef({"STR": 10})
    cores = {
        "Hero": _core("Hero"),
        "Mook0": _core("Mook0"),  # initiator (still alive)
        "Mook1": _core("Mook1", current=0),  # broken
        "Mook2": _core("Mook2", current=0),  # broken
        "Mook3": _core("Mook3", current=0),  # broken
    }

    result = resolve_opposed_check(
        player_actor=enc.find_actor("Hero"),
        opponent_actor=enc.find_actor("Mook0"),
        player_beat=_beat(),
        opponent_beat=_beat(),
        cdef=cdef,
        player_roll=10,
        opponent_roll=10,
        encounter=enc,
        edge_resolver=cores.get,
    )
    # Mook0 side: 3 allies all broken (100% > 50%) → inversion.
    # raw = floor(0/2) = 0 → -max(0,1) = -1.
    assert result.opponent_num_advantage == -1
    # Shift = (10 + 0 + 0) - (10 + 0 + -1) = 1.
    assert result.shift == 1
