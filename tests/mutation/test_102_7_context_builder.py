"""Story 102-7 RED — the narrator SEES mutations (Plan 2 §5.4 context block).

THE GAP: the mutation engine resolves uses (PR #781) and 102-7's sibling
tests force the dispatch routes — but the narrator's prompt never mentions
what mutations a PC owns, what they cost, or how many uses remain. A narrator
that cannot see the surface cannot call ``use_mutation`` or honor its limits;
it improvises. The magic system solved this with
``sidequest/magic/context_builder.py`` injected at the orchestrator's
``magic_state`` chokepoint (orchestrator.py ~2241); Plan 2 §5.4 mandates the
bespoke sibling: ``sidequest/mutation/context_builder.py`` with the same
static/volatile split (ADR-009/112 prompt-zone discipline).

CONTRACT PINNED (mirrors build_magic_static_block / build_magic_volatile_block):
  * ``build_mutation_static_block(mutation_state, catalog)`` — session-static:
    owned mutations with effect summaries. Empty string when state is None or
    owns nothing (absence costs zero tokens — the 61-12 lesson).
  * ``build_mutation_volatile_block(mutation_state, catalog)`` — per-turn:
    MP remaining + usage counters.
  * ``TurnContext.mutation_state`` exists, and ``build_narrator_prompt``
    includes the block iff it is populated — non-mutation worlds never pay.
"""

from __future__ import annotations

import json
from typing import Any

from sidequest.mutation.models import (
    MpEconomy,
    MutationCatalog,
    NegativeMutationDef,
    PositiveMutationDef,
    StigmaTables,
)
from sidequest.mutation.state import (
    CharacterMutationState,
    MutationState,
    UsageCounter,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _catalog() -> MutationCatalog:
    return MutationCatalog(
        mp_economy=MpEconomy(mutant_classes=["Mutant"]),
        stigma=StigmaTables(
            body_part=["a"] * 6,
            nature=["b"] * 6,
            flavor=["c"] * 12,
        ),
        negatives=[
            NegativeMutationDef(
                id="negative/frail", name="Frail", roll_range=(1, 100), effect="frail"
            )
        ],
        positives=[
            PositiveMutationDef(
                id="exotic/acid_spit",
                name="Acid Spit",
                category="exotic",
                effect="spit acid",
                strain_cost=2,
                usage="per_scene",
            ),
        ],
    )


def _state() -> MutationState:
    return MutationState(
        characters={
            "Rux": CharacterMutationState(
                mp_remaining=3,
                positive_ids=["exotic/acid_spit"],
                negative_ids=["negative/frail"],
                usage={"exotic/acid_spit": UsageCounter(period="per_scene", used=1)},
            )
        }
    )


# ---------------------------------------------------------------------------
# Static block — owned surface, effects, costs
# ---------------------------------------------------------------------------


def test_static_block_lists_owned_mutations_with_effects() -> None:
    from sidequest.mutation.context_builder import build_mutation_static_block

    block = build_mutation_static_block(mutation_state=_state(), catalog=_catalog())

    assert "Acid Spit" in block, "the narrator must see the owned positive by name"
    assert "Frail" in block, "negatives shape the fiction too — list them"
    assert "spit acid" in block, "effect summary, so the narrator narrates the real power"


def test_static_block_empty_when_state_none() -> None:
    from sidequest.mutation.context_builder import build_mutation_static_block

    assert build_mutation_static_block(mutation_state=None, catalog=_catalog()) == ""


def test_static_block_empty_when_no_characters_seeded() -> None:
    from sidequest.mutation.context_builder import build_mutation_static_block

    assert (
        build_mutation_static_block(
            mutation_state=MutationState(), catalog=_catalog()
        )
        == ""
    ), "an empty state must cost zero prompt tokens (the 61-12 lesson)"


# ---------------------------------------------------------------------------
# Volatile block — MP + usage, the per-turn truth
# ---------------------------------------------------------------------------


def test_volatile_block_carries_mp_and_usage() -> None:
    from sidequest.mutation.context_builder import build_mutation_volatile_block

    block = build_mutation_volatile_block(mutation_state=_state(), catalog=_catalog())

    assert "3" in block, "MP remaining must be visible (mp_remaining=3)"
    assert "Acid Spit" in block or "exotic/acid_spit" in block
    # per_scene, used once: the narrator must be able to see the limit state
    # (exact prose is Dev's; the FACT of a consumed use must be present).
    assert "1" in block


def test_volatile_block_empty_when_state_none() -> None:
    from sidequest.mutation.context_builder import build_mutation_volatile_block

    assert build_mutation_volatile_block(mutation_state=None, catalog=_catalog()) == ""


# ---------------------------------------------------------------------------
# Wiring — the orchestrator chokepoint (mirrors tests/magic precedent)
# ---------------------------------------------------------------------------


def _make_canned_client():
    """ClaudeClient whose subprocess always returns a minimal canned response
    (verbatim from tests/magic/test_narrator_pre_prompt.py)."""
    from sidequest.agents.claude_client import ClaudeClient

    async def spawn_fn(command: str, *args: str, env: Any = None, **kwargs: Any):
        class FakeProcess:
            returncode = 0

            async def communicate(self):
                payload = {
                    "result": "**The Silence**\n\nNothing stirs.\n\n```game_patch\n{}\n```",
                    "session_id": "test-session-001",
                    "usage": {"input_tokens": 10, "output_tokens": 20},
                }
                return json.dumps(payload).encode(), b""

            def kill(self):
                pass

            async def wait(self):
                return 0

        return FakeProcess()

    return ClaudeClient(spawn_fn=spawn_fn)


async def test_narrator_pre_prompt_contains_mutation_context_when_state_present():
    """THE WIRING TEST: ``build_narrator_prompt`` includes the mutation block
    when ``TurnContext.mutation_state`` is populated — the narrator can only
    honor crunch it can see. Today TurnContext has no mutation_state field and
    the prompt carries nothing."""
    from sidequest.agents.orchestrator import Orchestrator, TurnContext

    orch = Orchestrator(client=_make_canned_client())
    context = TurnContext(
        character_name="Rux",
        mutation_state=_state(),
        mutation_catalog=_catalog(),
    )
    prompt, _ = await orch.build_narrator_prompt("I bare my fangs", context)
    assert "Acid Spit" in prompt, (
        "the narrator prompt must carry the owned-mutation surface when "
        "mutation_state is populated"
    )


async def test_narrator_pre_prompt_omits_mutation_context_when_state_absent():
    """Non-mutation worlds never pay: no mutation_state, no block (the same
    single-chokepoint economics as the 61-12 magic gate)."""
    from sidequest.agents.orchestrator import Orchestrator, TurnContext

    orch = Orchestrator(client=_make_canned_client())
    context = TurnContext(character_name="kael")
    prompt, _ = await orch.build_narrator_prompt("look around", context)
    assert "Acid Spit" not in prompt
    assert "MUTATION" not in prompt, (
        "a world with no mutation surface must not carry a mutation section"
    )
