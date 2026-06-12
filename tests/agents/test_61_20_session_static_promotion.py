"""Story 61-20 — Reduce per-turn volatile-tail write VOLUME (option b).

RED-phase gate. 61-19 (merged, c92318b0) moved the ~9.7k volatile tail off the
1h tier onto 5m — it halved the *price* of the per-turn write but NOT its
*volume*: the same ~9.7k tokens are still written every turn (now at 1.25x
instead of 2x). 61-19 AC1 (<2k/turn), AC2 (<=$0.05/turn) and AC3 (flat at 50
turns) therefore remain open.

61-20 closes them via ADR-112 / Story 61-10 / 57-3 ZONE-PROMOTION: move the
genuinely SESSION-STATIC content currently riding the volatile Valley zone up
into the cache=True ``system_blocks[0]`` prefix, so it is written once and read
on every subsequent turn. Only the genuinely per-turn delta stays in the
volatile (5m) tail.

PROMOTION MECHANISM (verified 2026-05-30 against live code):
  - A section's destination block is governed by two things: its
    ``AttentionZone`` (orchestrator registration site) and its
    ``SectionBucket`` (``prompt_framework/bucket.py::STABLE_SECTION_NAMES``).
    Stable+System content lands in ``system_blocks[0]`` (cache=True, 1h);
    Valley/User content lands in the uncached follow-on blocks. This is the
    exact lever Story 57-3 pulled for ``genre_extraction``/``genre_keeper_
    monologue``/``genre_town`` — see
    ``test_cache_ttl_prefix_and_otel.py::test_promoted_genre_prose_lands_in_
    cached_system_block``. 61-20 pulls it for two more.

WHAT IS PROMOTABLE (verified):
  1. AVAILABLE CULTURES — section ``world_context`` (``orchestrator.py`` ~2076),
     today ``AttentionZone.Valley``. The per-world culture roster is
     session-static (it does not change turn-to-turn). PROMOTABLE.
  2. magic ``hard_limits`` — lives INSIDE section ``magic_context``
     (``orchestrator.py`` ~2134, ``magic/context_builder.py``), today
     ``AttentionZone.Valley``. The block is MIXED: the
     ``hard_limits:``/``allowed_sources:``/``world_knowledge:`` header is
     session-static, but ``active_ledger_for_<actor>:`` (sanity/notice bar
     values) CHANGES as magic is cast. Promoting the WHOLE section to 1h would
     re-introduce the exact cross-turn churn 61-19 fought. The fix MUST SPLIT
     the section: static header -> cached prefix, volatile ledger -> stays in
     the Valley tail. Tests below pin both halves so the split is forced.

WHAT IS **NOT** PROMOTABLE (blocking Delivery Finding — see session file):
  3. ``monster_manual`` — the story names it as a third promotion target, but
     in the Python impl the Monster Manual is NOT a cacheable prompt section.
     Per ``project_narrator_gaslighting_doctrine.md`` /
     ``server/dispatch/monster_manual_inject.py``, Manual entries are
     MATERIALIZED into ``snapshot.npcs`` as runtime ``Npc`` records and reach
     the prompt through the ``game_state`` (Valley) section — which is
     irreducibly volatile (NPCs/creatures/HP change every turn). It cannot be
     promoted to a 1h-cached prefix without breaking correctness. The
     ``test_volatile_game_state_not_promoted_*`` guard below pins that boundary.
     The architect must reconcile the story premise at spec-check; the <2k
     numeric (AC1) is achievable from the two valid promotions + the already-
     bounded snapshot (61-2), but NOT by promoting monster_manual.

Wiring discipline (server CLAUDE.md "No Source-Text Wiring Tests"): every test
drives a real turn through the production ``Orchestrator.run_narration_turn``
SDK path and inspects the captured wire payload's ``system_blocks``. No test
greps source.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

# Wires the tool adapters onto default_registry, matching the SDK path.
import sidequest.agents.tools  # noqa: F401

# Populate MAGIC_PLUGINS via import side effect (the tests/magic/ autouse
# fixture is not in scope under tests/agents/). MagicState.from_config needs
# the shipped plugins registered.
import sidequest.magic.plugins  # noqa: F401
from sidequest.magic.models import (
    HardLimit,
    LedgerBarSpec,
    WorldKnowledge,
    WorldMagicConfig,
)
from sidequest.magic.state import BarKey, MagicState
from tests.agents.fakes.fake_anthropic_sdk_client import (
    FakeAnthropicSdkClient,
    ScriptedResponse,
)

# Markers the tests grep for in the assembled wire payload (NOT source).
CULTURES_HEADER = "=== AVAILABLE CULTURES ==="
HARD_LIMIT_ID = "psionics_never_decisive"  # static — must ride the cached prefix
LEDGER_MARKER = "active_ledger_for_"  # volatile — must stay out of the prefix


def _end_turn(text: str = "ok") -> ScriptedResponse:
    return ScriptedResponse(
        text=text,
        stop_reason="end_turn",
        input_tokens=120,
        output_tokens=18,
        cached_input_read_tokens=0,
        cached_input_write_tokens=0,
        model="claude-sonnet-4-6",
    )


def _cultures_world_context() -> str:
    """A minimal AVAILABLE CULTURES block (the ``world_context`` payload).

    Shape mirrors ``server/dispatch/culture_context.py`` output: the
    ``=== AVAILABLE CULTURES ===`` header followed by roster lines. Content
    is session-static — identical every turn of a fixed world.
    """
    return (
        f"{CULTURES_HEADER}\n"
        "- Spindrift Compact (he/they) — orbital salvagers\n"
        "- Vautrin Hegemony (she/her) — corporate enforcers\n"
    )


def _magic_state_with_actor(actor_id: str) -> MagicState:
    """A minimal MagicState with one static hard_limit and one character whose
    ledger bars are mutable (so tests can change the volatile half between
    turns). Trimmed from the canonical coyote_star config in
    tests/magic/conftest.py.
    """
    config = WorldMagicConfig(
        world_slug="coyote_star",
        genre_slug="space_opera",
        allowed_sources=["innate", "item_based"],
        active_plugins=["innate_v1"],
        intensity=0.25,
        world_knowledge=WorldKnowledge(primary="classified", local_register="folkloric"),
        visibility={"primary": "feared", "local_register": "dismissed"},
        hard_limits=[HardLimit(id=HARD_LIMIT_ID, description="psionics never decide a scene")],
        cost_types=["sanity", "notice"],
        ledger_bars=[
            LedgerBarSpec(
                id="sanity",
                scope="character",
                direction="down",
                range=(0.0, 1.0),
                threshold_low=0.40,
                starts_at_chargen=1.0,
            ),
            LedgerBarSpec(
                id="notice",
                scope="character",
                direction="up",
                range=(0.0, 1.0),
                threshold_high=0.75,
                starts_at_chargen=0.0,
            ),
        ],
        narrator_register="x",
    )
    state = MagicState.from_config(config)
    state.add_character(actor_id)
    return state


# ===========================================================================
# AC1 — AVAILABLE CULTURES (world_context) promoted into the cached prefix
# ===========================================================================


@pytest.mark.asyncio
async def test_available_cultures_promoted_to_cached_prefix(
    simple_turn_context,
) -> None:
    """AC1 (promotion). The session-static AVAILABLE CULTURES roster MUST ride
    the cache-marked ``system_blocks[0]`` block, NOT an uncached follow-on
    block and NOT the per-turn user message.

    Pre-fix the ``world_context`` section is registered with
    ``AttentionZone.Valley`` (``orchestrator.py`` ~2076) and the default User
    bucket, so the roster lands in the volatile tail and is re-written every
    turn. Post-fix (zone -> Early/Primacy + name on STABLE_SECTION_NAMES, the
    Story 57-3 pattern) the header lands in the cached prefix and amortizes.
    """
    ctx = replace(simple_turn_context, world_context=_cultures_world_context())
    fake = FakeAnthropicSdkClient(responses=[_end_turn()])
    from sidequest.agents.orchestrator import Orchestrator

    orch = Orchestrator(client=fake)
    await orch.run_narration_turn("look around", ctx)

    assert len(fake.recorded_requests) == 1
    request = fake.recorded_requests[0]
    cached = request.system_blocks[0].text
    volatile = "\n".join(b.text for b in request.system_blocks[1:])
    user_msg = "\n".join(m.content for m in request.messages if m.role == "user")

    assert CULTURES_HEADER in cached, (
        f"{CULTURES_HEADER!r} must land in the cache-marked system_blocks[0] so "
        f"the session-static culture roster is written once and amortizes. "
        f"Found in volatile system_blocks[1:]: {CULTURES_HEADER in volatile}; "
        f"found in user message: {CULTURES_HEADER in user_msg}. Promote "
        f"'world_context' (zone Valley->Early + STABLE_SECTION_NAMES)."
    )


@pytest.mark.asyncio
async def test_available_cultures_absent_from_volatile_tail(
    simple_turn_context,
) -> None:
    """AC1 (displacement — the volume half). Once promoted, the AVAILABLE
    CULTURES roster must NO LONGER appear in any uncached (cache=False)
    follow-on block. This is the per-turn write-volume reduction: the static
    roster leaves the ~9.7k volatile tail. Asserting displacement (not a raw
    <2k token count) keeps the test robust to the residual snapshot size,
    which is bounded separately by 61-2.
    """
    ctx = replace(simple_turn_context, world_context=_cultures_world_context())
    fake = FakeAnthropicSdkClient(responses=[_end_turn()])
    from sidequest.agents.orchestrator import Orchestrator

    orch = Orchestrator(client=fake)
    await orch.run_narration_turn("look around", ctx)

    request = fake.recorded_requests[0]
    for i, block in enumerate(request.system_blocks[1:], start=1):
        assert CULTURES_HEADER not in block.text, (
            f"{CULTURES_HEADER!r} still rides volatile system_blocks[{i}] "
            f"(cache={block.cache}); the per-turn write volume is not reduced. "
            f"The static roster must move to the cached prefix, not stay in the "
            f"tail."
        )


# ===========================================================================
# AC1 + AC4 — magic hard_limits promoted, but the volatile ledger SPLIT off
# ===========================================================================


@pytest.mark.asyncio
async def test_magic_hard_limits_promoted_to_cached_prefix(
    simple_turn_context,
) -> None:
    """AC1 (promotion). The session-static magic ``hard_limits`` header MUST
    ride the cache-marked ``system_blocks[0]`` block.

    Pre-fix the entire ``magic_context`` section is ``AttentionZone.Valley``
    (``orchestrator.py`` ~2134), so even the static hard_limits header is
    re-written every turn. Post-fix the static header is promoted; the
    volatile ledger is split off (next test).
    """
    actor = "Kael"
    ctx = replace(
        simple_turn_context,
        character_name=actor,
        magic_state=_magic_state_with_actor(actor),
    )
    fake = FakeAnthropicSdkClient(responses=[_end_turn()])
    from sidequest.agents.orchestrator import Orchestrator

    orch = Orchestrator(client=fake)
    await orch.run_narration_turn("focus my will", ctx)

    request = fake.recorded_requests[0]
    cached = request.system_blocks[0].text
    volatile = "\n".join(b.text for b in request.system_blocks[1:])

    assert "hard_limits" in cached and HARD_LIMIT_ID in cached, (
        f"The static magic hard_limits header (incl. {HARD_LIMIT_ID!r}) must "
        f"land in the cache-marked system_blocks[0]. Found 'hard_limits' in "
        f"volatile blocks: {'hard_limits' in volatile}. Promote the static "
        f"half of 'magic_context' (split from the per-actor ledger)."
    )


@pytest.mark.asyncio
async def test_volatile_magic_ledger_stays_out_of_cached_prefix(
    simple_turn_context,
) -> None:
    """AC4 (the load-bearing split guard). The per-actor ledger
    (``active_ledger_for_<actor>:`` + sanity/notice bar values) CHANGES as
    magic is cast — it is volatile and MUST NOT be promoted into the cached
    1h prefix, or it re-creates the exact cross-turn churn 61-19 killed.

    Combined with ``test_magic_hard_limits_promoted_to_cached_prefix`` above,
    this forces the implementer to SPLIT ``magic_context`` rather than promote
    it wholesale: static header -> cached prefix, ledger -> volatile tail.
    """
    actor = "Kael"
    ctx = replace(
        simple_turn_context,
        character_name=actor,
        magic_state=_magic_state_with_actor(actor),
    )
    fake = FakeAnthropicSdkClient(responses=[_end_turn()])
    from sidequest.agents.orchestrator import Orchestrator

    orch = Orchestrator(client=fake)
    await orch.run_narration_turn("focus my will", ctx)

    cached = fake.recorded_requests[0].system_blocks[0].text
    assert LEDGER_MARKER not in cached, (
        f"The volatile per-actor ledger ({LEDGER_MARKER!r}) must NOT ride the "
        f"cache-marked system_blocks[0] — its bar values change every time "
        f"magic is cast, so a 1h write on it is invalidated next turn (the "
        f"73%-of-cost regression 61-19 fought). Keep the ledger in the Valley "
        f"tail; promote only the static hard_limits header."
    )


@pytest.mark.asyncio
async def test_cached_prefix_byte_stable_across_turns_with_changing_ledger(
    simple_turn_context,
) -> None:
    """AC3 / AC4 (the economic proof). With magic state present and the actor's
    ledger CHANGING between turns, the cache-marked ``system_blocks[0]`` MUST
    stay byte-identical across turns.

    This is the whole point of the promotion: a 1h write only pays off if the
    cached prefix is stable and re-read across turns. If the implementer
    promotes the static hard_limits correctly (and leaves the ledger volatile),
    the prefix is stable and amortizes -> flat per-turn cost. If they promote
    the volatile ledger too, this test fails because the prefix moves every
    turn -> the churn returns. Mirrors
    ``test_compose_split_system_prefix_byte_identical_across_3_turns`` but with
    a deliberately mutating magic ledger as the adversary.
    """
    actor = "Kael"
    state = _magic_state_with_actor(actor)
    fake = FakeAnthropicSdkClient(responses=[_end_turn("t1"), _end_turn("t2"), _end_turn("t3")])
    from sidequest.agents.orchestrator import Orchestrator

    orch = Orchestrator(client=fake)

    for n in range(3):
        # Mutate the volatile ledger between turns (as casting magic would).
        state.set_bar_value(
            BarKey(scope="character", owner_id=actor, bar_id="sanity"),
            1.0 - 0.1 * n,
        )
        ctx = replace(
            simple_turn_context,
            character_name=actor,
            turn_number=n,
            magic_state=state,
        )
        await orch.run_narration_turn(f"channel attempt {n}", ctx)

    assert len(fake.recorded_requests) == 3
    prefixes = [r.system_blocks[0].text for r in fake.recorded_requests]
    digests = {hashlib.sha256(p.encode("utf-8")).hexdigest() for p in prefixes}
    assert len(digests) == 1, (
        "The cache-marked system_blocks[0] MUST be byte-identical across turns "
        "even while the magic ledger changes. Got "
        f"{len(digests)} distinct prefixes — the volatile ledger leaked into "
        "the cached prefix, so the 1h write is invalidated every turn (the "
        "61-19 churn, re-created). Split the ledger out of the promoted block."
    )


# ===========================================================================
# Boundary guard — monster_manual / game_state is NOT promotable
# ===========================================================================


@pytest.mark.asyncio
async def test_volatile_game_state_not_promoted_to_cached_prefix(
    simple_turn_context,
) -> None:
    """Boundary guard (the blocking Delivery Finding in executable form).

    The story names ``monster_manual`` as a promotion target, but in the
    Python impl the Manual is materialized into ``snapshot.npcs`` and reaches
    the prompt via the ``game_state`` (Valley) section — irreducibly volatile.
    A naive reading of the story might try to promote that content into the
    cached prefix. This guard pins the correct boundary: ``game_state`` content
    changes between turns and MUST stay OUT of the byte-stable cached prefix.
    """
    fake = FakeAnthropicSdkClient(responses=[_end_turn("t1"), _end_turn("t2")])
    from sidequest.agents.orchestrator import Orchestrator

    orch = Orchestrator(client=fake)

    summaries = [
        "The cavern is quiet; a goblin scout watches from the ledge.",
        "The cavern echoes; the goblin scout has fled and a troll blocks the exit.",
    ]
    for n, summary in enumerate(summaries):
        ctx = replace(simple_turn_context, turn_number=n, state_summary=summary)
        await orch.run_narration_turn(f"survey the room {n}", ctx)

    assert len(fake.recorded_requests) == 2
    # The changing creature roster must NOT be in the cached prefix...
    for n, req in enumerate(fake.recorded_requests):
        assert "goblin scout" not in req.system_blocks[0].text or n == 0, (
            "volatile game_state content (monster_manual-materialized NPCs) "
            "leaked into the cached prefix — it must stay in the Valley tail."
        )
    # ...and the cached prefix must stay byte-identical despite the roster change.
    prefixes = [r.system_blocks[0].text for r in fake.recorded_requests]
    digests = {hashlib.sha256(p.encode("utf-8")).hexdigest() for p in prefixes}
    assert len(digests) == 1, (
        "The cached prefix moved when only the volatile game_state changed — "
        "monster_manual/game_state content must NOT be promoted (it is not "
        "session-static). See the session Delivery Finding."
    )
