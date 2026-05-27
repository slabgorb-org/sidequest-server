"""Task 5 — apply_beat hp_depletion resolution branch + dial gating.

Verifies that for ``win_condition="hp_depletion"`` confrontations apply_beat
ends the encounter when a side's primary combatant reaches 0 HP (read via the
``edge_resolver``), and that the legacy dial-threshold resolution branches are
gated OFF for hp_depletion while remaining intact for dial_threshold packs.
"""

from __future__ import annotations

from sidequest.game.beat_kinds import apply_beat
from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.protocol.dice import RollOutcome


def _enc(win_condition: str) -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="combat",
        win_condition=win_condition,
        player_metric=EncounterMetric(name="hp", current=0, starting=0, threshold=1_000_000),
        opponent_metric=EncounterMetric(name="hp", current=0, starting=0, threshold=1_000_000),
        actors=[
            EncounterActor(name="Hero", role="player", side="player"),
            EncounterActor(name="Pirate", role="opponent", side="opponent"),
        ],
    )


def _cores(pirate_hp: int, hero_hp: int = 10):
    cores = {
        "Hero": CreatureCore(
            name="Hero",
            description="a hero",
            personality="brave",
            hp=HpPool(current=hero_hp, max=10, base_max=10),
        ),
        "Pirate": CreatureCore(
            name="Pirate",
            description="a pirate",
            personality="greedy",
            hp=HpPool(current=pirate_hp, max=10, base_max=10),
        ),
    }
    return lambda name: cores.get(name)


class _StrikeBeat:
    id = "shoot"
    kind = "strike"
    stat_check = "Physique"
    damage_channel = "strike"


def test_hp_depletion_resolves_player_victory_when_opponent_drops():
    enc = _enc("hp_depletion")
    result = apply_beat(
        enc,
        enc.actors[0],
        _StrikeBeat(),
        RollOutcome.Success,
        turn=1,
        edge_resolver=_cores(pirate_hp=0),
        damage_resolver=lambda: 0,
    )
    assert enc.resolved is True
    assert enc.outcome == "player_victory"
    assert result.resolved is True


def test_hp_depletion_resolves_opponent_victory_when_player_drops():
    enc = _enc("hp_depletion")
    apply_beat(
        enc,
        enc.actors[0],
        _StrikeBeat(),
        RollOutcome.Success,
        turn=1,
        edge_resolver=_cores(pirate_hp=10, hero_hp=0),
        damage_resolver=lambda: 0,
    )
    assert enc.resolved is True
    assert enc.outcome == "opponent_victory"


def test_hp_depletion_does_not_resolve_while_both_alive():
    enc = _enc("hp_depletion")
    apply_beat(
        enc,
        enc.actors[0],
        _StrikeBeat(),
        RollOutcome.Success,
        turn=1,
        edge_resolver=_cores(pirate_hp=4),
        damage_resolver=lambda: 3,
    )
    assert enc.resolved is False


def test_hp_depletion_ignores_dial_threshold():
    # Even if a metric were at threshold, hp_depletion must NOT resolve on the dial.
    enc = _enc("hp_depletion")
    enc.player_metric.current = enc.player_metric.threshold
    apply_beat(
        enc,
        enc.actors[0],
        _StrikeBeat(),
        RollOutcome.Success,
        turn=1,
        edge_resolver=_cores(pirate_hp=5),
        damage_resolver=lambda: 1,
    )
    assert enc.resolved is False


def test_dial_threshold_still_resolves_for_non_hp_packs():
    enc = _enc("dial_threshold")
    enc.player_metric.threshold = 2
    enc.player_metric.current = 2
    apply_beat(
        enc,
        enc.actors[0],
        _StrikeBeat(),
        RollOutcome.Success,
        turn=1,
        edge_resolver=_cores(pirate_hp=10),
        damage_resolver=lambda: 0,
    )
    assert enc.resolved is True
    assert enc.outcome == "player_victory"
