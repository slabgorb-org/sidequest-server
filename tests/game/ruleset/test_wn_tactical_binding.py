"""RED tests for Task 5 — Without-Number tactical binding (ADR-096 v2, Track C2).

The WN SRD movement/reach/range facts are authored ONCE on
``WithoutNumberRulesetModule`` (cell scale, per-turn Move budget in cells, melee
reach, ranged range bands) — SRD-sourced, inherited by every sibling
(swn/wwn/cwn/awn), never re-derived per world (the flat-13 bug class). The
adjudicators (``adjudicate_tactical_move`` / ``adjudicate_tactical_reach``) are
this ruleset binding's production wiring into the pure C1 library
(``sidequest.game.tactical.adjudication``): they must delegate, not reimplement.

Test values are hand-verified against the merged C1 library, per the 165-1
carryover (the plan doc's embedded code is not authoritative).
"""

from types import SimpleNamespace

import pytest

from sidequest.game.ruleset.registry import get_ruleset_module
from sidequest.game.ruleset.without_number import WithoutNumberRulesetModule
from sidequest.game.tactical.adjudication import MoveAdjudication, RangeAdjudication

ROOM = "#######\n#.....#\n#.....#\n#.....#\n#######"  # 5x3 floor interior


def _wn():
    return get_ruleset_module("wwn")


# --- SRD facts: move / reach / range -------------------------------------------------


def test_combat_move_cells_default_and_override():
    wn = _wn()
    assert wn.combat_move_cells(SimpleNamespace(move=None)) == 6  # 10m / 1.5
    assert wn.combat_move_cells(SimpleNamespace(move=15)) == 10  # 15m / 1.5
    assert wn.combat_move_cells(None) == 6  # no core -> SRD default


def test_combat_move_cells_floors_to_min_one_cell():
    """A sub-cell Move must never floor to a 0-cell budget (that would deny all
    movement); the SRD binding clamps to at least one cell."""
    wn = _wn()
    assert wn.combat_move_cells(SimpleNamespace(move=1)) == 1  # int(1/1.5)=0 -> max(1, 0)


def test_combat_move_cells_explicit_zero_is_honored_not_defaulted():
    """165-3 BLOCKER 2b: an immobilized stock authors ``move=0``
    (mutation/stocks.py propagates it). The 165-2 shim did
    ``getattr(core, "move", None) or DEFAULT_MOVE_METERS`` — and ``0 or 10`` is
    ``10``, so a pinned-down actor SILENTLY regained the full 10 m default Move
    (6 cells). That is a No-Silent-Fallbacks violation: an explicit 0 must be
    honoured, distinct from an absent value. The fix is an ``is None`` check
    (not falsy-``or``)."""
    wn = _wn()
    default_cells = wn.combat_move_cells(SimpleNamespace(move=None))  # SRD 10 m -> 6
    zero_cells = wn.combat_move_cells(SimpleNamespace(move=0))
    assert zero_cells != default_cells, "move=0 was silently promoted to the SRD default"
    assert zero_cells == 1  # max(1, int(0/1.5)) floor — the immobilized actor is honoured


def test_weapon_range_cells_melee_and_ranged():
    wn = _wn()
    assert wn.weapon_range_cells(SimpleNamespace(range_band=None)) == (
        "melee",
        wn.MELEE_REACH_CELLS,
    )
    assert wn.weapon_range_cells(SimpleNamespace(range_band="melee")) == ("melee", 1)
    mode, cells = wn.weapon_range_cells(SimpleNamespace(range_band="rifle"))
    assert mode == "ranged" and cells == wn.RANGE_BAND_CELLS["rifle"]


def test_weapon_range_cells_garbage_band_fails_loud():
    """165-3 BLOCKER 2a (165-2 Delivery Finding, now RESOLVED): an unparseable
    band must FAIL LOUD, never silently cap at rifle. The 165-2 shim did
    ``.get(band, RANGE_BAND_CELLS["rifle"])`` — a No-Silent-Fallbacks violation
    (a mistyped/unknown weapon range would ship as a 40-cell rifle and nobody
    would notice). ``"trebuchet"`` is neither a numeric ``"N/N"`` range, ``melee``,
    nor a known categorical band, so it must raise (ValueError listing the
    accepted forms). REPLACES test_weapon_range_cells_unknown_band_defaults_to_rifle,
    which pinned the buggy behaviour."""
    wn = _wn()
    with pytest.raises(ValueError):
        wn.weapon_range_cells(SimpleNamespace(range_band="trebuchet"))


def test_weapon_range_cells_reads_real_content_nn_format():
    """165-3 BLOCKER 1: real packs author ``range_band`` as ``"N/N"`` short/long
    METRE strings ("10/30", "600/2400" — 16 distinct values across 8 inventory
    files), NOT the categorical keys ("rifle"/"pistol"/…) the 165-2 table was
    keyed on. So the shipped shim resolved EVERY ranged weapon identically: a
    ``DamageSpec`` carries no ``range_band`` at all (→ silent melee), and even
    read off the ``CatalogItem`` the ``"N/N"`` value missed every table key and
    hit the rifle silent-cap (BLOCKER 2a). This story wires the reach gate to
    real specs, so the ``"N/N"`` format MUST resolve to a real ranged band whose
    cell reach tracks the authored range — a pistol must not out-range a rifle.

    Contract (fix-agnostic — derive-from-numbers OR map-to-band, Dev's call):
    a real ``"N/N"`` weapon is ``ranged`` (not silently melee), and two weapons of
    very different range do NOT collapse to one identical cap."""
    wn = _wn()
    short_mode, short_cells = wn.weapon_range_cells(SimpleNamespace(range_band="10/30"))
    long_mode, long_cells = wn.weapon_range_cells(SimpleNamespace(range_band="600/2400"))
    assert short_mode == "ranged", "a real ranged weapon must not silently resolve as melee"
    assert long_mode == "ranged"
    assert short_cells != long_cells, (
        "distinct authored ranges collapsed to one cap — the rifle silent-fallback bug"
    )
    assert short_cells < long_cells, "a short-range weapon must not out-reach a long-range one"


# --- Adjudicators over C1 ------------------------------------------------------------


def test_adjudicate_tactical_move_uses_move_budget():
    wn = _wn()
    core = SimpleNamespace(move=None)  # 6 cells
    ok = wn.adjudicate_tactical_move(
        origin=(1, 1), path=[(1, 1), (2, 1), (3, 1)], core=core, mask=ROOM
    )
    assert ok.valid and ok.cells_spent == 2 and ok.cells_budget == 6
    slow = SimpleNamespace(move=1)  # 1m/1.5 -> max(1,0)=1 cell budget
    denied = wn.adjudicate_tactical_move(
        origin=(1, 1), path=[(1, 1), (2, 1), (3, 1)], core=slow, mask=ROOM
    )
    assert not denied.valid and "you can move 1 cell" in denied.reason


def test_adjudicate_tactical_move_charges_difficult_terrain():
    """Difficult terrain is charged on ENTERING a cell (2 vs 1), and must push a
    move over budget through the binding. Closes the 165-1 coverage gap on
    ``movement_cost(difficult=...)`` via its production caller."""
    wn = _wn()
    core = SimpleNamespace(move=5)  # int(5/1.5)=3 cell budget
    path = [(1, 1), (2, 1), (3, 1)]
    easy = wn.adjudicate_tactical_move(origin=(1, 1), path=path, core=core, mask=ROOM)
    assert easy.valid and easy.cells_spent == 2 and easy.cells_budget == 3
    hard = wn.adjudicate_tactical_move(
        origin=(1, 1),
        path=path,
        core=core,
        mask=ROOM,
        difficult=frozenset({(2, 1), (3, 1)}),
    )
    assert not hard.valid
    assert hard.cells_spent == 4  # 2 difficult entries x 2 cost each
    assert hard.cells_budget == 3


def test_adjudicate_tactical_reach_melee_out_of_reach():
    wn = _wn()
    spec = SimpleNamespace(range_band=None)  # melee reach 1
    verdict = wn.adjudicate_tactical_reach(
        attacker_cell=(1, 1), target_cell=(4, 1), spec=spec, mask=ROOM
    )
    assert not verdict.in_range and verdict.mode == "melee" and verdict.distance_cells == 3


def test_adjudicate_tactical_reach_ranged_los_gate():
    wn = _wn()
    spec = SimpleNamespace(range_band="rifle")
    verdict = wn.adjudicate_tactical_reach(
        attacker_cell=(1, 1), target_cell=(5, 3), spec=spec, mask=ROOM
    )
    assert verdict.in_range and verdict.mode == "ranged" and verdict.has_los


def test_adjudicators_return_c1_types():
    """The binding must delegate to the pure C1 library, not reimplement — proven
    by the verdicts being C1's own dataclasses. This is C1's production wiring."""
    wn = _wn()
    move_verdict = wn.adjudicate_tactical_move(
        origin=(1, 1), path=[(1, 1), (2, 1)], core=SimpleNamespace(move=None), mask=ROOM
    )
    reach_verdict = wn.adjudicate_tactical_reach(
        attacker_cell=(1, 1), target_cell=(2, 1), spec=SimpleNamespace(range_band=None), mask=ROOM
    )
    assert isinstance(move_verdict, MoveAdjudication)
    assert isinstance(reach_verdict, RangeAdjudication)


# --- Authored-once inheritance (anti flat-13) ----------------------------------------


def test_tactical_facts_authored_once_shared_by_all_wn_siblings():
    """The SRD tactical facts are defined on the WN core and every sibling
    inherits the SAME values — not re-derived per ruleset/world. A sibling that
    forked its own move scale would fail here (the flat-13 re-derivation bug)."""
    # Facts live on the base class, not scattered per subclass.
    assert WithoutNumberRulesetModule.METERS_PER_CELL == 1.5  # ADR-096 5-ft / 1.5-m cell
    assert WithoutNumberRulesetModule.DEFAULT_MOVE_METERS == 10  # WN SRD default Move
    assert WithoutNumberRulesetModule.MELEE_REACH_CELLS == 1

    core = SimpleNamespace(move=None)
    for slug in ("swn", "wwn", "cwn", "awn"):
        sibling = get_ruleset_module(slug)
        assert isinstance(sibling, WithoutNumberRulesetModule)
        assert sibling.METERS_PER_CELL == 1.5
        assert sibling.DEFAULT_MOVE_METERS == 10
        assert sibling.MELEE_REACH_CELLS == 1
        assert sibling.combat_move_cells(core) == 6
        assert sibling.RANGE_BAND_CELLS["rifle"] == _wn().RANGE_BAND_CELLS["rifle"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
