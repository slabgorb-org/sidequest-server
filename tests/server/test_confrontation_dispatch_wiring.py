"""Task 11: CONFRONTATION message dispatched on encounter begin/active/end.

These tests mock the orchestrator — no LLM call — and assert the handler
pushes a single ConfrontationMessage into the outbound list per transition.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sidequest.agents.orchestrator import BeatSelection, NarrationTurnResult
from sidequest.genre.loader import load_genre_pack
from sidequest.protocol.messages import ConfrontationMessage

# Fixture pack on disk — cache-free reload to sidestep the session-wide
# GenreLoader cache that other tests can poison with real-content CAC.
_FIXTURE_PACK = Path(__file__).resolve().parents[1] / "fixtures" / "packs" / "test_genre"


def _result(narration: str = "ok", **kwargs) -> NarrationTurnResult:
    return NarrationTurnResult(narration=narration, **kwargs)


@pytest.mark.asyncio
# test_confrontation_message_emitted_on_encounter_start was retired in
# Story 59-4 (ADR-113). It drove encounter creation by setting
# ``result.confrontation`` on the narrator's sidecar result and relied on
# narration_apply's consumer to instantiate the encounter — both of which
# were removed in the atomic IntentRouter cutover. Coverage for the new
# creation path lives in:
#   - tests/agents/subsystems/test_confrontation_dispatch.py
#     (handler creates encounter on snapshot pre-narrator)
#   - tests/server/test_59_4_router_wiring.py
#     (end-to-end helper drives router → bank → handler)
# The ConfrontationMessage broadcast wiring itself is exercised by the
# sibling tests below, which pre-set ``sd.snapshot.encounter`` directly
# (the broadcast logic keys on prior vs current encounter, not on the
# creation mechanism).


@pytest.mark.asyncio
async def test_confrontation_message_active_false_when_resolved(
    session_handler_factory,
):
    from sidequest.game.encounter import (
        EncounterActor,
        EncounterMetric,
        StructuredEncounter,
    )
    from sidequest.protocol.dice import RollOutcome

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    # Drive the encounter to resolution via an OPPONENT-side beat so the
    # SOUL-gate (Playtest 2026-04-26 [S2-BUG]) doesn't reject it. PC-side
    # beats can no longer fire from narrator extraction; only the legitimate
    # dice-dispatch path (or NPC turns) advance the player_metric. The unit
    # under test here is the CONFRONTATION-message dispatch on resolution,
    # not which side wins — opponent victory still triggers the same code
    # path.
    enc = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=9, starting=0, threshold=10),
        actors=[
            EncounterActor(name="Rux", role="combatant", side="player"),
            EncounterActor(name="Goblin", role="hostile", side="opponent"),
        ],
    )
    sd.snapshot.encounter = enc
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=_result(
            beat_selections=[
                BeatSelection(
                    actor="Goblin",
                    beat_id="attack",
                    outcome=RollOutcome.Success,
                    target=None,
                )
            ],
        ),
    )
    from sidequest.server.session_handler import _build_turn_context

    msgs = await handler._execute_narration_turn(
        sd,
        "Press the attack!",
        _build_turn_context(sd),
    )
    conf = [m for m in msgs if isinstance(m, ConfrontationMessage)]
    assert len(conf) == 1
    assert conf[0].payload.active is False


@pytest.mark.asyncio
async def test_no_confrontation_message_when_state_unchanged(
    session_handler_factory,
):
    """No encounter before and no encounter after → no CONFRONTATION message."""
    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=_result(narration="You take a quiet walk."),
    )
    from sidequest.server.session_handler import _build_turn_context

    msgs = await handler._execute_narration_turn(
        sd,
        "Walk quietly.",
        _build_turn_context(sd),
    )
    conf = [m for m in msgs if isinstance(m, ConfrontationMessage)]
    assert len(conf) == 0


@pytest.mark.asyncio
async def test_confrontation_message_refreshed_on_live_to_live(
    session_handler_factory,
):
    """A live encounter that stays live still emits CONFRONTATION with updated
    metric — the UI needs the new payload each turn to repaint beats/bars."""
    from sidequest.game.encounter import (
        EncounterActor,
        EncounterMetric,
        StructuredEncounter,
    )
    from sidequest.protocol.dice import RollOutcome

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    # Use an opponent-side actor so the beat advances the dial through
    # the legitimate (NPC) narrator-extraction path. PC-side beats from
    # narration are gated out post Playtest 2026-04-26 [S2-BUG].
    enc = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        actors=[
            EncounterActor(name="Rux", role="combatant", side="player"),
            EncounterActor(name="Goblin", role="hostile", side="opponent"),
        ],
    )
    sd.snapshot.encounter = enc
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=_result(
            beat_selections=[
                BeatSelection(
                    actor="Goblin",
                    beat_id="attack",
                    outcome=RollOutcome.Success,
                    target=None,
                )
            ],
        ),
    )
    from sidequest.server.session_handler import _build_turn_context

    msgs = await handler._execute_narration_turn(
        sd,
        "Swing again.",
        _build_turn_context(sd),
    )
    conf = [m for m in msgs if isinstance(m, ConfrontationMessage)]
    assert len(conf) == 1
    assert conf[0].payload.active is True
    # attack beat: kind=strike, base=2 → opponent's own (=opponent_metric)
    # advances 0+2=2, within threshold=10.
    assert conf[0].payload.opponent_metric["current"] == 2


# ---------------------------------------------------------------------------
# ADR-074 dice integration — wiring test
# (pingpong 2026-04-24 — "Momentum increments on a failed Use Mutation roll")
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dice_turn_filters_only_rolling_actor_beat_selection(
    session_handler_factory,
):
    """Wiring test: on a dice-replay turn, only the rolling actor's
    ``beat_selection`` is filtered (already applied by ``dispatch_dice_throw``).
    Opponent-side actor selections still apply so the opponent dial can
    advance and combat is two-sided.

    Original 2026-04-24 regression (playtest pingpong "Player auto-plays
    'attack' beat after failed Flank", "Confrontation tab disappears
    mid-fight") was about silent threshold-crossing via invisible NPC
    beat extractions. The original fix dropped *all* selections — but
    that overcorrected, leaving the opponent dial inert (playtest
    2026-04-25 [P0] "Beat resolution has zero effect on momentum
    dials"). The current contract keeps the no-double-apply guarantee
    for the rolling actor while preserving opponent agency. Each kept
    application emits an ``encounter.beat_applied`` watcher event — the
    GM panel sees every advance, so silent threshold-crossing is
    surfaced via the lie-detector rather than suppressed.
    """
    from sidequest.game.encounter import (
        EncounterActor,
        EncounterMetric,
        StructuredEncounter,
    )
    from sidequest.protocol.dice import RollOutcome

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    # Bypass the session-wide pack cache — otherwise a prior test that
    # loaded real-content caverns_and_claudes leaves us with a pack that
    # has no ``mutant_ability`` beat (see tests/server/conftest.py pack
    # cache comment). load_genre_pack() reads directly from disk.
    sd.genre_pack = load_genre_pack(_FIXTURE_PACK)
    enc = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        actors=[
            EncounterActor(name="Rux", role="combatant", side="player"),
            EncounterActor(name="Warden", role="hostile", side="opponent"),
        ],
    )
    sd.snapshot.encounter = enc

    # Dice-replay context: Rux just rolled (and dispatch/dice already
    # applied Rux's beat). pending_roll_outcome=Fail so the rolling
    # actor's beat would be a no-op anyway, but that's not what's under
    # test — we're testing that Warden's selection still applies.
    sd.pending_roll_outcome = SimpleNamespace(name="Fail")
    sd.pending_roll_actor = "Rux"

    # Narrator extracted beats for both player and NPC. The rolling
    # actor's (Rux) beat is filtered; the opponent's (Warden) is applied.
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=_result(
            beat_selections=[
                BeatSelection(actor="Rux", beat_id="mutant_ability", target=None),
                BeatSelection(
                    actor="Warden",
                    beat_id="mutant_ability",
                    outcome=RollOutcome.Success,
                    target=None,
                ),
            ],
        ),
    )
    from sidequest.server.session_handler import _build_turn_context

    msgs = await handler._execute_narration_turn(
        sd,
        "[BEAT_RESOLVED] Use Mutation (Instinct): ...",
        _build_turn_context(sd),
    )

    conf = [m for m in msgs if isinstance(m, ConfrontationMessage)]
    assert len(conf) == 1
    # Player dial untouched — Rux's selection was filtered (dice already
    # applied) and the dice itself rolled Fail.
    assert conf[0].payload.player_metric["current"] == 0
    # Opponent dial advanced — Warden's selection is kept. mutant_ability
    # is kind=strike, base=4; on Success, own_delta=4 routed to the
    # opponent side's own metric (= opponent_metric).
    assert conf[0].payload.opponent_metric["current"] == 4

    # Consumed — both pending fields cleared after the turn.
    assert sd.pending_roll_outcome is None
    assert sd.pending_roll_actor is None


# ---------------------------------------------------------------------------
# CHAPTER_MARKER emission on narrator-driven location change
# (pingpong 2026-04-24 — "Location not rendered in the header on resume")
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_narration_with_location_emits_chapter_marker(
    session_handler_factory,
):
    """When the narrator sets ``result.location``, the outbound frames
    include a CHAPTER_MARKER carrying the new location. The UI's
    ``useRunningHeader`` hook reads CHAPTER_MARKER events — without
    this emission the running-header chapter title stays blank.

    Wiring guard: this is the server-side half of a half-wired feature
    (the UI hook existed; the server never emitted). Regression here
    would silently break the running header again.
    """
    from sidequest.protocol.enums import MessageType
    from sidequest.protocol.messages import ChapterMarkerMessage

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=_result(
            narration="You step through the arch.",
            location="The Inner Sanctum",
        ),
    )
    from sidequest.server.session_handler import _build_turn_context

    msgs = await handler._execute_narration_turn(
        sd,
        "Move forward.",
        _build_turn_context(sd),
    )

    chapter = [m for m in msgs if isinstance(m, ChapterMarkerMessage)]
    assert len(chapter) == 1, (
        f"Expected exactly one CHAPTER_MARKER for a location change; got "
        f"{[type(m).__name__ for m in msgs]}"
    )
    assert chapter[0].type == MessageType.CHAPTER_MARKER
    assert chapter[0].payload.location == "The Inner Sanctum"


@pytest.mark.asyncio
async def test_narration_without_location_skips_chapter_marker(
    session_handler_factory,
):
    """Narration turns that do NOT change the location must not emit
    a CHAPTER_MARKER — the UI's hook would clobber the prior title
    if we emitted with the unchanged location on every turn. Scoped
    to actual location changes only.
    """
    from sidequest.protocol.messages import ChapterMarkerMessage

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=_result(narration="You look around."),
    )
    from sidequest.server.session_handler import _build_turn_context

    msgs = await handler._execute_narration_turn(
        sd,
        "Look.",
        _build_turn_context(sd),
    )
    chapter = [m for m in msgs if isinstance(m, ChapterMarkerMessage)]
    assert chapter == []


@pytest.mark.asyncio
async def test_dice_turn_success_applies_opponent_beat_selections(
    session_handler_factory,
):
    """Success roll branch of the dice-replay filter. The rolling
    actor's beat is filtered (dice already applied it via
    ``dispatch_dice_throw``); the opponent's beat is applied so the
    opponent dial advances. Guards against the filter incorrectly
    suppressing opponent agency on the Success branch.
    """
    from sidequest.game.encounter import (
        EncounterActor,
        EncounterMetric,
        StructuredEncounter,
    )
    from sidequest.protocol.dice import RollOutcome

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.genre_pack = load_genre_pack(_FIXTURE_PACK)
    enc = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=3, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        actors=[
            EncounterActor(name="Rux", role="combatant", side="player"),
            EncounterActor(name="Warden", role="hostile", side="opponent"),
        ],
    )
    sd.snapshot.encounter = enc
    sd.pending_roll_outcome = SimpleNamespace(name="Success")
    sd.pending_roll_actor = "Rux"
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=_result(
            beat_selections=[
                BeatSelection(
                    actor="Warden",
                    beat_id="mutant_ability",
                    outcome=RollOutcome.Success,
                    target=None,
                ),
            ],
        ),
    )
    from sidequest.server.session_handler import _build_turn_context

    msgs = await handler._execute_narration_turn(
        sd,
        "[BEAT_RESOLVED] ...",
        _build_turn_context(sd),
    )
    conf = [m for m in msgs if isinstance(m, ConfrontationMessage)]
    # Player dial untouched by the narrator's beat_selections (Rux not in
    # the list; dice handled Rux's roll out-of-band).
    assert conf[0].payload.player_metric["current"] == 3
    # Opponent dial advances by Warden's mutant_ability (strike, base=4).
    assert conf[0].payload.opponent_metric["current"] == 4
