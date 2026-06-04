"""RED tests for Story 60-6 — stable-prefix byte-drift under live state mutations.

Story 60-3 measured system_blocks[0] as byte-stable (digest ``7c926d96`` across
synthetic turns of one captured request). That measurement was against an
isolated SDK replay, not a live session with real state mutations.

60-6 re-opens the hypothesis with empirical evidence: drive >=5 turns through
the real prompt-builder + SDK path with *progressive state mutations*
(NPC roster changes, game-state rewrites, trope context, encounter
transitions) and verify the ``system_blocks[0]`` digest holds.

If the digest is stable: 60-3 confirmed — proceed with 60-4 (cache-control
fix). If it drifts: 60-3 disproved — reopen Epic 60 investigation.

ACs covered:
  AC-1  OTEL + watcher digest emitted on each turn
  AC-2  Digest byte-identical across >=5 mutating turns
  AC-3  State mutations (NPC, location, trope) do not cause drift
  AC-4  Per-turn cost emitted on OTEL span (baseline capture)
  AC-5  No silent fallbacks — digest field present and valid every turn
  AC-6  Cross-check: emitted digest matches actual system_blocks hash
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from typing import Any

import pytest

import sidequest.agents.tools  # noqa: F401
from sidequest.agents.orchestrator import Orchestrator, TurnContext
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.telemetry.watcher_hub import WatcherHub, watcher_hub
from tests._helpers.doubles import FakeSocket
from tests.agents.fakes.fake_anthropic_sdk_client import (
    FakeAnthropicSdkClient,
    ScriptedResponse,
)

TURN_COUNT = 5

# Story 61-20: world_context (AVAILABLE CULTURES) is a session-static promoted
# field — set once, constant across the session, rides the cached prefix.
_AVAILABLE_CULTURES = "AVAILABLE CULTURES: Dwarven (Khazad dialect), Human (Common), Elven (Sylvan)"


def _end_turn(text: str) -> ScriptedResponse:
    return ScriptedResponse(
        text=text,
        stop_reason="end_turn",
        input_tokens=500,
        output_tokens=80,
        cached_input_read_tokens=11000,
        cached_input_write_tokens=500,
        model="claude-sonnet-4-6",
    )


def _mutating_contexts(base: TurnContext) -> list[TurnContext]:
    """Build 5 TurnContexts with progressively mutating state.

    Each turn adds or changes User-bucket fields (npc_pool, state_summary,
    active_trope_summary, pending_trope_context) that should land in the user
    message — NOT in system_blocks[0]. If any of these mutations leak into the
    cached prefix, the byte-stability guarantee is broken.

    Story 61-20: ``world_context`` (the AVAILABLE CULTURES roster) is now a
    session-static PROMOTED field — it rides the cache-marked system prefix
    (Early + STABLE_SECTION_NAMES). It is therefore set ONCE on the base
    context here and held CONSTANT across all five turns (its real lifecycle:
    fixed at connect for the life of the session). It legitimately lives in
    system_blocks[0]; the test still proves the genuinely-volatile fields above
    do not drift the prefix.
    """
    base = replace(base, world_context=_AVAILABLE_CULTURES)
    return [
        # Turn 0: base state — no mutable fields populated (world_context is
        # the session-static promoted field, constant from here on).
        replace(base, turn_number=0),
        # Turn 1: NPC roster appears (2 NPCs)
        replace(
            base,
            turn_number=1,
            npc_pool=[
                NpcPoolMember(
                    name="Harlan", role="innkeeper", pronouns="he/him", drawn_from="world_authored"
                ),
                NpcPoolMember(
                    name="Vessa", role="merchant", pronouns="she/her", drawn_from="world_authored"
                ),
            ],
        ),
        # Turn 2: game state summary materializes
        replace(
            base,
            turn_number=2,
            npc_pool=[
                NpcPoolMember(
                    name="Harlan", role="innkeeper", pronouns="he/him", drawn_from="world_authored"
                ),
                NpcPoolMember(
                    name="Vessa", role="merchant", pronouns="she/her", drawn_from="world_authored"
                ),
            ],
            state_summary=(
                "Location: The Black Hart Tavern\n"
                "Party: Kael (level 2 ranger)\n"
                "Active quest: Find the missing merchant"
            ),
        ),
        # Turn 3: trope context fires + active trope summary
        replace(
            base,
            turn_number=3,
            npc_pool=[
                NpcPoolMember(
                    name="Harlan", role="innkeeper", pronouns="he/him", drawn_from="world_authored"
                ),
                NpcPoolMember(
                    name="Vessa", role="merchant", pronouns="she/her", drawn_from="world_authored"
                ),
                NpcPoolMember(
                    name="Drenwick",
                    role="guard captain",
                    pronouns="he/him",
                    drawn_from="world_authored",
                ),
            ],
            state_summary=(
                "Location: The Black Hart Tavern — back room\n"
                "Party: Kael (level 2 ranger)\n"
                "Active quest: Find the missing merchant\n"
                "Recent: Guard captain Drenwick arrived asking questions"
            ),
            pending_trope_context=(
                "TROPE BEAT: A sealed letter was discovered behind the "
                "bar. The innkeeper denies knowledge."
            ),
            active_trope_summary="Active trope: The Sealed Letter (stage: discovery)",
        ),
        # Turn 4: state mutates further + world context added
        replace(
            base,
            turn_number=4,
            npc_pool=[
                NpcPoolMember(
                    name="Harlan", role="innkeeper", pronouns="he/him", drawn_from="world_authored"
                ),
                NpcPoolMember(
                    name="Vessa", role="merchant", pronouns="she/her", drawn_from="world_authored"
                ),
                NpcPoolMember(
                    name="Drenwick",
                    role="guard captain",
                    pronouns="he/him",
                    drawn_from="world_authored",
                ),
                NpcPoolMember(
                    name="Old Meg",
                    role="fortune teller",
                    pronouns="she/her",
                    drawn_from="narrator_invented",
                ),
            ],
            state_summary=(
                "Location: Market square\n"
                "Party: Kael (level 2 ranger)\n"
                "Active quest: Deliver the sealed letter\n"
                "Recent: Left the tavern after confrontation with Drenwick"
            ),
            active_trope_summary="Active trope: The Sealed Letter (stage: escalation)",
        ),
    ]


@pytest.fixture
async def bound_hub() -> WatcherHub:
    watcher_hub.bind_loop(asyncio.get_running_loop())
    async with watcher_hub._lock:  # noqa: SLF001
        watcher_hub._subscribers.clear()  # noqa: SLF001
    return watcher_hub


# ---------------------------------------------------------------------------
# AC-2 + AC-3: stable prefix byte-identical across 5 mutating turns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stable_prefix_byte_identical_across_5_mutating_turns(
    simple_turn_context: TurnContext,
) -> None:
    """Core validation: system_blocks[0].text hashes to a single value
    across 5 turns with progressively mutating User-bucket state (NPC
    roster, game state, trope context). world_context is held constant
    (session-static promoted field, Story 61-20).

    If this fails, the stable prefix is NOT stable under live mutations
    and the 60-3 finding is disproved.
    """
    contexts = _mutating_contexts(simple_turn_context)
    fake = FakeAnthropicSdkClient(
        responses=[_end_turn(f"narration for turn {i}") for i in range(TURN_COUNT)]
    )
    orch = Orchestrator(client=fake)

    for i, ctx in enumerate(contexts):
        await orch.run_narration_turn(f"player does thing {i}", ctx)

    assert len(fake.recorded_requests) == TURN_COUNT, (
        f"expected {TURN_COUNT} recorded SDK requests; got {len(fake.recorded_requests)}"
    )

    prefixes = [r.system_blocks[0].text for r in fake.recorded_requests]
    digests = [hashlib.sha256(p.encode("utf-8")).hexdigest()[:8] for p in prefixes]
    unique_digests = set(digests)

    assert len(unique_digests) == 1, (
        f"system_blocks[0] MUST be byte-identical across {TURN_COUNT} turns "
        f"with mutating state. Got {len(unique_digests)} distinct digests: "
        f"{digests}. This disproves 60-3's finding — the stable prefix "
        f"drifted under live state mutations. Turn-by-turn digest sequence: "
        + ", ".join(f"T{i}={d}" for i, d in enumerate(digests))
    )


# ---------------------------------------------------------------------------
# AC-1 + AC-2: watcher-event digest stable across 5 mutating turns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stable_digest_on_watcher_events_across_5_mutating_turns(
    bound_hub: WatcherHub,
    simple_turn_context: TurnContext,
) -> None:
    """Same mutation sequence, verified through the prompt_assembled watcher
    event path — the channel the GM panel actually reads."""
    contexts = _mutating_contexts(simple_turn_context)
    sock = FakeSocket()
    fake = FakeAnthropicSdkClient(
        responses=[_end_turn(f"narration for turn {i}") for i in range(TURN_COUNT)]
    )
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]
    orch = Orchestrator(client=fake)

    for i, ctx in enumerate(contexts):
        await orch.run_narration_turn(f"player does thing {i}", ctx)
    await asyncio.sleep(0.05)

    enriched = [
        e
        for e in sock.events
        if e.get("event_type") == "prompt_assembled" and "cache_blocks" in e.get("fields", {})
    ]
    assert len(enriched) == TURN_COUNT, (
        f"expected {TURN_COUNT} enriched prompt_assembled events; "
        f"got {len(enriched)}. The OTEL watcher path is not firing on every turn."
    )

    digests: list[str] = []
    for ev in enriched:
        by_label = {b["label"]: b for b in ev["fields"]["cache_blocks"]}
        assert "stable" in by_label, (
            f"prompt_assembled event missing 'stable' cache_block; labels found: {sorted(by_label)}"
        )
        digests.append(by_label["stable"]["digest"])

    unique_digests = set(digests)
    assert len(unique_digests) == 1, (
        f"watcher-emitted stable digest MUST be constant across {TURN_COUNT} "
        f"mutating turns. Got {len(unique_digests)} distinct digests: "
        f"{digests}. The GM panel would report false drift."
    )


# ---------------------------------------------------------------------------
# AC-4: per-turn cost emitted on OTEL span
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_turn_cost_on_otel_span_across_5_turns(
    simple_turn_context: TurnContext,
    otel_capture: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each narration.turn span carries a positive total_cost_usd — the
    per-turn baseline Dev will record for 60-4 regression comparison."""
    from sidequest.agents.anthropic_sdk_client import AnthropicSdkClient

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    class _CacheCreation:
        ephemeral_5m_input_tokens: int = 0
        ephemeral_1h_input_tokens: int = 0

    class _Usage:
        def __init__(self) -> None:
            self.input_tokens = 500
            self.output_tokens = 80
            self.cache_read_input_tokens = 11000
            self.cache_creation_input_tokens = 500
            self.cache_creation = _CacheCreation()

    class _TextBlock:
        type = "text"

        def __init__(self, text: str) -> None:
            self.text = text

    class _Resp:
        def __init__(self, text: str) -> None:
            self.content = [_TextBlock(text)]
            self.stop_reason = "end_turn"
            self.usage = _Usage()
            self.model = "claude-sonnet-4-6"

    class _Msgs:
        def __init__(self) -> None:
            self._turn = 0

        async def create(self, **_: Any) -> _Resp:
            self._turn += 1
            return _Resp(f"Turn {self._turn} narration.")

    class _Sdk:
        def __init__(self) -> None:
            self.messages = _Msgs()

    contexts = _mutating_contexts(simple_turn_context)
    client = AnthropicSdkClient(sdk=_Sdk(), cache_ttl="5m")
    orch = Orchestrator(client=client)

    for i, ctx in enumerate(contexts):
        await orch.run_narration_turn(f"player does thing {i}", ctx)

    turn_spans = [s for s in otel_capture.get_finished_spans() if s.name == "narration.turn"]
    assert len(turn_spans) == TURN_COUNT, (
        f"expected {TURN_COUNT} narration.turn spans; got {len(turn_spans)}"
    )

    costs: list[float] = []
    for i, span in enumerate(turn_spans):
        attrs = dict(span.attributes or {})
        cost = attrs.get("narration.turn.total_cost_usd")
        assert isinstance(cost, float) and cost > 0.0, (
            f"Turn {i}: narration.turn.total_cost_usd must be a positive float; "
            f"got {cost!r}. The cost baseline for 60-4 requires real per-turn "
            f"cost data on every span."
        )
        costs.append(cost)

    assert all(c > 0.0 for c in costs), (
        f"all {TURN_COUNT} turns must report positive cost; got {costs}"
    )


# ---------------------------------------------------------------------------
# AC-5: no silent fallbacks — digest field present and valid every turn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_blocks_digest_present_and_valid_every_turn(
    bound_hub: WatcherHub,
    simple_turn_context: TurnContext,
) -> None:
    """If the digest field is missing from any prompt_assembled event, fail
    loudly with the exact field name and location. The ground truth is the
    live watcher data, not inference."""
    contexts = _mutating_contexts(simple_turn_context)
    sock = FakeSocket()
    fake = FakeAnthropicSdkClient(responses=[_end_turn(f"turn {i}") for i in range(TURN_COUNT)])
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]
    orch = Orchestrator(client=fake)

    for i, ctx in enumerate(contexts):
        await orch.run_narration_turn(f"action {i}", ctx)
    await asyncio.sleep(0.05)

    prompt_events = [e for e in sock.events if e.get("event_type") == "prompt_assembled"]
    assert len(prompt_events) >= TURN_COUNT, (
        f"expected at least {TURN_COUNT} prompt_assembled events; "
        f"got {len(prompt_events)}. 60-2 wiring may not be fully live."
    )

    enriched = [e for e in prompt_events if "cache_blocks" in e.get("fields", {})]
    assert len(enriched) == TURN_COUNT, (
        f"expected {TURN_COUNT} prompt_assembled events with cache_blocks; "
        f"got {len(enriched)}. The cache attribution field from 60-2 is "
        f"missing on some turns — fail loudly per AC-5."
    )

    for i, ev in enumerate(enriched):
        blocks = ev["fields"]["cache_blocks"]
        by_label = {b["label"]: b for b in blocks}
        assert "stable" in by_label, (
            f"Turn {i}: prompt_assembled.cache_blocks missing 'stable' entry. "
            f"Labels found: {sorted(by_label)}. Cannot verify prefix stability "
            f"without this field."
        )
        digest = by_label["stable"]["digest"]
        assert isinstance(digest, str) and len(digest) == 8, (
            f"Turn {i}: stable block digest must be an 8-char hex string; got {digest!r}"
        )
        assert all(c in "0123456789abcdef" for c in digest), (
            f"Turn {i}: stable block digest must be lowercase hex; got {digest!r}"
        )


# ---------------------------------------------------------------------------
# AC-6: emitted digest matches actual system_blocks hash (cross-check)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watcher_digest_matches_real_system_blocks_across_mutations(
    bound_hub: WatcherHub,
    simple_turn_context: TurnContext,
) -> None:
    """Cross-check: the digest on the prompt_assembled event equals
    sha256(actual system_blocks[0].text)[:8] on every mutating turn. If
    these diverge, the watcher is computing the digest from a different
    string than what was cached — exactly the decoupled-estimate bug the
    60-2 eyes were built to prevent."""
    contexts = _mutating_contexts(simple_turn_context)
    sock = FakeSocket()
    fake = FakeAnthropicSdkClient(responses=[_end_turn(f"turn {i}") for i in range(TURN_COUNT)])
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]
    orch = Orchestrator(client=fake)

    for i, ctx in enumerate(contexts):
        await orch.run_narration_turn(f"action {i}", ctx)
    await asyncio.sleep(0.05)

    assert len(fake.recorded_requests) == TURN_COUNT

    enriched = [
        e
        for e in sock.events
        if e.get("event_type") == "prompt_assembled" and "cache_blocks" in e.get("fields", {})
    ]
    assert len(enriched) == TURN_COUNT

    for i in range(TURN_COUNT):
        real_prefix = fake.recorded_requests[i].system_blocks[0].text
        expected_digest = hashlib.sha256(real_prefix.encode("utf-8")).hexdigest()[:8]
        by_label = {b["label"]: b for b in enriched[i]["fields"]["cache_blocks"]}
        emitted_digest = by_label["stable"]["digest"]
        assert emitted_digest == expected_digest, (
            f"Turn {i}: emitted digest {emitted_digest!r} does not match "
            f"sha256(actual system_blocks[0])[:8] = {expected_digest!r}. "
            f"The watcher is reporting a digest computed from different "
            f"content than what was sent to the API."
        )


# ---------------------------------------------------------------------------
# AC-3 edge case: encounter state transitions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_encounter_transitions_do_not_drift_stable_prefix(
    simple_turn_context: TurnContext,
) -> None:
    """Transitions between peace/combat/chase/peace exercise conditional
    section registrations (genre_combat_voice, genre_chase_voice) that are
    NOT in STABLE_SECTION_NAMES and route to User-bucket. The stable prefix
    must remain identical throughout."""
    contexts = [
        replace(simple_turn_context, turn_number=0, in_combat=False, in_chase=False),
        replace(
            simple_turn_context,
            turn_number=1,
            in_combat=True,
            in_encounter=True,
            encounter_summary="Round 1: Kael vs Goblin Skirmisher. Kael has edge.",
        ),
        replace(
            simple_turn_context,
            turn_number=2,
            in_combat=False,
            in_chase=True,
            encounter_summary="Chase: Kael pursues fleeing goblin through tunnels.",
        ),
        replace(
            simple_turn_context,
            turn_number=3,
            in_combat=False,
            in_chase=False,
            state_summary="Location: Deep cavern. The goblin escaped into a crack.",
        ),
        replace(
            simple_turn_context,
            turn_number=4,
            in_combat=True,
            in_encounter=True,
            encounter_summary="Round 1: Kael vs Cave Spider. Ambush from above.",
            npc_pool=[
                NpcPoolMember(
                    name="Cave Spider",
                    role="creature",
                    pronouns="it/its",
                    drawn_from="world_authored",
                ),
            ],
        ),
    ]
    fake = FakeAnthropicSdkClient(responses=[_end_turn(f"turn {i}") for i in range(TURN_COUNT)])
    orch = Orchestrator(client=fake)

    for i, ctx in enumerate(contexts):
        await orch.run_narration_turn(f"action {i}", ctx)

    prefixes = [r.system_blocks[0].text for r in fake.recorded_requests]
    digests = [hashlib.sha256(p.encode("utf-8")).hexdigest()[:8] for p in prefixes]
    unique_digests = set(digests)

    assert len(unique_digests) == 1, (
        f"stable prefix drifted during encounter state transitions. "
        f"Digest sequence: {digests}. Conditional registrations "
        f"(combat/chase voice) leaked into the cached block."
    )


# ---------------------------------------------------------------------------
# Wiring test: full end-to-end — recorded blocks + watcher events agree
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_five_turn_stability_wired_end_to_end(
    bound_hub: WatcherHub,
    simple_turn_context: TurnContext,
) -> None:
    """Integration wiring: both the recorded system_blocks AND the watcher-
    emitted digest agree across all 5 mutating turns. This is the single
    test that would catch a decoupling between the real SDK input and the
    GM panel's display."""
    contexts = _mutating_contexts(simple_turn_context)
    sock = FakeSocket()
    fake = FakeAnthropicSdkClient(responses=[_end_turn(f"turn {i}") for i in range(TURN_COUNT)])
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]
    orch = Orchestrator(client=fake)

    for i, ctx in enumerate(contexts):
        await orch.run_narration_turn(f"action {i}", ctx)
    await asyncio.sleep(0.05)

    assert len(fake.recorded_requests) == TURN_COUNT

    enriched = [
        e
        for e in sock.events
        if e.get("event_type") == "prompt_assembled" and "cache_blocks" in e.get("fields", {})
    ]
    assert len(enriched) == TURN_COUNT

    recorded_digests: list[str] = []
    emitted_digests: list[str] = []

    for i in range(TURN_COUNT):
        prefix_text = fake.recorded_requests[i].system_blocks[0].text
        recorded_digests.append(hashlib.sha256(prefix_text.encode("utf-8")).hexdigest()[:8])
        by_label = {b["label"]: b for b in enriched[i]["fields"]["cache_blocks"]}
        emitted_digests.append(by_label["stable"]["digest"])

    # Recorded digests must all be identical (prefix stability)
    assert len(set(recorded_digests)) == 1, (
        f"recorded system_blocks[0] digest drifted: {recorded_digests}"
    )

    # Emitted digests must all be identical (watcher stability)
    assert len(set(emitted_digests)) == 1, (
        f"watcher-emitted stable digest drifted: {emitted_digests}"
    )

    # Both channels must agree on the same digest value
    assert recorded_digests[0] == emitted_digests[0], (
        f"recorded digest {recorded_digests[0]!r} != emitted digest "
        f"{emitted_digests[0]!r}. The watcher is decoupled from the "
        f"real SDK input."
    )
