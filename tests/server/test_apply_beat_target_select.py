"""target_select wiring on BeatDef (Step 2 of numerical-advantage design).

Adds a per-beat target-resolution mode that controls how
``target_edge_delta`` is distributed across the opposing side:

- ``focus`` (default): the single first live opposing actor takes the
  full debit. Same as Step 1.
- ``spread``: the debit is divided ``floor(N)`` across every live
  opposing actor; remainder is dropped (genre-truthful AOE attenuation).
- ``swarm``: focus mode, but the OTEL span carries a ``target_select``
  attribute so Step 3's numerical-advantage rule can detect a beat that
  wants ally amplification.

These tests do NOT exercise numerical advantage; they only verify the
distribution logic.
"""

from __future__ import annotations

from sidequest.game.beat_kinds import apply_beat
from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.genre.models.rules import BeatDef
from sidequest.protocol.dice import RollOutcome


def _enc_with_opposing(n_opponents: int) -> StructuredEncounter:
    actors = [EncounterActor(name="Sam", role="combatant", side="player")]
    for i in range(n_opponents):
        actors.append(EncounterActor(name=f"Mook{i}", role="combatant", side="opponent"))
    return StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        actors=actors,
    )


def _core(name: str, *, current: int = 10, max_: int = 10) -> CreatureCore:
    return CreatureCore(
        name=name,
        description="x",
        personality="x",
        hp=HpPool(current=current, max=max_, base_max=max_),
    )


def _strike(*, target_edge_delta: int, target_select: str | None = None) -> BeatDef:
    payload: dict = {
        "id": "attack",
        "label": "attack",
        "kind": "strike",
        "base": 1,
        "stat_check": "STR",
        "target_edge_delta": target_edge_delta,
    }
    if target_select is not None:
        payload["target_select"] = target_select
    return BeatDef.model_validate(payload)


# ---------------------------------------------------------------------------
# Default mode = focus (back-compat with Step 1)
# ---------------------------------------------------------------------------


def test_default_target_select_is_focus_single_target():
    """Beats that omit target_select fall back to focus mode (Step 1 behavior)."""
    enc = _enc_with_opposing(3)
    sam = enc.find_actor("Sam")
    cores = {a.name: _core(a.name) for a in enc.actors}

    apply_beat(
        enc,
        sam,
        _strike(target_edge_delta=3),
        RollOutcome.Success,
        edge_resolver=cores.get,
    )

    assert cores["Mook0"].hp.current == 7  # full debit on first opponent
    assert cores["Mook1"].hp.current == 10  # untouched
    assert cores["Mook2"].hp.current == 10  # untouched


# ---------------------------------------------------------------------------
# Spread mode
# ---------------------------------------------------------------------------


def test_spread_divides_debit_across_all_live_opponents():
    """target_edge_delta=6 spread across 3 opponents → 2 each."""
    enc = _enc_with_opposing(3)
    sam = enc.find_actor("Sam")
    cores = {a.name: _core(a.name) for a in enc.actors}

    apply_beat(
        enc,
        sam,
        _strike(target_edge_delta=6, target_select="spread"),
        RollOutcome.Success,
        edge_resolver=cores.get,
    )

    assert cores["Mook0"].hp.current == 8
    assert cores["Mook1"].hp.current == 8
    assert cores["Mook2"].hp.current == 8


def test_spread_floor_divides_remainder_dropped():
    """target_edge_delta=7 across 3 opponents → 2 each (remainder absorbed)."""
    enc = _enc_with_opposing(3)
    sam = enc.find_actor("Sam")
    cores = {a.name: _core(a.name) for a in enc.actors}

    apply_beat(
        enc,
        sam,
        _strike(target_edge_delta=7, target_select="spread"),
        RollOutcome.Success,
        edge_resolver=cores.get,
    )

    # 7 // 3 = 2; remainder of 1 is dropped (genre-truthful AOE attenuation).
    assert cores["Mook0"].hp.current == 8
    assert cores["Mook1"].hp.current == 8
    assert cores["Mook2"].hp.current == 8


def test_spread_with_one_opponent_acts_like_focus():
    enc = _enc_with_opposing(1)
    sam = enc.find_actor("Sam")
    cores = {a.name: _core(a.name) for a in enc.actors}

    apply_beat(
        enc,
        sam,
        _strike(target_edge_delta=4, target_select="spread"),
        RollOutcome.Success,
        edge_resolver=cores.get,
    )

    assert cores["Mook0"].hp.current == 6


def test_spread_skips_withdrawn_opponents():
    """Withdrawn actors don't count as live opponents for spread division."""
    enc = _enc_with_opposing(3)
    enc.actors[2].withdrawn = True  # Mook1 withdrawn
    sam = enc.find_actor("Sam")
    cores = {a.name: _core(a.name) for a in enc.actors}

    apply_beat(
        enc,
        sam,
        _strike(target_edge_delta=6, target_select="spread"),
        RollOutcome.Success,
        edge_resolver=cores.get,
    )

    # 2 live opponents → 6 // 2 = 3 each.
    assert cores["Mook0"].hp.current == 7
    assert cores["Mook1"].hp.current == 10  # withdrawn, untouched
    assert cores["Mook2"].hp.current == 7


def test_spread_drops_to_zero_marks_composure_break():
    """If any spread target hits 0 edge, composure_break still fires."""
    enc = _enc_with_opposing(3)
    sam = enc.find_actor("Sam")
    cores = {
        "Sam": _core("Sam"),
        "Mook0": _core("Mook0", current=2),
        "Mook1": _core("Mook1", current=10),
        "Mook2": _core("Mook2", current=10),
    }

    apply_beat(
        enc,
        sam,
        _strike(target_edge_delta=6, target_select="spread"),  # 2 each
        RollOutcome.Success,
        edge_resolver=cores.get,
    )

    assert cores["Mook0"].hp.current == 0
    assert enc.resolved is True
    assert enc.outcome == "composure_break:Mook0"


# ---------------------------------------------------------------------------
# Swarm mode (focus targeting, ally-amplification flag)
# ---------------------------------------------------------------------------


def test_swarm_targets_first_opponent_like_focus():
    """Swarm mode hits exactly one target — the same one focus would."""
    enc = _enc_with_opposing(3)
    sam = enc.find_actor("Sam")
    cores = {a.name: _core(a.name) for a in enc.actors}

    apply_beat(
        enc,
        sam,
        _strike(target_edge_delta=3, target_select="swarm"),
        RollOutcome.Success,
        edge_resolver=cores.get,
    )

    assert cores["Mook0"].hp.current == 7
    assert cores["Mook1"].hp.current == 10
    assert cores["Mook2"].hp.current == 10


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_unknown_target_select_value_rejected_at_load_time():
    """BeatDef pydantic validator must reject malformed target_select."""
    import pytest

    with pytest.raises(ValueError, match="target_select"):
        BeatDef.model_validate(
            {
                "id": "x",
                "label": "x",
                "kind": "strike",
                "base": 1,
                "stat_check": "STR",
                "target_edge_delta": 1,
                "target_select": "bogus",
            }
        )
