"""Regression: the dispatch bank runs ONCE per turn (playtest #G5 / #456).

Before this fix the bank ran twice per turn with complementary-incomplete
contexts:

  * the pre-narrator pass (``intent_router_pass``) carried snapshot / pack /
    player_name / dungeon_store but NOT ``npc_pool`` — so ``run_npc_agency``
    died with ``missing 1 required keyword-only argument: 'npc_pool'``;
  * the orchestrator re-ran the bank with ONLY ``npc_pool`` — so
    ``run_movement_dispatch`` / ``run_scenario_clue_dispatch`` died on a
    missing ``snapshot`` / ``player_name``.

Every dispatch the router selected failed on exactly one of the two paths
(playtest 2026-05-28 Glenross: npc_agency t2/t5, movement t3, scenario_clue
t4). The fix consolidates to a SINGLE bank run in the pre-narrator pass with
a complete context (``npc_pool`` sourced from the snapshot); the orchestrator
consumes the stashed ``BankResult`` and never re-runs the bank — re-running
would engage every engine twice (a PC moves twice, a clue is consumed twice).

These tests pin both halves of the contract.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from sidequest.protocol.dispatch import (
    DispatchPackage,
    NarratorDirective,
    PlayerDispatch,
    SubsystemDispatch,
    VisibilityTag,
)

pytestmark = pytest.mark.asyncio


def _open_viz() -> VisibilityTag:
    return VisibilityTag(visible_to="all")


def _npc_agency_package() -> DispatchPackage:
    return DispatchPackage(
        turn_id="t-npc",
        per_player=[
            PlayerDispatch(
                player_id="player:Vyvyan",
                raw_action="I study Old Tam's face — eager, or reluctant?",
                dispatch=[
                    SubsystemDispatch(
                        subsystem="npc_agency",
                        params={"npc_name": "Old Tam", "situation": "the body in the burn"},
                        idempotency_key="vyvyan_read_old_tam",
                        confidence=1.0,
                        visibility=_open_viz(),
                    )
                ],
            )
        ],
        confidence_global=1.0,
    )


def _snapshot_with_npc(npc_name: str) -> Any:
    from sidequest.game.npc_pool import NpcPoolMember
    from sidequest.game.session import GameSnapshot

    return GameSnapshot(
        genre_slug="tea_and_murder",
        world_slug="glenross",
        encounter=None,
        player_seats={"player:Vyvyan": "Vyvyan"},
        npc_pool=[NpcPoolMember(name=npc_name, role="publican", drawn_from="world_authored")],
    )


async def test_pre_narrator_pass_engages_npc_agency_with_pool_from_snapshot() -> None:
    """#G5: the pre-narrator pass sources ``npc_pool`` from the snapshot, so
    ``run_npc_agency`` engages instead of dying on the missing kwarg.

    Asserts the returned BankResult carries the NPC's disposition directive
    (proving the handler ran with a populated pool) and records NO dispatch
    error (the old ``missing 'npc_pool'`` TypeError is gone)."""
    from sidequest.server.intent_router_pass import (
        execute_intent_router_pre_narrator_pass,
    )

    snap = _snapshot_with_npc("Old Tam")
    package = _npc_agency_package()
    router = MagicMock()
    router.decompose = AsyncMock(return_value=package)
    # _build_state_summary reads pack.rules; a bare object would crash. Use a
    # MagicMock pack — the router is stubbed so the summary content is unused.
    pack = MagicMock()
    pack.rules = None

    returned_pkg, bank_result = await execute_intent_router_pre_narrator_pass(
        intent_router=router,
        snapshot=snap,
        pack=pack,
        action="I study Old Tam's face — eager, or reluctant?",
        player_name="Vyvyan",
    )

    assert returned_pkg is package
    # No dispatch failed — the npc_pool kwarg reached the handler.
    assert bank_result.errors == [], (
        f"npc_agency dispatch failed (the #G5 bug): {bank_result.errors}"
    )
    # The handler engaged and produced the disposition directive.
    out = bank_result.outputs_by_key["vyvyan_read_old_tam"]
    assert out.data.get("npc_name") == "Old Tam"
    assert any("Old Tam" in d.payload for d in bank_result.directives), (
        "npc_agency produced no narrator directive — handler did not engage"
    )


def _snapshot_with_roster_npc(npc_name: str, *, disposition: int = 0) -> Any:
    """A snapshot where the NPC is in the AUTHORED roster (snapshot.npcs) and
    NOT in npc_pool — the coyote_star crew / Old Tam shape (playtest #C1)."""
    from sidequest.game.creature_core import CreatureCore, HpPool
    from sidequest.game.disposition import Disposition
    from sidequest.game.session import GameSnapshot, Npc

    npc = Npc(
        core=CreatureCore(
            name=npc_name,
            description="An NPC.",
            personality="Neutral.",
            hp=HpPool(current=10, max=10, base_max=10),
        ),
        disposition=Disposition(disposition),
    )
    return GameSnapshot(
        genre_slug="tea_and_murder",
        world_slug="glenross",
        encounter=None,
        player_seats={"player:Vyvyan": "Vyvyan"},
        npcs=[npc],
        npc_pool=[],  # deliberately empty — the roster is the only source
    )


async def test_pre_narrator_pass_engages_npc_agency_for_roster_npc_not_in_pool() -> None:
    """#C1: a disposition read on an authored roster NPC (snapshot.npcs, NOT in
    npc_pool) must engage. This pins the full wiring — the pre-narrator pass
    threads ``npcs`` into the bank context, the bank signature-filters it into
    ``run_npc_agency``, and the roster path produces the disposition directive.
    Before the fix the pool-only lookup returned ``npc_not_registered`` and the
    subsystem never fired for the game's primary NPCs."""
    from sidequest.server.intent_router_pass import (
        execute_intent_router_pre_narrator_pass,
    )

    snap = _snapshot_with_roster_npc("Old Tam", disposition=40)  # friendly
    assert snap.npc_pool == []  # the NPC is ONLY in the roster
    package = _npc_agency_package()
    router = MagicMock()
    router.decompose = AsyncMock(return_value=package)
    pack = MagicMock()
    pack.rules = None

    _returned_pkg, bank_result = await execute_intent_router_pre_narrator_pass(
        intent_router=router,
        snapshot=snap,
        pack=pack,
        action="I study Old Tam's face — eager, or reluctant?",
        player_name="Vyvyan",
    )

    assert bank_result.errors == [], f"npc_agency dispatch failed: {bank_result.errors}"
    out = bank_result.outputs_by_key["vyvyan_read_old_tam"]
    assert out.data.get("source") == "npcs_roster", (
        f"roster NPC did not resolve via the roster path: {out.data}"
    )
    assert out.data.get("disposition") == "friendly"
    assert any("Old Tam" in d.payload for d in bank_result.directives), (
        "npc_agency produced no directive for a roster NPC — wiring break"
    )


async def test_build_narrator_prompt_fails_loud_when_bank_result_missing() -> None:
    """Consolidation invariant: a present ``dispatch_package`` means the
    pre-narrator pass ran and stashed its ``BankResult``. ``build_narrator_prompt``
    must NOT re-run the bank to recover a missing result — that would re-engage
    every engine. Per No Silent Fallbacks, it fails loud instead."""
    from sidequest.agents.orchestrator import Orchestrator, TurnContext
    from tests.agents.test_orchestrator import make_canned_client

    orch = Orchestrator(client=make_canned_client("narration"))
    ctx = TurnContext(
        character_name="Vyvyan",
        dispatch_package=_npc_agency_package(),
        bank_result=None,  # pre-narrator pass result NOT stashed — wiring break
    )

    with pytest.raises(RuntimeError, match="bank_result is None"):
        await orch.build_narrator_prompt("I study Old Tam's face.", ctx)


async def test_orchestrator_consumes_stashed_result_without_rerunning_bank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The orchestrator must consume the stashed ``BankResult`` and NEVER call
    ``run_dispatch_bank`` itself (single-dispatch). We monkeypatch the bank
    callable in the subsystems module to explode if invoked, then build a
    prompt with a pre-computed result and assert its directive lands."""
    import sidequest.agents.subsystems as _subsystems_mod
    from sidequest.agents.orchestrator import Orchestrator, TurnContext
    from sidequest.agents.subsystems import BankResult
    from tests.agents.test_orchestrator import make_canned_client

    async def _explode(*_a, **_k):  # pragma: no cover — must NOT be called
        raise AssertionError("orchestrator re-ran run_dispatch_bank (double-dispatch)")

    monkeypatch.setattr(_subsystems_mod, "run_dispatch_bank", _explode)

    orch = Orchestrator(client=make_canned_client("narration"))
    bank_result = BankResult(
        directives=[
            NarratorDirective(
                kind="must_narrate",
                payload="zzz-old-tam-disposition-zzz",
                visibility=_open_viz(),
            )
        ]
    )
    ctx = TurnContext(
        character_name="Vyvyan",
        dispatch_package=_npc_agency_package(),
        bank_result=bank_result,
    )

    prompt_text, _registry = await orch.build_narrator_prompt("I study Old Tam's face.", ctx)

    assert "zzz-old-tam-disposition-zzz" in prompt_text


async def test_redacted_directive_filtered_from_prompt_via_visibility() -> None:
    """The single bank run executes on the FULL package, so the orchestrator
    must strip directives flagged ``redact_from_narrator_canonical`` by their
    visibility tag (MP perception firewall) before they reach the prompt."""
    from sidequest.agents.orchestrator import Orchestrator, TurnContext
    from sidequest.agents.subsystems import BankResult
    from tests.agents.test_orchestrator import make_canned_client

    secret_viz = VisibilityTag(
        visible_to=["player:Alice"],
        secrets_for=["player:Alice"],
        redact_from_narrator_canonical=True,
    )
    bank_result = BankResult(
        directives=[
            NarratorDirective(
                kind="must_narrate",
                payload="zzz-PUBLIC-visible-zzz",
                visibility=_open_viz(),
            ),
            NarratorDirective(
                kind="canonical_only_do_not_reveal_to_others",
                payload="zzz-SECRET-redacted-zzz",
                visibility=secret_viz,
            ),
        ]
    )
    orch = Orchestrator(client=make_canned_client("narration"))
    ctx = TurnContext(
        character_name="Vyvyan",
        dispatch_package=_npc_agency_package(),
        bank_result=bank_result,
    )

    prompt_text, _registry = await orch.build_narrator_prompt("I study Old Tam's face.", ctx)

    assert "zzz-PUBLIC-visible-zzz" in prompt_text
    assert "zzz-SECRET-redacted-zzz" not in prompt_text
