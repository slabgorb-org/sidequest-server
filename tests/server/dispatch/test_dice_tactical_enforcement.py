"""Task 7 (ADR-096 v2, Track C2) — reach enforcement inside dispatch_dice_throw.

Two layers:
  * UNIT — drive ``_enforce_tactical_reach`` directly with synthetic actors
    carrying ``per_actor_state['cell']``. It calls the REAL WN binding
    (``WithoutNumberRulesetModule.adjudicate_tactical_reach``) which delegates to
    the pure C1 library — so these already reach ``game.tactical`` through the
    production ruleset, not just adjudication.py in isolation.
  * WIRING (test_dice_tactical_enforcement_wiring.py) — the load-bearing proof
    that ``dispatch_dice_throw`` actually CALLS the helper on a real strike.

RED until ``_enforce_tactical_reach`` exists in ``sidequest.server.dispatch.dice``.

Contract (plan Task 7, hand-verified against the real RangeAdjudication API —
the plan's embedded snippet is reference only): returns a ``RangeAdjudication``
when enforcement runs; returns ``None`` (a deliberate skip, emitting
``tactical.enforcement.skipped`` — NOT a silent fallback) when the ruleset is
non-WN, the mask is absent, or either actor lacks a cell. An out-of-range verdict
carries ``in_range=False`` and a legible ``reason``; the caller aborts the strike,
never silently retargets (SOUL: The Test).
"""

from __future__ import annotations

from types import SimpleNamespace

from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.ruleset.registry import get_ruleset_module
from sidequest.server.dispatch.dice import _enforce_tactical_reach

# 7x5 interior; floor is x in 1..5, y in 1..3.
ROOM = "#######\n#.....#\n#.....#\n#.....#\n#######"


def _combat(attacker_cell, target_cell):
    a = EncounterActor(
        name="Rux", role="combatant", side="player", per_actor_state={"cell": list(attacker_cell)}
    )
    t = EncounterActor(
        name="rope-spider",
        role="combatant",
        side="opponent",
        per_actor_state={"cell": list(target_cell)},
    )
    return StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="tension", threshold=10),
        opponent_metric=EncounterMetric(name="fear", threshold=10),
        actors=[a, t],
    )


def test_melee_in_reach_returns_valid_verdict():
    enc = _combat((1, 1), (2, 1))  # adjacent -> in melee reach
    verdict = _enforce_tactical_reach(
        ruleset=get_ruleset_module("wwn"),
        encounter=enc,
        actor=enc.actors[0],
        target_name="rope-spider",
        spec=SimpleNamespace(range_band=None),  # None -> melee
        mask=ROOM,
        snapshot=None,
    )
    assert verdict is not None
    assert verdict.in_range is True


def test_melee_out_of_reach_returns_denied_verdict():
    enc = _combat((1, 1), (5, 3))  # 4 cells away -> out of melee reach
    verdict = _enforce_tactical_reach(
        ruleset=get_ruleset_module("wwn"),
        encounter=enc,
        actor=enc.actors[0],
        target_name="rope-spider",
        spec=SimpleNamespace(range_band=None),
        mask=ROOM,
        snapshot=None,
    )
    assert verdict is not None
    assert verdict.in_range is False
    assert "reach" in verdict.reason.lower()  # melee noun, legible refusal


def test_no_grid_skips_enforcement():
    """No cells on the actors == no tactical grid. Enforcement is a DELIBERATE
    no-op (returns None, emits tactical.enforcement.skipped) — combat resolves as
    it does today. This is the scope boundary, not a silent fallback."""
    enc = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="tension", threshold=10),
        opponent_metric=EncounterMetric(name="fear", threshold=10),
        actors=[
            EncounterActor(name="Rux", role="combatant", side="player"),
            EncounterActor(name="rope-spider", role="combatant", side="opponent"),
        ],
    )
    verdict = _enforce_tactical_reach(
        ruleset=get_ruleset_module("wwn"),
        encounter=enc,
        actor=enc.actors[0],
        target_name="rope-spider",
        spec=SimpleNamespace(range_band=None),
        mask=None,
        snapshot=None,
    )
    assert verdict is None


def test_missing_mask_with_cells_still_skips():
    """Even when both actors carry cells, a missing room mask skips enforcement —
    there is nothing to adjudicate LOS/walls against. Deliberate no-op (None)."""
    enc = _combat((1, 1), (5, 3))
    verdict = _enforce_tactical_reach(
        ruleset=get_ruleset_module("wwn"),
        encounter=enc,
        actor=enc.actors[0],
        target_name="rope-spider",
        spec=SimpleNamespace(range_band=None),
        mask=None,
        snapshot=None,
    )
    assert verdict is None


def test_dial_ruleset_not_enforced():
    """Capability gate (ADR-117): only a WithoutNumber binding enforces the grid.
    A dial/Fate pack is untouched — even with cells and a mask present."""
    enc = _combat((1, 1), (5, 3))
    verdict = _enforce_tactical_reach(
        ruleset=get_ruleset_module("dial"),
        encounter=enc,
        actor=enc.actors[0],
        target_name="rope-spider",
        spec=SimpleNamespace(range_band=None),
        mask=ROOM,
        snapshot=None,
    )
    assert verdict is None  # non-WN ruleset -> no grid enforcement
