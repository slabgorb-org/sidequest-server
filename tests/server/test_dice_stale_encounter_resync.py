"""Regression tests for playtest 2026-04-30 — Confrontation UI/server
state desync.

Pre-fix flow (the bug Sage hit, post-Crit-Success-Threaten):

1. Player clicks an action button while the UI thinks the encounter is
   active (because the React store still has the prior CONFRONTATION
   payload with ``active=True``).
2. Server has resolved or lost the encounter — could be:
    a. Natural beat-driven resolution where the prior_live → now_live
       emit at session_handler.py was missed (defense-in-depth path).
    b. uvicorn ``--reload`` mid-session that wiped in-memory encounter
       state while the React store kept the action menu.
    c. Any future state-machine path that resolves the encounter
       without emitting a clear.
3. ``DICE_THROW`` arrives with a beat_id; ``dispatch_dice_throw`` rejects
   with ``DiceDispatchError("DICE_THROW with beat_id requires an active
   encounter")``.
4. Pre-fix: handler returned ``[error_msg]`` only — UI showed the red
   error banner but kept the action menu. Every subsequent click
   bounced. Player perceived a totally stuck encounter overlay.

Fix: on this specific rejection, ALSO emit a CONFRONTATION-clear
payload so the overlay unmounts. The error message still flows so the
player sees the rejection and the GM panel sees the span, but the UI
doesn't get stuck in a state where every click bounces.

Scope note: this is a defense-in-depth recovery for cases where the
encounter is *resolved* but the UI didn't get the clear. The
encounter-is-None case (uvicorn reload wiped server state) is a
separate fix — the encounter_type isn't available so a typed clear
can't be built. Tracking under the uvicorn reload zombie bug.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
from sidequest.protocol.enums import MessageType
from sidequest.protocol.messages import DiceThrowMessage
from sidequest.server.session_handler import _State


def _make_dice_throw_msg(beat_id: str = "concede_point") -> DiceThrowMessage:
    return DiceThrowMessage(
        type=MessageType.DICE_THROW,
        payload=DiceThrowPayload(
            request_id="req-1",
            throw_params=ThrowParams(
                velocity=(0.0, 0.0, 0.0),
                angular=(0.0, 0.0, 0.0),
                position=(0.0, 0.0),
            ),
            face=[6, 6, 6],
            beat_id=beat_id,
        ),
        player_id="p1",
    )


def _resolved_encounter(encounter_type: str = "negotiation") -> StructuredEncounter:
    """Build a confrontation that's already resolved server-side —
    the bug shape after a Crit Success threshold-cross or a wandered-
    state path.
    """
    enc = StructuredEncounter(
        encounter_type=encounter_type,
        player_metric=EncounterMetric(
            name="leverage",
            current=10,
            starting=0,
            threshold=10,
        ),
        opponent_metric=EncounterMetric(
            name="leverage",
            current=2,
            starting=0,
            threshold=10,
        ),
        actors=[
            EncounterActor(name="Sam", role="participant", side="player"),
            EncounterActor(name="Lt", role="participant", side="opponent"),
        ],
    )
    enc.resolved = True
    enc.outcome = "player_victory"
    return enc


def _make_session(snapshot, pack):
    """Mock the minimum session surface DiceThrowHandler reads."""
    session = MagicMock()
    session._state = _State.Playing
    session._room = None  # solo path

    sd = MagicMock()
    sd.snapshot = snapshot
    sd.player_id = "p1"
    sd.player_name = "Sam"
    sd.genre_slug = "test_pack"
    sd.world_slug = "test_world"
    sd.genre_pack = pack

    session._session_data = sd
    return session


@pytest.mark.asyncio
async def test_dice_throw_on_resolved_encounter_emits_clear_resync(
    snapshot_with_pack,
    character_named_sam,
    caplog,
):
    """The Sage repro shape: encounter resolved server-side, UI doesn't
    know, player clicks an action button. Handler must reject AND emit
    a CONFRONTATION-clear so the overlay unmounts and the player can
    continue with free-text narration.
    """
    from sidequest.handlers.dice_throw import HANDLER

    snap, pack = snapshot_with_pack
    snap.encounter = _resolved_encounter("combat")
    snap.characters.append(character_named_sam)
    session = _make_session(snap, pack)

    with caplog.at_level("INFO"):
        outbound = await HANDLER.handle(session, _make_dice_throw_msg(beat_id="attack"))

    # Two messages: the error explaining the rejection, AND the clear
    # CONFRONTATION so the UI can recover.
    assert len(outbound) == 2, (
        f"DiceThrowHandler must emit BOTH an ERROR (rejection visible "
        f"to the player) and a CONFRONTATION-clear (UI recovery). "
        f"Pre-fix returned [error_msg] only. Got {outbound!r}"
    )

    types = [m.type for m in outbound]
    assert "ERROR" in types, "rejection must remain visible to the player"
    assert MessageType.CONFRONTATION in types, (
        "missing CONFRONTATION-clear — UI overlay would stay stuck "
        "showing action buttons that bounce on every click"
    )

    confrontation_msg = next(m for m in outbound if m.type == MessageType.CONFRONTATION)
    assert confrontation_msg.payload.active is False, (
        "the clear payload must be active=False so the overlay unmounts"
    )
    assert confrontation_msg.payload.type == "combat"

    # Lie-detector: the resync event has its own log line so the GM
    # panel / grep can see the recovery firing.
    assert any("dice.stale_encounter_resync" in r.message for r in caplog.records), (
        "missing dice.stale_encounter_resync INFO log"
    )


@pytest.mark.asyncio
async def test_dice_throw_other_rejection_does_not_emit_clear(
    snapshot_with_pack,
    character_named_sam,
):
    """Defensive: rejections that ARE NOT 'requires an active encounter'
    (e.g. unknown beat_id, invalid stat_check) should NOT trigger the
    resync. The clear is only appropriate when the UI has stale
    encounter state — other errors mean the encounter is fine but the
    request is malformed.
    """
    from sidequest.handlers.dice_throw import HANDLER

    snap, pack = snapshot_with_pack
    # Active encounter — beat_id miss is the rejection cause, not stale
    # encounter state.
    snap.encounter = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(
            name="momentum",
            current=0,
            starting=0,
            threshold=10,
        ),
        opponent_metric=EncounterMetric(
            name="momentum",
            current=0,
            starting=0,
            threshold=10,
        ),
        actors=[
            EncounterActor(name="Sam", role="combatant", side="player"),
        ],
    )
    snap.characters.append(character_named_sam)
    session = _make_session(snap, pack)

    outbound = await HANDLER.handle(
        session,
        _make_dice_throw_msg(beat_id="nonexistent_beat"),
    )

    types = [m.type for m in outbound]
    assert "ERROR" in types
    # No clear payload — the encounter is fine, just the beat ref was bad.
    # Adding one would unmount a working overlay and the player would
    # need to re-enter the confrontation.
    assert MessageType.CONFRONTATION not in types, (
        f"non-stale-encounter rejection must not unmount the overlay. Got {types!r}"
    )


@pytest.mark.asyncio
async def test_dice_throw_with_no_encounter_returns_error_only():
    """Encounter-is-None edge case (e.g. post-uvicorn-reload). We can't
    build a typed clear payload without an encounter_type, so the
    handler returns the ERROR alone. The UI-recovery path for this
    case is the separate uvicorn-reload fix that re-binds the session.
    """
    from sidequest.handlers.dice_throw import HANDLER

    session = MagicMock()
    session._state = _State.Playing
    session._room = None

    # Snapshot with no encounter.
    sd = MagicMock()
    snap = MagicMock()
    snap.encounter = None
    snap.characters = []
    snap.turn_manager.interaction = 1
    sd.snapshot = snap
    sd.player_id = "p1"
    sd.player_name = "Sam"
    sd.genre_slug = "test_pack"
    sd.world_slug = "test_world"
    sd.genre_pack = MagicMock()
    sd.genre_pack.rules.confrontations = []

    session._session_data = sd

    outbound = await HANDLER.handle(session, _make_dice_throw_msg())

    # Just the error — no encounter_type to anchor a clear payload.
    types = [m.type for m in outbound]
    assert types == ["ERROR"], (
        f"encounter-is-None case has no encounter_type for the clear "
        f"payload — handler returns ERROR only. Got {types!r}"
    )


@pytest.mark.asyncio
async def test_dice_throw_state_check_unchanged_for_non_playing():
    """Pre-existing guard: dice in non-Playing state returns an ERROR.
    The new resync logic must not regress this.
    """
    from sidequest.handlers.dice_throw import HANDLER

    session = MagicMock()
    session._state = _State.Creating
    session._session_data = MagicMock()

    outbound = await HANDLER.handle(session, _make_dice_throw_msg())

    assert len(outbound) == 1
    assert outbound[0].type == "ERROR"
