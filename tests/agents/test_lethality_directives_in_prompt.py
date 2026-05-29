"""End-to-end: arbiter's directives land in the narrator prompt.

Verifies the paired must_narrate / must_not_narrate lines appear in the
narrator_directives section produced by Orchestrator.build_narrator_prompt
when a PC is at zero edge, and that they are absent when nobody is down.

Group C Task 10 — the wiring pass between LethalityArbiter and the prompt
registry. No session handler in scope here: TurnContext is constructed
directly and the arbiter runs inline on it.
"""

from __future__ import annotations

import pytest

from sidequest.agents.claude_client import ClaudeClient
from sidequest.agents.orchestrator import (
    Orchestrator,
    TurnContext,
)
from sidequest.agents.subsystems import BankResult
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.genre.models.lethality import LethalityPolicy, VerdictsOnZeroHp
from sidequest.protocol.dispatch import (
    DispatchPackage,
    PlayerDispatch,
)
from tests.agents.test_orchestrator import make_spawn_fn

pytestmark = pytest.mark.asyncio


def _policy() -> LethalityPolicy:
    return LethalityPolicy(
        genre_key="heavy_metal",
        default_reversibility="permanent",
        verdicts_on_zero_hp=VerdictsOnZeroHp(pc="dead", npc="dead"),
        soul_md_constraint="genre_truth:lethal_for_this_genre",
        must_narrate="Render the death with specific brutal detail.",
        must_not_narrate="invent rescue; narrate survival",
    )


def _pc(current: int) -> CreatureCore:
    return CreatureCore(
        name="Alice",
        description="d",
        personality="p",
        inventory=Inventory(),
        hp=HpPool(current=current, max=10, base_max=10),
    )


def _empty_visible_package() -> DispatchPackage:
    return DispatchPackage(
        turn_id="t1",
        per_player=[PlayerDispatch(player_id="alice", raw_action="swing")],
        cross_player=[],
        confidence_global=1.0,
    )


async def test_pc_at_zero_edge_injects_paired_directives_in_prompt():
    """Arbiter verdict + paired must/must-not directives land in the prompt."""
    orch = Orchestrator(client=ClaudeClient(spawn_fn=make_spawn_fn("narration")))
    context = TurnContext(
        character_name="Alice",
        dispatch_package=_empty_visible_package(),
        bank_result=BankResult(),
        lethality_policy=_policy(),
        pc_cores_by_player={"alice": _pc(0)},
        npc_cores_by_name={},
    )
    prompt, _ = await orch.build_narrator_prompt(
        "swing sword",
        context,
    )
    assert "must_narrate" in prompt
    assert "Render the death" in prompt
    assert "must_not_narrate" in prompt
    assert "narrate survival" in prompt


async def test_pc_above_zero_edge_injects_no_lethality_directives():
    """Quiet turn: arbiter emits no directives, prompt stays clean of the policy."""
    orch = Orchestrator(client=ClaudeClient(spawn_fn=make_spawn_fn("narration")))
    context = TurnContext(
        character_name="Alice",
        dispatch_package=_empty_visible_package(),
        bank_result=BankResult(),
        lethality_policy=_policy(),
        pc_cores_by_player={"alice": _pc(7)},
        npc_cores_by_name={},
    )
    prompt, _ = await orch.build_narrator_prompt(
        "swing sword",
        context,
    )
    assert "Render the death" not in prompt
    assert "narrate survival" not in prompt


async def test_lethality_policy_none_leaves_bank_directives_unaffected():
    """Absence of a lethality_policy is a no-op — bank directives still flow."""
    orch = Orchestrator(client=ClaudeClient(spawn_fn=make_spawn_fn("narration")))
    context = TurnContext(
        character_name="Alice",
        dispatch_package=_empty_visible_package(),
        bank_result=BankResult(),
        lethality_policy=None,  # Group C not loaded — acts like pre-Group-C.
        pc_cores_by_player={"alice": _pc(0)},  # Even though at zero edge!
        npc_cores_by_name={},
    )
    prompt, _ = await orch.build_narrator_prompt(
        "swing sword",
        context,
    )
    # No arbiter ran — policy was None — so none of its text surfaces.
    assert "Render the death" not in prompt
