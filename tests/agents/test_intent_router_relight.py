"""Tests for the Intent Router relight intent (Task 4.2, light-darkness spec).

The light & darkness survival clock burns the ``light`` pool one unit per
time-advancing turn (Task 3.2 — server-INJECTED, undodgeable). RELIGHTING a
torch, by contrast, is a deliberate PLAYER intent: "I light a fresh torch."
That intent must come through the decomposer/router as an
``environment_clock`` dispatch carrying ``params["mode"]=="relight"`` — the
same handler the injection path uses, but with the relight branch.

These tests assert the ROUTER CONTRACT, not live LLM behavior:

* The router's system prompt documents the relight intent so a torch-lighting
  action routes to ``environment_clock`` with ``mode=relight`` (behavioral
  observation of the producer's sent ``system`` prompt — the same shape as
  ``test_intent_router_prompt_documents_subsystem_params_contract``, NOT a
  source-text grep).
* When the stubbed LLM emits an ``environment_clock``/``mode=relight``
  dispatch, the router parses it into a schema-valid ``DispatchPackage`` whose
  ``per_player[*].dispatch`` carries that subsystem + mode intact.
* The dispatch is confidence-gated (a deliberate intent, scored < 1.0 — unlike
  the forced confidence=1.0 of the server-injected burn).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def haiku_response_relight() -> dict:
    """Synthetic SDK-Haiku tool input — a relight-shaped dispatch.

    Per ADR-102 the router consumes the ``tool_use`` block's structured
    ``input`` dict, so this fixture is a dict (not a JSON string). It mirrors
    what Haiku should emit when the player lights a fresh torch: an
    ``environment_clock`` dispatch with ``params["mode"]=="relight"`` at a
    deliberate-intent confidence (0.8 — below the forced 1.0 of the injected
    burn, above the 0.6 gate so the engine engages).
    """
    return {
        "turn_id": "turn-relight",
        "per_player": [
            {
                "player_id": "player:Delver",
                "raw_action": "I light a fresh torch.",
                "resolved": [],
                "dispatch": [
                    {
                        "subsystem": "environment_clock",
                        "params": {"mode": "relight", "character_name": "Delver"},
                        "depends_on": [],
                        "idempotency_key": "idem:turn-relight:delver:0",
                        "confidence": 0.8,
                        "visibility": {
                            "visible_to": "all",
                            "perception_fidelity": {},
                            "secrets_for": [],
                            "redact_from_narrator_canonical": False,
                        },
                    }
                ],
                "lethality": [],
                "narrator_instructions": [],
            }
        ],
        "cross_player": [],
        "confidence_global": 0.8,
    }


@pytest.fixture
def haiku_response_quiet_turn() -> dict:
    """Quiet-turn dispatch — empty per_player + cross_player, valid schema."""
    return {
        "turn_id": "turn-quiet",
        "per_player": [],
        "cross_player": [],
        "confidence_global": 1.0,
    }


def _make_mock_router_llm(response: dict) -> AsyncMock:
    """Build a mocked router LLM adapter (ADR-102 tool-use shape)."""
    mock = AsyncMock()
    mock.emit_tool = AsyncMock(return_value=response)
    return mock


@pytest.mark.asyncio
async def test_router_emits_environment_clock_relight_dispatch(
    haiku_response_relight: dict,
) -> None:
    """The router contract: given a stubbed LLM emitting a relight intent, the
    package's per_player[*].dispatch carries subsystem=environment_clock with
    params['mode']=='relight' intact.
    """
    from sidequest.agents.intent_router import IntentRouter
    from sidequest.protocol.dispatch import DispatchPackage

    llm = _make_mock_router_llm(haiku_response_relight)
    router = IntentRouter(llm=llm)

    pkg = await router.decompose(
        action="I light a fresh torch.",
        state_summary={"scene": "a dark cavern, the last torch guttering"},
    )

    assert isinstance(pkg, DispatchPackage)
    assert len(pkg.per_player) == 1
    dispatches = pkg.per_player[0].dispatch
    assert len(dispatches) == 1, "the relight dispatch must survive intact"
    relight = dispatches[0]
    assert relight.subsystem == "environment_clock", (
        f"a torch-lighting intent must route to environment_clock; got {relight.subsystem}"
    )
    assert relight.params.get("mode") == "relight", (
        "the relight dispatch must carry params['mode']=='relight' so the "
        "environment_clock handler takes the relight branch (consume torch, "
        f"light=max, clear darkness penalty); got params={relight.params}"
    )


@pytest.mark.asyncio
async def test_router_relight_is_confidence_gated_not_forced(
    haiku_response_relight: dict,
) -> None:
    """Relight is a DELIBERATE intent: the router scores its confidence from the
    LLM output (here 0.8), NOT forced to 1.0 like the server-injected burn.

    This pins that the per-dispatch confidence round-trips from the LLM
    emission so ``run_dispatch_bank`` can gate it against the 0.6 default
    threshold (engage at/above, degrade to a narrator hint below).
    """
    from sidequest.agents.intent_router import IntentRouter

    llm = _make_mock_router_llm(haiku_response_relight)
    router = IntentRouter(llm=llm)

    pkg = await router.decompose(
        action="I light a fresh torch.",
        state_summary={},
    )

    relight = pkg.per_player[0].dispatch[0]
    assert relight.confidence == pytest.approx(0.8), (
        "the relight dispatch confidence must round-trip from the LLM output "
        "(a deliberate intent is gated, not forced to 1.0 like the injected burn)"
    )
    assert relight.confidence < 1.0, (
        "a deliberate relight intent must NOT carry the forced confidence=1.0 "
        "of the server-injected, undodgeable burn tick"
    )


@pytest.mark.asyncio
async def test_router_prompt_documents_relight_intent(
    haiku_response_quiet_turn: dict,
) -> None:
    """The router must TELL the model that lighting/snuffing a torch routes to
    ``environment_clock`` with ``mode=relight``, or Haiku has no vocabulary to
    emit the dispatch.

    Behavioral assertion on the ``system`` prompt the router actually sends —
    the same shape as ``test_intent_router_prompt_documents_subsystem_params
    _contract`` — NOT a source-text grep of production files.
    """
    from sidequest.agents.intent_router import IntentRouter

    llm = _make_mock_router_llm(haiku_response_quiet_turn)
    router = IntentRouter(llm=llm)

    await router.decompose(action="I light a fresh torch.", state_summary={})

    system = llm.emit_tool.await_args.kwargs["system"]
    assert "environment_clock" in system, (
        "router prompt must name the environment_clock subsystem so a "
        "torch-lighting action has a dispatch target"
    )
    assert '"mode": "relight"' in system or "mode=relight" in system, (
        "router prompt must document that lighting a torch carries "
        "params['mode']=='relight' — without this the handler never takes the "
        "relight branch"
    )
