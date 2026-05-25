"""Per-pack smoke: load each shipped genre pack's lethality_policy and run
the arbiter on a PC at zero hp. Each pack must produce a verdict whose
kind matches its declared policy.verdicts_on_zero_hp.pc.

Group C Task 13 — mechanical breadth: if a pack author typos the YAML or
drifts the schema in a future PR, this parametrised smoke catches it.
"""

from __future__ import annotations

import pytest

from sidequest.agents.lethality_arbiter import LethalityArbiter
from sidequest.agents.subsystems import BankResult
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.genre.lethality_policy_loader import load_lethality_policy
from sidequest.protocol.dispatch import DispatchPackage, PlayerDispatch
from tests._helpers.genre_paths import find_pack_path

SHIPPED_PACKS = [
    "caverns_and_claudes",
    "elemental_harmony",
    "heavy_metal",
    "mutant_wasteland",
    "space_opera",
    "spaghetti_western",
]


def _pc(current: int) -> CreatureCore:
    return CreatureCore(
        name="Alice",
        description="d",
        personality="p",
        inventory=Inventory(),
        hp=HpPool(current=current, max=10, base_max=10),
    )


@pytest.mark.parametrize("pack_name", SHIPPED_PACKS)
def test_zero_edge_pc_produces_policy_declared_verdict(pack_name: str):
    pack_dir = find_pack_path(pack_name)
    policy = load_lethality_policy(pack_dir)
    arbiter = LethalityArbiter(policy=policy)
    result = arbiter.arbitrate(
        package=DispatchPackage(
            turn_id="t1",
            per_player=[PlayerDispatch(player_id="alice", raw_action="x")],
            cross_player=[],
            confidence_global=1.0,
        ),
        bank_result=BankResult(),
        pc_cores_by_player={"alice": _pc(0)},
        npc_cores_by_name={},
    )
    assert len(result.verdicts) == 1, f"pack={pack_name} produced 0 or >1 verdicts"
    v = result.verdicts[0]
    assert v.verdict == policy.verdicts_on_zero_hp.pc
    assert v.reversibility == policy.default_reversibility
    assert v.soul_md_constraint == policy.soul_md_constraint
    # Paired directives present.
    kinds = [d.kind for d in result.directives]
    assert "must_narrate" in kinds
    assert "must_not_narrate" in kinds


@pytest.mark.parametrize("pack_name", SHIPPED_PACKS)
def test_pack_policy_files_load_without_error(pack_name: str):
    """Lightweight load check — catches YAML drift before integration runs."""
    pack_dir = find_pack_path(pack_name)
    policy = load_lethality_policy(pack_dir)
    assert policy.genre_key == pack_name
    assert policy.must_narrate.strip()
    assert policy.must_not_narrate.strip()
