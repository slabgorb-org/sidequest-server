"""LethalityArbiter — deterministic verdict synthesis from game state + policy.

Spec: docs/superpowers/specs/2026-04-23-local-dm-decomposer-design.md §4
Group C: verdict producer consumes HP/edge state + policy, emits verdicts
and paired narrator directives. Edge-based triggers only for Phase A.
"""

from __future__ import annotations

from sidequest.agents.lethality_arbiter import LethalityArbiter, LethalityResult
from sidequest.agents.subsystems import BankResult
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.genre.models.lethality import LethalityPolicy, VerdictsOnZeroHp
from sidequest.protocol.dispatch import (
    DispatchPackage,
    LethalityVerdict,
    PlayerDispatch,
)


def _make_pc(name: str, edge_current: int, edge_max: int = 10) -> CreatureCore:
    return CreatureCore(
        name=name,
        description="A PC.",
        personality="Brave.",
        level=1,
        xp=0,
        inventory=Inventory(),
        statuses=[],
        hp=HpPool(current=edge_current, max=edge_max, base_max=edge_max),
    )


def _heavy_metal_policy() -> LethalityPolicy:
    return LethalityPolicy(
        genre_key="heavy_metal",
        default_reversibility="permanent",
        verdicts_on_zero_hp=VerdictsOnZeroHp(pc="dead", npc="dead"),
        soul_md_constraint="genre_truth:lethal_for_this_genre",
        must_narrate="Render the death.",
        must_not_narrate="narrate survival; invent rescue",
    )


def _empty_package(turn_id: str = "turn-1", player_id: str = "alice") -> DispatchPackage:
    return DispatchPackage(
        turn_id=turn_id,
        per_player=[PlayerDispatch(player_id=player_id, raw_action="swing sword")],
        cross_player=[],
        confidence_global=1.0,
    )


def test_pc_at_zero_edge_produces_heavy_metal_dead_verdict():
    """Edge.current == 0 → policy.verdicts_on_zero_hp.pc → verdict emitted."""
    arbiter = LethalityArbiter(policy=_heavy_metal_policy())
    pc = _make_pc("Alice", edge_current=0)
    result = arbiter.arbitrate(
        package=_empty_package(player_id="alice"),
        bank_result=BankResult(),
        pc_cores_by_player={"alice": pc},
        npc_cores_by_name={},
    )
    assert isinstance(result, LethalityResult)
    assert len(result.verdicts) == 1
    v = result.verdicts[0]
    assert v.entity == "player:alice"
    assert v.verdict == "dead"
    assert v.reversibility == "permanent"
    assert v.soul_md_constraint == "genre_truth:lethal_for_this_genre"
    assert "Alice" in v.cause


def test_pc_above_zero_edge_produces_no_verdict():
    """Edge.current > 0 → arbiter emits nothing for that PC."""
    arbiter = LethalityArbiter(policy=_heavy_metal_policy())
    pc = _make_pc("Alice", edge_current=5)
    result = arbiter.arbitrate(
        package=_empty_package(player_id="alice"),
        bank_result=BankResult(),
        pc_cores_by_player={"alice": pc},
        npc_cores_by_name={},
    )
    assert result.verdicts == []
    assert result.directives == []


# Task 6 — NPC + multi-entity coverage


def _caverns_policy() -> LethalityPolicy:
    return LethalityPolicy(
        genre_key="caverns_and_claudes",
        default_reversibility="narrative_only",
        verdicts_on_zero_hp=VerdictsOnZeroHp(pc="humiliated", npc="defeated"),
        soul_md_constraint="genre_truth:comedic_danger_no_permadeath",
        must_narrate="Slapstick incapacitation.",
        must_not_narrate="permadeath; solemn eulogy",
    )


def test_npc_at_zero_edge_produces_caverns_defeated_verdict():
    arbiter = LethalityArbiter(policy=_caverns_policy())
    npc = _make_pc("Gobbert", edge_current=0)
    result = arbiter.arbitrate(
        package=_empty_package(),
        bank_result=BankResult(),
        pc_cores_by_player={},
        npc_cores_by_name={"Gobbert": npc},
    )
    assert len(result.verdicts) == 1
    v = result.verdicts[0]
    assert v.entity == "npc:Gobbert"
    assert v.verdict == "defeated"
    assert v.reversibility == "narrative_only"


def test_multiple_entities_at_zero_edge_produce_separate_verdicts():
    arbiter = LethalityArbiter(policy=_caverns_policy())
    alice = _make_pc("Alice", edge_current=0)
    bob = _make_pc("Bob", edge_current=3)
    gobbert = _make_pc("Gobbert", edge_current=0)
    result = arbiter.arbitrate(
        package=_empty_package(),
        bank_result=BankResult(),
        pc_cores_by_player={"alice": alice, "bob": bob},
        npc_cores_by_name={"Gobbert": gobbert},
    )
    entities = sorted(v.entity for v in result.verdicts)
    assert entities == ["npc:Gobbert", "player:alice"]


# Task 7 — paired must_narrate + must_not_narrate directives


def test_arbiter_emits_paired_directives_per_verdict():
    """Spec §4.2: every verdict ships with must_narrate + must_not_narrate."""
    arbiter = LethalityArbiter(policy=_heavy_metal_policy())
    pc = _make_pc("Alice", edge_current=0)
    result = arbiter.arbitrate(
        package=_empty_package(player_id="alice"),
        bank_result=BankResult(),
        pc_cores_by_player={"alice": pc},
        npc_cores_by_name={},
    )
    kinds = [d.kind for d in result.directives]
    assert kinds.count("must_narrate") == 1
    assert kinds.count("must_not_narrate") == 1
    must = next(d for d in result.directives if d.kind == "must_narrate")
    must_not = next(d for d in result.directives if d.kind == "must_not_narrate")
    assert "Render the death" in must.payload
    assert "narrate survival" in must_not.payload


def test_no_directives_when_no_verdicts():
    arbiter = LethalityArbiter(policy=_heavy_metal_policy())
    pc = _make_pc("Alice", edge_current=7)
    result = arbiter.arbitrate(
        package=_empty_package(),
        bank_result=BankResult(),
        pc_cores_by_player={"alice": pc},
        npc_cores_by_name={},
    )
    assert result.directives == []


# Task 8 — merge with decomposer-authored verdicts


def _decomposer_verdict(entity: str, verdict: str) -> LethalityVerdict:
    return LethalityVerdict(
        entity=entity,
        verdict=verdict,  # type: ignore[arg-type]
        cause="decomposer proposed",
        reversibility="narrative_only",
        narrator_directive="decomposer directive",
        soul_md_constraint="decomposer_constraint",
        witness_scope={},
    )


def test_arbiter_overrides_decomposer_verdict_on_conflict():
    """Arbiter wins when both author a verdict for the same entity."""
    arbiter = LethalityArbiter(policy=_heavy_metal_policy())
    pc = _make_pc("Alice", edge_current=0)
    pkg = DispatchPackage(
        turn_id="t1",
        per_player=[
            PlayerDispatch(
                player_id="alice",
                raw_action="x",
                lethality=[_decomposer_verdict("player:alice", "humiliated")],
            )
        ],
        cross_player=[],
        confidence_global=1.0,
    )
    result = arbiter.arbitrate(
        package=pkg,
        bank_result=BankResult(),
        pc_cores_by_player={"alice": pc},
        npc_cores_by_name={},
    )
    verdicts_for_alice = [v for v in result.verdicts if v.entity == "player:alice"]
    assert len(verdicts_for_alice) == 1
    assert verdicts_for_alice[0].verdict == "dead"  # arbiter wins
    assert "decomposer proposed" not in verdicts_for_alice[0].cause


def test_arbiter_passes_through_decomposer_only_entities():
    """Entity the arbiter did not touch → decomposer verdict preserved."""
    arbiter = LethalityArbiter(policy=_heavy_metal_policy())
    pkg = DispatchPackage(
        turn_id="t1",
        per_player=[
            PlayerDispatch(
                player_id="alice",
                raw_action="x",
                lethality=[_decomposer_verdict("npc:BoneChewer", "maimed")],
            )
        ],
        cross_player=[],
        confidence_global=1.0,
    )
    result = arbiter.arbitrate(
        package=pkg,
        bank_result=BankResult(),
        pc_cores_by_player={},  # no one at zero edge
        npc_cores_by_name={},  # BoneChewer's core not tracked this turn
    )
    assert len(result.verdicts) == 1
    assert result.verdicts[0].entity == "npc:BoneChewer"
    assert result.verdicts[0].verdict == "maimed"
    assert "decomposer proposed" in result.verdicts[0].cause
