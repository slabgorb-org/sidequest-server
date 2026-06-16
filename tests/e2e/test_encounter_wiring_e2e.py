"""End-to-end caverns_and_claudes combat wiring (story 3.4 closing gate, rebuilt 73-12).

No live LLM — both the narrator (``orchestrator.run_narration_turn``) and the
intent router (``decompose``) are scripted, so the turns are deterministic.

Post-ADR-113 scope note (Story 73-12): the original story-3.4 test drove the whole
``initiate → tick → resolve`` lifecycle off the narration result. That model is dead:
  - Encounter CREATION moved off ``result.confrontation`` (removed in Story 59-4) onto
    the router-driven pre-narrator dispatch bank (``run_confrontation_dispatch``).
  - PC BEATS no longer come from narrator ``beat_selections`` — the SOUL "The Test"
    gate rejects inferred PC beats; explicit beats arrive via DICE_THROW.
  - The single ``metric`` field became the dual ``player_metric``/``opponent_metric``
    dials (ADR-024); ``StructuredEncounter`` rejects the legacy field.

This file now pins the CREATION half end-to-end through the handler (router dispatch →
ADR-116 seating → dual-dial encounter → CONFRONTATION frame → OTEL span) plus the
in-combat XP-award semantics. The beat-tick → threshold → resolution MECHANICS are
covered deterministically at the engine level (``tests/integration/test_combat_otel_wiring.py``
calls ``apply_beat`` directly; ``tests/server/test_confrontation_dispatch_wiring.py``
pre-seats an encounter to drive resolution). The FULL router→beat→resolution *handler*
path is not yet covered anywhere — a tracked gap (73-12 delivery finding): driving it
needs the DICE_THROW path with fully wired opponent combatant cores, a fixture lift out
of scope for this stale-assertion rewrite.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from pydantic import ValidationError

from sidequest.agents.orchestrator import BeatSelection, NarrationTurnResult
from sidequest.game.encounter import StructuredEncounter
from sidequest.protocol.messages import ConfrontationMessage


@pytest.fixture
def span_exporter():
    """Attach an in-memory exporter to the running TracerProvider.

    Mirrors the ``otel_capture`` fixture pattern from test_room_graph_init.py:
    mounts an additional SimpleSpanProcessor alongside existing processors so
    handler span emissions fan out to in-memory for the duration of the test.
    Does NOT replace the global provider (which would corrupt other tests).
    """
    from sidequest.telemetry.setup import init_tracer

    init_tracer()  # idempotent — installs SDK provider if not yet set
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider), (
        f"expected SDK TracerProvider, got {type(provider)!r}"
    )
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


@pytest.fixture
def deterministic_combat_router(monkeypatch):
    """Make encounter CREATION deterministic by stubbing the router's ``decompose``.

    ADR-113 / Story 59-4 moved confrontation engagement OFF the narration result
    (``result.confrontation`` is dead on the SDK path — narration_apply.py block (a)
    was removed) and ONTO the router-driven pre-narrator dispatch bank: the encounter
    is instantiated by ``run_confrontation_dispatch`` BEFORE the narrator runs. So a
    walkthrough that only mocks ``orchestrator.run_narration_turn`` never creates an
    encounter at all.

    Left fully live the pre-pass also (a) builds a real Anthropic SDK client (needs
    ANTHROPIC_API_KEY) and (b) makes a NON-DETERMINISTIC Haiku ``decompose`` call —
    the classification varies run to run (encounter intermittently created / not),
    which is the real root cause of this file's flakiness (and of the 73-6
    "in-combat XP observed 10" finding: no encounter ⇒ ``in_combat_now`` False ⇒ 10).
    That flake was masked while the stale single-``metric`` assertion crashed first.

    Fix: stub only the LLM judgment. ``build_intent_router_for_session`` returns a
    router whose ``decompose`` emits a deterministic ``confrontation``/``combat``
    dispatch on the encounter-OPENING turn (no live encounter yet + an attack action)
    and an empty package otherwise. The REAL pre-narrator pass, dispatch bank,
    ``run_confrontation_dispatch`` and ADR-116 opponent seating all run — so this stays
    a genuine end-to-end wiring test, just with the network classification pinned.
    Project rule: tests MUST NOT spawn a real Claude client (mirrors the Orchestrator
    MagicMock stub in tests/server/conftest.py).
    """
    from sidequest.protocol.dispatch import (
        DispatchPackage,
        PlayerDispatch,
        SubsystemDispatch,
        VisibilityTag,
    )

    def _empty_package() -> DispatchPackage:
        return DispatchPackage(
            turn_id="t-e2e",
            per_player=[],
            cross_player=[],
            confidence_global=0.0,
        )

    def _combat_package(action: str) -> DispatchPackage:
        dispatch = SubsystemDispatch(
            subsystem="confrontation",
            params={"type": "combat"},
            depends_on=[],
            idempotency_key="e2e-combat",
            visibility=VisibilityTag(
                visible_to="all",
                perception_fidelity={},
                secrets_for=[],
                redact_from_narrator_canonical=False,
            ),
            confidence=0.95,  # >= 0.6 default gate → engages
        )
        return DispatchPackage(
            turn_id="t-e2e",
            per_player=[
                PlayerDispatch(
                    player_id="player-1",
                    raw_action=action,
                    resolved=[],
                    dispatch=[dispatch],
                    lethality=[],
                    narrator_instructions=[],
                )
            ],
            cross_player=[],
            confidence_global=0.95,
        )

    async def _fake_decompose(*, action: str, state_summary) -> DispatchPackage:
        # Open a combat encounter only on the turn that BOTH lacks a LIVE encounter
        # and reads as an attack. Liveness check (corrected per 73-12 review): the
        # production lifecycle does NOT clear ``snapshot.encounter`` to None on
        # resolution — it flips ``resolved=True`` on the live object. ``state_summary``
        # is ``model_dump(exclude_defaults=True, exclude_none=True)``, so a present
        # "encounter" dict carries ``resolved: True`` once resolved (resolved is a
        # non-default value); a resolved encounter must therefore be treated as NOT
        # live so a subsequent attack can re-open. (No current test drives a
        # resolved-then-attack turn, but the guard must be correct, not accidental.)
        enc_summary = state_summary.get("encounter")
        has_live_encounter = bool(enc_summary) and not enc_summary.get("resolved", False)
        if not has_live_encounter and "attack" in action.lower():
            return _combat_package(action)
        return _empty_package()

    fake_router = MagicMock()
    fake_router.decompose = AsyncMock(side_effect=_fake_decompose)
    monkeypatch.setattr(
        "sidequest.server.intent_router_pass.build_intent_router_for_session",
        lambda **_kwargs: fake_router,
    )


_COMBAT_LOCATION = "Mawdeep Caverns"


def _seat_combat_scene(sd) -> None:
    """Give the factory session a combat-capable scene.

    The bare ``session_handler_factory`` snapshot has a PC but no seat mapping,
    no location, and no NPCs. ADR-116 ("A Confrontation Requires an Other")
    correctly refuses to open an opponentless combat encounter, so the
    router-driven confrontation dispatch logs ``encounter.no_opponent_available``
    and creates nothing. Seat the acting player as that PC, place the PC at a
    location, and put a hostile NPC there so the ADR-116 location-roster
    fallback (``_npc_fallback_at_location``) can seat the Other. Recipe mirrors
    ``tests/server/test_encounter_actors_all_combatants.py``.
    """
    from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
    from sidequest.game.session import Npc

    snap = sd.snapshot
    # Derive the PC name from the factory's own character rather than hard-coding
    # it (73-12 review) so this helper survives a factory rename. Resolve the
    # acting player to that PC so the acting-character location lookup AND the
    # party-wide XP seating both target it.
    pc_name = snap.characters[0].core.name
    snap.player_seats[sd.player_id] = pc_name
    snap.character_locations[pc_name] = _COMBAT_LOCATION
    snap.npcs.append(
        Npc(
            core=CreatureCore(
                name="Crawling Scavenger",
                description="a chittering carapaced thing the size of a hound",
                personality="Hostile.",
                inventory=Inventory(),
                hp=HpPool(current=10, max=10, base_max=10),
            ),
            npc_role_id="hostile",
            last_seen_location=_COMBAT_LOCATION,
            last_seen_turn=0,
        )
    )


@pytest.mark.asyncio
async def test_combat_walkthrough_router_initiates_dual_dial_encounter(
    session_handler_factory,
    span_exporter,
    deterministic_combat_router,
):
    """A player attack opens a dual-dial combat encounter through the real
    handler pipeline: intent-router confrontation dispatch → ADR-116 opponent
    seating → StructuredEncounter on the snapshot → CONFRONTATION frame →
    ``encounter.confrontation_initiated`` OTEL span.

    This is the creation half of the original story-3.4 walkthrough, rebuilt for
    the post-ADR-113 architecture: encounter creation moved OFF the narration
    result (``result.confrontation`` was removed in Story 59-4) and ONTO the
    router-driven pre-narrator dispatch bank, and the single ``metric`` field
    became the dual ``player_metric``/``opponent_metric`` dials (ADR-024). The
    beat-tick → threshold → resolution half is deliberately NOT driven here — see
    the module docstring and the 73-12 deviation note. It belongs at the engine
    level (``tests/integration/test_combat_otel_wiring.py`` constructs the
    encounter + combatant cores and calls ``apply_beat`` directly), because
    driving PC beats through the handler requires the DICE_THROW path with fully
    wired opponent combatant cores — a fixture lift out of scope for this
    stale-assertion rewrite.
    """
    from sidequest.server.session_handler import _build_turn_context

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    _seat_combat_scene(sd)
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(narration="Goblins leap from the shadows!"),
    )

    msgs = await handler._execute_narration_turn(
        sd,
        "I attack!",
        _build_turn_context(sd),
    )

    enc = sd.snapshot.encounter
    assert enc is not None, (
        "router-driven confrontation dispatch did not open an encounter — "
        "creation is router-driven post-ADR-113 (run_confrontation_dispatch)"
    )
    assert enc.encounter_type == "combat"
    assert not enc.resolved
    # ADR-116: a confrontation requires an Other. The dispatch seated the PC
    # plus the hostile NPC from the location roster fallback.
    sides = {a.side for a in enc.actors}
    assert "player" in sides and "opponent" in sides

    # Dual-dial model (ADR-024): both side-routed dials exist and start at 0.
    assert enc.player_metric.current == 0
    assert enc.opponent_metric.current == 0
    assert enc.player_metric.threshold > 0
    assert enc.opponent_metric.threshold > 0
    # The legacy single ``metric`` field isn't merely absent from the schema — the
    # validator ACTIVELY rejects it (73-12 review: the prior `assert not hasattr`
    # was vacuous, since a Pydantic model can't carry an undeclared attribute and it
    # proved nothing about `_reject_legacy_metric`). ``match=`` pins the validator's
    # own message so the block fails if `_reject_legacy_metric` is deleted — without
    # it the block would pass on the unrelated ``extra="forbid"`` / missing-required-
    # dial ValidationError this same constructor also raises (round-2 review).
    with pytest.raises(ValidationError, match=r"legacy 'metric' field"):
        StructuredEncounter(
            encounter_type="combat",
            metric={"name": "momentum", "current": 0, "threshold": 10},
        )

    conf = [m for m in msgs if isinstance(m, ConfrontationMessage)]
    assert len(conf) == 1
    assert conf[0].payload.active is True
    assert conf[0].payload.type == "combat"
    # Payload mirrors the dual dials (ConfrontationPayload Task 12) — no legacy
    # ``metric`` key, both side dials present.
    assert conf[0].payload.player_metric["current"] == 0
    assert conf[0].payload.opponent_metric["current"] == 0

    assert "encounter.confrontation_initiated" in {
        s.name for s in span_exporter.get_finished_spans()
    }


@pytest.mark.asyncio
async def test_xp_award_higher_in_combat_than_out(
    session_handler_factory,
    deterministic_combat_router,
):
    """Regression (story 3.4 Task 13): in-combat turn awards 25 xp, 10 otherwise."""
    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    _seat_combat_scene(sd)
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(narration="You take a quiet walk."),
    )
    from sidequest.server.session_handler import _build_turn_context

    # Pin XP reads to the ACTING character by name (73-12 review) rather than
    # characters[0], so an award credited to the wrong character can't pass.
    pc_name = sd.snapshot.characters[0].core.name

    def _pc_xp() -> int:
        return next(c for c in sd.snapshot.characters if c.core.name == pc_name).core.xp

    # Out-of-combat turn.
    before = _pc_xp()
    await handler._execute_narration_turn(
        sd,
        "I walk.",
        _build_turn_context(sd),
    )
    after_out = _pc_xp()
    assert after_out - before == 10

    # Start combat, then take a beat turn in combat. The router-driven
    # confrontation dispatch opens the encounter on the "I attack." turn (ADR-113);
    # the narration results no longer carry the retired ``confrontation`` field.
    sd.orchestrator.run_narration_turn = AsyncMock(
        side_effect=[
            NarrationTurnResult(narration="Goblins!"),
            NarrationTurnResult(
                narration="You strike.",
                beat_selections=[
                    BeatSelection(actor=pc_name, beat_id="attack", target=None),
                ],
            ),
        ]
    )

    # Turn that creates the encounter. Its OWN XP award is in-combat (25), not 10:
    # the router opens the encounter in the pre-narrator pass, so in_combat_now is
    # already True when award_turn_xp runs this turn. Pin that boundary turn
    # explicitly (73-12 review) — it is the exact encounter-commit-vs-XP-timing the
    # 73-6 flake turned on.
    await handler._execute_narration_turn(
        sd,
        "I attack.",
        _build_turn_context(sd),
    )
    mid = _pc_xp()
    assert mid - after_out == 25

    # Second combat turn: still live, attack beat ticks metric but does NOT
    # resolve (momentum 0+2=2 < 10). XP still 25.
    await handler._execute_narration_turn(
        sd,
        "Again!",
        _build_turn_context(sd),
    )
    after_combat = _pc_xp()
    assert after_combat - mid == 25
