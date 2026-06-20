"""RESUME + resolve-with-recorded-defense (spec 2026-06-18 §5,§7, story 126-8).

First, a characterization test pins today's NPC-server-rolls behavior so the
recorded-defense branch is provably additive (this one PASSES on current code).
Then the resume test asserts the PARKED PC defense is read from the ledger — never
re-rolled — and the ledger is cleared after the walk.

RED: resume_fate_exchange does not exist yet (plan Task 7); it is imported lazily
inside the resume test so the characterization safety-net still runs green.
"""

from __future__ import annotations

import random

import sidequest.game.ruleset.fate_resolution as fate_resolution
import sidequest.server.dispatch.fate_conflict as fc
from sidequest.game.encounter import FateSealedCommit
from sidequest.game.ruleset import get_ruleset_module
from sidequest.server.dispatch.fate_conflict import run_fate_exchange
from tests._helpers.fate_fixtures import conflict_with_pc_and_npc, parked_conflict_filled


def test_npc_defender_still_server_rolls(monkeypatch):
    # CHARACTERIZATION (passes today): an exchange with no recorded defenses walks
    # via the server RNG — roll_4df fires for NPC seating + reactive defenses.
    snap, encounter = conflict_with_pc_and_npc(pc="Rux", npc="Bandit")
    encounter.fate_commits.clear()
    encounter.fate_commits.append(
        FateSealedCommit(
            actor="Rux",
            action="attack",
            skill="Fight",
            target="Bandit",
            ladder_total=4,
            dice=(1, 1, 1, 1),
        )
    )

    calls = {"n": 0}
    real = fate_resolution.roll_4df
    monkeypatch.setattr(
        fate_resolution,
        "roll_4df",
        lambda rng: calls.__setitem__("n", calls["n"] + 1) or real(rng),
    )

    result = run_fate_exchange(
        encounter=encounter,
        snapshot=snap,
        ruleset=get_ruleset_module("fate"),
        rng=random.Random(3),
        round_number=1,
    )
    assert calls["n"] >= 1  # server rolled (NPC seating and/or reactive defense)
    assert isinstance(result.resolution_order, str) and result.resolution_order


def test_recorded_pc_defense_used_instead_of_roll(monkeypatch):
    from sidequest.server.dispatch.fate_conflict import resume_fate_exchange

    # Parked: NPC attack sealed at total 5; Rux's defense ALREADY recorded as 2.
    # 5 - 2 = 3 unabsorbed shifts → a real mechanical effect lands on Rux.
    snap, encounter = parked_conflict_filled(
        defender="Rux", attacker="Bandit", attack_total=5, recorded_defense_total=2
    )

    calls = {"roll_defense": 0}
    real = fc._roll_defense
    monkeypatch.setattr(
        fc,
        "_roll_defense",
        lambda **k: calls.__setitem__("roll_defense", calls["roll_defense"] + 1) or real(**k),
    )

    resume_fate_exchange(
        encounter=encounter,
        snapshot=snap,
        ruleset=get_ruleset_module("fate"),
        round_number=1,
    )

    # The PC defense came from the RECORD, not a server roll; ledger cleared.
    assert calls["roll_defense"] == 0
    assert encounter.pending_defenses == []

    # The 3-shift hit is mechanically real: stress checked, a consequence taken,
    # or Rux taken out (bind the ruleset — the math is Fate's, not improvised).
    rux = snap.find_creature_core("Rux")
    took_stress = any(b.checked for b in rux.fate_sheet.stress["physical"].boxes)
    took_consequence = any(c.aspect is not None for c in rux.fate_sheet.consequences)
    taken_out = encounter.find_actor("Rux").withdrawn
    assert took_stress or took_consequence or taken_out


def test_recorded_pc_defense_that_ties_applies_no_harm_and_grants_momentum():
    # AC4 (defense WINS, exact tie). test_recorded_pc_defense_used_instead_of_roll
    # covers the LOSE side (defense 2 vs attack 5 → 3 shifts of harm); this covers
    # the win side of _resolve_attack's recorded-defense branch where shifts == 0:
    # attack ladder_total 3 == recorded defense 3 → NO harm, defender NOT taken out,
    # and the defender gains a Momentum boost (Fate's exact-tie reward).
    from sidequest.server.dispatch.fate_conflict import _resolve_attack

    snap, encounter = parked_conflict_filled(
        defender="Rux", attacker="Bandit", attack_total=3, recorded_defense_total=3
    )
    commit = next(c for c in encounter.fate_commits if c.actor == "Bandit")
    recorded = {p.defender: p for p in encounter.pending_defenses}
    hints: list[str] = []

    _resolve_attack(
        encounter=encounter,
        snapshot=snap,
        ruleset=get_ruleset_module("fate"),
        commit=commit,
        mental=False,
        rng=random.Random(0),
        hints=hints,
        recorded_defenses=recorded,
    )

    rux = snap.find_creature_core("Rux")
    assert not any(b.checked for b in rux.fate_sheet.stress["physical"].boxes)
    assert all(c.aspect is None for c in rux.fate_sheet.consequences)
    assert encounter.find_actor("Rux").withdrawn is False
    # Exact tie grants the defender exactly one Momentum boost with a free invoke.
    momentum = [a for a in encounter.situation_aspects if "Momentum" in a.text]
    assert len(momentum) == 1
    assert momentum[0].kind == "boost"
    assert momentum[0].free_invokes == 1


def test_recorded_pc_defense_that_beats_attack_misses_with_no_harm_no_boost():
    # AC4 (defense WINS, clean miss). Defense strictly greater than the attack →
    # shifts < 0 → the attack MISSES: no harm, defender NOT taken out, and NO Momentum
    # boost (the boost is the exact-tie reward only, not every successful defense).
    from sidequest.server.dispatch.fate_conflict import _resolve_attack

    snap, encounter = parked_conflict_filled(
        defender="Rux", attacker="Bandit", attack_total=2, recorded_defense_total=4
    )
    commit = next(c for c in encounter.fate_commits if c.actor == "Bandit")
    recorded = {p.defender: p for p in encounter.pending_defenses}
    hints: list[str] = []

    _resolve_attack(
        encounter=encounter,
        snapshot=snap,
        ruleset=get_ruleset_module("fate"),
        commit=commit,
        mental=False,
        rng=random.Random(0),
        hints=hints,
        recorded_defenses=recorded,
    )

    rux = snap.find_creature_core("Rux")
    assert not any(b.checked for b in rux.fate_sheet.stress["physical"].boxes)
    assert all(c.aspect is None for c in rux.fate_sheet.consequences)
    assert encounter.find_actor("Rux").withdrawn is False
    assert not any("Momentum" in a.text for a in encounter.situation_aspects)
    # The miss is recorded as a narrator hint (mechanical truth, not improvisation).
    assert any("missed" in h for h in hints)
