"""RW-2 wiring test — the opposed_check engine is reachable on the SDK path.

Playtest 2026-06-05 (``road_warrior/the_circuit`` chase, slug
``2026-06-05-the_circuit``) measured the entire opposed_check resolution
engine as structurally DEAD on the default (ADR-101 SDK) narrator backend:

1. ``_SDK_TOOL_OWNED_FIELDS`` zeroed ``beat_selections`` on every SDK turn;
2. ``narration_apply``'s resolution dispatch gates on non-empty selections,
   so ``_resolve_opposed_check_branch`` never ran;
3. the player's stashed DICE_THROW d20 was cleared silently every turn;
4. all dial movement came from the narrator free-handing
   ``advance_confrontation`` with invented deltas — PG telemetry showed ZERO
   ``opposed_roll_resolved`` events all session. The dice were decorative.

This suite drives the REAL production seams end-to-end (CLAUDE.md "Every
Test Suite Needs a Wiring Test"):

* ``run_narration_turn`` through ``FakeAnthropicSdkClient`` whose scripted
  final text embeds a game_patch carrying an OPPONENT-side beat_selection,
  with a live opposed_check ``confrontation_def`` on the TurnContext →
  the SDK assembler's carve-out lifts the selection onto the result;
* ``_apply_narration_result_to_snapshot`` with the production-default
  ``from_explicit_action=False`` and the stashed player d20 (exactly what
  the session handler threads from ``sd.pending_opposed_player_*``) →
  the SOUL gate keeps the opponent beat, the opposed resolver runs,
  ``encounter.opposed_roll_resolved`` fires, and the player metric moves
  by the beat base.
"""

from __future__ import annotations

import json

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# Importing the tools package wires the WRITE-tool adapters onto
# default_registry — the SDK narration path exposes them.
import sidequest.agents.tools  # noqa: F401
from sidequest.agents.orchestrator import NarrationTurnResult, Orchestrator, TurnContext
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    EncounterPhase,
    StructuredEncounter,
)
from sidequest.game.session import GameSnapshot
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.rules import ConfrontationDef, RulesConfig
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from sidequest.telemetry.spans import SPAN_ENCOUNTER_OPPOSED_ROLL_RESOLVED
from tests._helpers.session_room import room_for
from tests.agents.fakes.fake_anthropic_sdk_client import (
    FakeAnthropicSdkClient,
    ScriptedResponse,
)

# ---------------------------------------------------------------------------
# Fixtures — mirror tests/server/test_opposed_check_wiring.py shapes
# ---------------------------------------------------------------------------


def _opposed_cdef() -> ConfrontationDef:
    return ConfrontationDef.model_validate(
        {
            "type": "combat",
            "label": "Combat",
            "category": "combat",
            "resolution_mode": "opposed_check",
            "opponent_default_stats": {"STR": 12},
            "player_metric": {"name": "momentum", "starting": 0, "threshold": 10},
            "opponent_metric": {"name": "momentum", "starting": 0, "threshold": 10},
            "beats": [
                {
                    "id": "attack",
                    "label": "Attack",
                    "kind": "strike",
                    "base": 2,
                    "stat_check": "STR",
                }
            ],
        }
    )


def _make_pack(cdef: ConfrontationDef) -> GenrePack:
    return GenrePack.model_construct(rules=RulesConfig(confrontations=[cdef]))


def _make_encounter() -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        structured_phase=EncounterPhase.Setup,
        actors=[
            EncounterActor(
                name="Sam",
                role="combatant",
                side="player",
                per_actor_state={"stats": {"STR": 14}},
            ),
            EncounterActor(
                name="Wolf",
                role="combatant",
                side="opponent",
                per_actor_state={"stats": {"STR": 14}},
            ),
        ],
    )


_GAME_PATCH = {
    # Opponent-side beat — the narrator's half of the opposed exchange.
    "beat_selections": [{"actor": "Wolf", "beat_id": "attack", "outcome": "Success"}],
    "scene_mood": "kinetic",
}

_NARRATOR_TEXT = (
    f"Wolf lunges low, going for the hamstring.\n\n```game_patch\n{json.dumps(_GAME_PATCH)}\n```\n"
)


class _FakeRegistry:
    def compose_split(self, agent_name: str) -> tuple[str, str]:
        return ("system text", "user text")

    def compose_split_by_zone(self, agent_name: str):
        from sidequest.agents.prompt_framework.types import AttentionZone

        return ({AttentionZone.Primacy: "system text"}, "user text")

    def registry(self, agent_name: str) -> list:
        return []


async def _run_sdk_turn(monkeypatch: pytest.MonkeyPatch) -> NarrationTurnResult:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    client = FakeAnthropicSdkClient(
        responses=[
            ScriptedResponse(
                text=_NARRATOR_TEXT,
                stop_reason="end_turn",
                input_tokens=200,
                output_tokens=60,
                cached_input_read_tokens=0,
                cached_input_write_tokens=0,
                model="claude-sonnet-4-6",
            )
        ]
    )
    orch = Orchestrator(client=client)

    async def _fake_build_prompt(
        self: Orchestrator, action: str, context: TurnContext
    ) -> tuple[str, _FakeRegistry]:
        return ("prompt-text", _FakeRegistry())

    monkeypatch.setattr(Orchestrator, "build_narrator_prompt", _fake_build_prompt)

    ctx = TurnContext(
        character_name="Sam",
        genre="road_warrior",
        turn_number=3,
        confrontation_def=_opposed_cdef(),
        encounter=_make_encounter(),
    )
    return await orch.run_narration_turn("I duck behind the wreck", ctx)


# ---------------------------------------------------------------------------
# The wiring test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sdk_turn_feeds_opposed_resolver_end_to_end(
    monkeypatch: pytest.MonkeyPatch, otel_capture: InMemorySpanExporter
) -> None:
    """SDK narrator turn → carve-out carries the opponent beat → production
    narration_apply (default ``from_explicit_action=False``, stashed player
    d20 threaded like the session handler does) → SOUL gate keeps the
    opponent beat → opposed resolver fires the lie-detector span and moves
    the player metric by the beat base.
    """
    result = await _run_sdk_turn(monkeypatch)

    # Stage 1 — the assembler carve-out carried the opponent beat.
    assert len(result.beat_selections) == 1
    assert result.beat_selections[0].actor == "Wolf"
    assert result.beat_selections[0].beat_id == "attack"

    # Stage 2 — production apply with the session-handler threading.
    enc = _make_encounter()
    pack = _make_pack(_opposed_cdef())
    snapshot = GameSnapshot(genre_slug="road_warrior", world_slug="the_circuit")
    snapshot.encounter = enc

    # Opponent rolls 5 (+2 mod) → 7 vs DC 14 → Fail; player rolled 18
    # (+2 mod) → 20 vs DC 14 → Success → strike grants own=base → +2.
    monkeypatch.setattr(
        "sidequest.server.narration_apply._roll_d20_server_side",
        lambda: 5,
    )

    _apply_narration_result_to_snapshot(
        snapshot,
        result,
        player_name="p1",
        pack=pack,
        opposed_player_d20=18,
        opposed_player_beat_id="attack",
        opposed_player_actor="Sam",
        # Production session-handler default — the SOUL gate is ACTIVE.
        from_explicit_action=False,
        room=room_for(snapshot),
    )

    # The player's metric moved by the beat base — the dice are no longer
    # decorative.
    assert enc.player_metric.current == 2
    # Opponent failed its own roll — no unearned pursuit ticks.
    assert enc.opponent_metric.current == 0

    # The lie-detector span fired — the GM panel can see the exchange.
    opposed_spans = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == SPAN_ENCOUNTER_OPPOSED_ROLL_RESOLVED
    ]
    assert len(opposed_spans) == 1
    attrs = dict(opposed_spans[0].attributes or {})
    assert attrs["player_roll"] == 18
    assert attrs["opponent_roll"] == 5
