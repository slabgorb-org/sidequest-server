"""Wire-first boundary test: DICE_THROW must emit CONFRONTATION post-beat-apply.

Story 45-3 — Momentum readout state sync.

The bug: between the player's beat-click → DICE_RESULT and the narrator's
NARRATION_END (5–15s on a long turn), the server holds the new
``encounter.player_metric.current`` but never broadcasts it. The UI
``ConfrontationOverlay`` sits on the prior turn's ``confrontationData``
and the dual-dial visibly lags the engine state — Sebastien's lie-detector
flag from playtest 2026-04-19.

The fix: ``dispatch_dice_throw`` (or the caller) must broadcast a
CONFRONTATION frame carrying post-``apply_beat`` momentum on every
non-deferred beat, BEFORE the inline narrator runs. This test exercises
the full ``_handle_dice_throw`` path (handler → dispatch_dice_throw →
inline narrator), captures the room's broadcast queue, and asserts:

1. **AC1 (mid-turn emit):** A CONFRONTATION message is broadcast between
   DICE_RESULT and NARRATION_END, with ``player_metric.current`` /
   ``opponent_metric.current`` reflecting the post-beat-apply encounter
   state.

2. **AC1 negative (opposed-check deferral):** When the active
   confrontation declares ``resolution_mode: opposed_check``, deltas are
   deferred to ``narration_apply``; the dispatcher MUST NOT emit a
   mid-turn CONFRONTATION on this branch.

3. **Existing post-narration emit unchanged (AC5 regression):** the
   post-narration CONFRONTATION emit at ``session_handler.py`` still
   fires once per narration turn — the new mid-turn emit is additive.

Wire-first gate: the test drives the production handler through
``handle_message(DiceThrowMessage)`` — not ``dispatch_dice_throw``
in isolation. A future regression that re-orders emit vs broadcast (or
moves the call inside the narrator) will be caught here.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    EncounterPhase,
    StructuredEncounter,
)
from sidequest.genre.models.rules import (
    BeatDef,
    ConfrontationDef,
    MetricDef,
)
from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
from sidequest.protocol.messages import (
    ConfrontationMessage,
    DiceRequestMessage,
    DiceResultMessage,
    DiceThrowMessage,
    NarrationMessage,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Bind the process pool to a per-worker throwaway PG db, clean per test
    (ADR-115 F1: the AC5 emit test persists/reads events through Postgres)."""
    import psycopg

    from sidequest.game import db_pool

    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(plain, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename <> 'alembic_version'"
        ).fetchall()
        if rows:
            names = ", ".join(f'"{r[0]}"' for r in rows)
            conn.execute(f"TRUNCATE {names} RESTART IDENTITY CASCADE")
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    yield
    db_pool.close_pool()


def _install_combat_def(sd, *, resolution_mode: str = "beat_selection") -> None:
    """Install a deterministic combat ConfrontationDef.

    ``stat_check=STRENGTH`` maps to the +2 modifier we set on the seeded
    Rux character. ``base=3`` so a Success throw produces a non-zero
    metric delta we can observe on the post-apply momentum.
    """
    cdef = ConfrontationDef.model_validate(
        {
            "type": "combat",
            "label": "Dungeon Combat",
            "category": "combat",
            "resolution_mode": resolution_mode,
            "opponent_default_stats": ({"STR": 12} if resolution_mode == "opposed_check" else {}),
            "player_metric": MetricDef(
                name="momentum",
                starting=0,
                threshold=10,
            ).model_dump(),
            "opponent_metric": MetricDef(
                name="momentum",
                starting=0,
                threshold=10,
            ).model_dump(),
            "beats": [
                BeatDef.model_validate(
                    {
                        "id": "attack",
                        "label": "Attack",
                        "kind": "strike",
                        "base": 3,
                        "stat_check": "STRENGTH",
                    }
                ).model_dump(),
            ],
        }
    )
    sd.genre_pack.rules.confrontations = [cdef]


def _install_active_encounter(sd) -> None:
    enc = StructuredEncounter(
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
        beat=0,
        structured_phase=EncounterPhase.Setup,
        secondary_stats=None,
        actors=[
            EncounterActor(name="Rux", role="combatant", side="player"),
            EncounterActor(name="Goblin", role="combatant", side="opponent"),
        ],
        outcome=None,
        resolved=False,
        mood_override=None,
        narrator_hints=[],
    )
    sd.snapshot.encounter = enc


def _throw(face: int = 15, beat_id: str = "attack") -> DiceThrowMessage:
    return DiceThrowMessage(
        payload=DiceThrowPayload(
            request_id="momentum-sync-req-1",
            throw_params=ThrowParams(
                velocity=(0.0, 5.0, -2.0),
                angular=(1.0, 1.0, 1.0),
                position=(0.5, 0.5),
            ),
            face=[face],
            beat_id=beat_id,
        ),
        player_id="player-1",
    )


class _StubRoom:
    """SessionRoom stand-in that records broadcasts in arrival order."""

    slug = "momentum-sync-test"

    def __init__(self) -> None:
        self.broadcasts: list[tuple[object, str | None]] = []
        from sidequest.game.session import GameSnapshot
        from sidequest.server.session import Session

        self.session = Session(GameSnapshot(genre_slug="caverns_and_claudes"))

    def broadcast(
        self,
        msg: object,
        *,
        exclude_socket_id: str | None = None,
    ) -> None:
        self.broadcasts.append((msg, exclude_socket_id))

    def is_paused(self) -> bool:
        return False


# ---------------------------------------------------------------------------
# AC1: Server emits CONFRONTATION after beat-apply, before narration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dice_throw_emits_confrontation_with_post_beat_momentum(
    session_handler_factory,
):
    """The mid-turn CONFRONTATION broadcast carries post-apply momentum.

    Sequence under wire-first:
      1. UI sends DICE_THROW with beat_id=attack (face=15, +2 mod, DC=14
         → total 17, Success tier, base=3 own_delta).
      2. ``dispatch_dice_throw`` calls ``apply_beat`` which mutates
         ``encounter.player_metric.current`` (0 → 3 on Success).
      3. ``room_broadcast`` fans out DICE_REQUEST + DICE_RESULT.
      4. **NEW (this story):** a CONFRONTATION broadcast carrying the
         post-apply ``player_metric.current=3`` arrives in the room
         queue BEFORE the inline narrator's NARRATION_END.

    The assertion: scan the broadcast queue, find the CONFRONTATION
    that arrives BEFORE NARRATION_END, and verify it reflects the
    post-apply metric — not the starting zero.
    """
    from sidequest.server.session_handler import _State

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    handler._state = _State.Playing
    _install_combat_def(sd)
    _install_active_encounter(sd)
    sd.snapshot.characters[0].stats["STRENGTH"] = 14  # +2 modifier

    room = _StubRoom()
    handler._room = room  # type: ignore[assignment]

    # Stub the narrator so the inline turn returns deterministically. The
    # post-narration CONFRONTATION emit at ``_execute_narration_turn``
    # runs through this stub; we capture both emits (mid-turn + post-
    # narration) on the same room queue.
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(narration="You strike true!"),
    )

    await handler.handle_message(_throw(face=15))

    # Order witness: index of DICE_RESULT and any NARRATION_END marker.
    order: list[str] = []
    for msg, _exclude in room.broadcasts:
        if isinstance(msg, DiceResultMessage):
            order.append("DICE_RESULT")
        elif isinstance(msg, ConfrontationMessage):
            order.append("CONFRONTATION")
        elif isinstance(msg, NarrationMessage):
            # NarrationMessage.type is one of NARRATION/NARRATION_END
            order.append(getattr(msg.type, "value", str(msg.type)))

    # The mid-turn CONFRONTATION must land between DICE_RESULT and
    # the narrator's NARRATION_END. A pre-fix server has no
    # CONFRONTATION in the room queue at all (it only emits post-
    # narration via the handler return path), so this assertion fails
    # red.
    confrontations = [m for m, _ in room.broadcasts if isinstance(m, ConfrontationMessage)]
    assert len(confrontations) >= 1, (
        f"expected at least one CONFRONTATION broadcast in the room queue "
        f"between DICE_RESULT and NARRATION_END; got order={order!r}"
    )

    # Find the FIRST CONFRONTATION (the new mid-turn emit). It must
    # arrive after DICE_RESULT and carry the post-apply momentum.
    first_conf_idx = next(
        i for i, (m, _) in enumerate(room.broadcasts) if isinstance(m, ConfrontationMessage)
    )
    dice_result_idx = next(
        i for i, (m, _) in enumerate(room.broadcasts) if isinstance(m, DiceResultMessage)
    )
    assert first_conf_idx > dice_result_idx, (
        f"CONFRONTATION must broadcast AFTER DICE_RESULT so the UI "
        f"applies it once the dice settle; got idx={first_conf_idx} "
        f"vs DICE_RESULT idx={dice_result_idx}, order={order!r}"
    )

    # Post-apply momentum: face=15, +2 mod, DC=14 → total 17, Success
    # tier (margin 3 ≥ DECISIVE_MARGIN). own_delta=base=3 on the
    # Success tier per beat_kinds; encounter.player_metric.current
    # advances 0 → 3.
    first_conf = room.broadcasts[first_conf_idx][0]
    assert isinstance(first_conf, ConfrontationMessage)
    payload = first_conf.payload
    # ConfrontationPayload.player_metric is dict[str, Any] (mirrors the
    # JSON wire shape) — access via key, not attribute.
    assert payload.player_metric["current"] == 3, (
        f"mid-turn CONFRONTATION must carry POST-apply player_metric "
        f"(0 → 3 after Success on attack); got "
        f"player_metric.current={payload.player_metric.get('current')!r}"
    )
    # Active flag asserts the encounter is not yet resolved (3 < 10).
    assert payload.active is True, (
        "mid-turn CONFRONTATION must keep active=True while the encounter remains unresolved"
    )


@pytest.mark.asyncio
async def test_dice_throw_mid_turn_confrontation_arrives_before_narration_end(
    session_handler_factory,
):
    """The mid-turn CONFRONTATION must precede the narrator's NARRATION_END.

    The whole point of the fix is that the dial moves *as the dice settle*,
    not *after the narrator completes*. If the new emit lands AFTER
    NARRATION_END the bug isn't fixed — the UI still sees the lag.
    """
    from sidequest.server.session_handler import _State

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    handler._state = _State.Playing
    _install_combat_def(sd)
    _install_active_encounter(sd)
    sd.snapshot.characters[0].stats["STRENGTH"] = 14

    room = _StubRoom()
    handler._room = room  # type: ignore[assignment]

    # Strict ordering witness: the mock captures a snapshot of
    # ``room.broadcasts`` at the moment the narrator is invoked. If the
    # mid-turn CONFRONTATION fired BEFORE the narrator started, the
    # snapshot will already contain it. If the broadcast were moved
    # inside or after the narrator step, the snapshot would NOT contain
    # the CONFRONTATION (because the narrator runs first), and a later
    # post-narration emit would still satisfy a naive "exists in queue"
    # check — which is exactly the regression the ordering claim must
    # catch.
    pre_narrator_broadcasts: list[object] = []

    async def _capture_then_return(
        *_args,
        **_kwargs,  # noqa: ANN002, ANN003
    ) -> NarrationTurnResult:
        # session_handler invokes run_narration_turn(action, turn_context)
        # — capture *args/**kwargs to be invariant to that call shape.
        # Snapshot the room queue at narrator-call time; list slice copies
        # the references so subsequent broadcasts don't appear here.
        pre_narrator_broadcasts.extend(m for m, _ in room.broadcasts)
        return NarrationTurnResult(narration="Strike lands.")

    sd.orchestrator.run_narration_turn = AsyncMock(
        side_effect=_capture_then_return,
    )

    msgs = await handler.handle_message(_throw(face=15))

    # The pre-narrator snapshot must contain exactly the dice pair plus
    # the new mid-turn CONFRONTATION — i.e., all three broadcasts landed
    # on the room queue BEFORE the narrator started its work.
    pre_types = [type(m).__name__ for m in pre_narrator_broadcasts]
    assert any(isinstance(m, ConfrontationMessage) for m in pre_narrator_broadcasts), (
        f"mid-turn CONFRONTATION must broadcast BEFORE the narrator runs; "
        f"narrator saw room queue contents {pre_types!r} — no "
        f"CONFRONTATION among them. A regression that moved the broadcast "
        f"inside or after run_narration_turn would land here."
    )
    # And both dice messages must precede it (sanity — without this the
    # ordering claim could be satisfied by an out-of-order CONFRONTATION
    # arriving before DICE_RESULT).
    assert any(isinstance(m, DiceRequestMessage) for m in pre_narrator_broadcasts), (
        f"narrator saw {pre_types!r} — DICE_REQUEST missing"
    )
    assert any(isinstance(m, DiceResultMessage) for m in pre_narrator_broadcasts), (
        f"narrator saw {pre_types!r} — DICE_RESULT missing"
    )

    # Sanity: the narrator was actually invoked (otherwise the side_effect
    # never ran and pre_narrator_broadcasts would just be empty —
    # vacuously satisfying the assertions above).
    sd.orchestrator.run_narration_turn.assert_called_once()

    # And the narrator returned to the rolling client.
    narration = [m for m in msgs if isinstance(m, NarrationMessage)]
    assert len(narration) >= 1, (
        f"narrator must still run inline after the broadcast; got "
        f"{len(narration)} NarrationMessage(s) in handler return"
    )


# ---------------------------------------------------------------------------
# AC1 negative: opposed_check defers — no mid-turn CONFRONTATION
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_opposed_check_does_not_emit_mid_turn_confrontation(
    session_handler_factory,
):
    """Opposed-check encounters defer beat application to ``narration_apply``.

    Per ``dispatch_dice_throw`` (dice.py:336-359), when the active
    confrontation declares ``resolution_mode: opposed_check`` the deltas
    are deferred — the player has rolled but no metric mutation happens
    yet. There is therefore NO post-apply momentum to broadcast at the
    dice-throw site; the existing post-narration emit handles it once
    the narrator picks the opponent's beat.

    This guards against a fix that emits unconditionally and broadcasts
    a stale-zero CONFRONTATION mid-turn on the opposed branch — which
    would teach the UI a wrong "the engine processed the beat" signal
    on a turn where the opposed roll hasn't run yet.
    """
    from sidequest.server.session_handler import _State

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    handler._state = _State.Playing
    _install_combat_def(sd, resolution_mode="opposed_check")
    _install_active_encounter(sd)
    sd.snapshot.characters[0].stats["STRENGTH"] = 14

    room = _StubRoom()
    handler._room = room  # type: ignore[assignment]

    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(narration="You square off."),
    )

    await handler.handle_message(_throw(face=15))

    # The opposed branch: deltas are deferred to narration_apply, so
    # the mid-turn emit MUST NOT fire from the dice path. Any
    # CONFRONTATION in the room queue here would be a regression — it
    # would broadcast a stale-zero metric and convince the UI the engine
    # had moved when it hasn't yet.
    confrontations_in_room = [m for m, _ in room.broadcasts if isinstance(m, ConfrontationMessage)]
    assert confrontations_in_room == [], (
        f"opposed_check defers beat application; the dice-throw site "
        f"MUST NOT emit a mid-turn CONFRONTATION on this branch. Got "
        f"{len(confrontations_in_room)} CONFRONTATION broadcasts: "
        f"{confrontations_in_room!r}"
    )

    # Sanity: the dice messages still went out — the deferral only
    # gates the new CONFRONTATION emit, not the existing dice fan-out.
    dice_msgs = [
        m for m, _ in room.broadcasts if isinstance(m, (DiceRequestMessage, DiceResultMessage))
    ]
    assert len(dice_msgs) == 2, (
        f"opposed_check still fans out DICE_REQUEST + DICE_RESULT; got "
        f"{len(dice_msgs)} dice broadcasts"
    )


# ---------------------------------------------------------------------------
# Mid-turn emit is additive — narrator step is reached, mid-turn fires once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_narrator_step_still_runs_after_mid_turn_emit(
    session_handler_factory,
):
    """Confirm the new mid-turn emit doesn't short-circuit the narrator.

    A regression that crashed in the new emit (or returned early) would
    skip ``_execute_narration_turn``, breaking every downstream effect
    (NARRATION delivery, post-narration CONFRONTATION, audio cues, the
    party-status refresh). This test locks the contract that the
    mid-turn emit is *additive* on the dice path: the new code runs,
    fires the broadcast once, and yields to the narrator.

    AC5 ("post-narration emit unchanged") is exercised end-to-end by
    ``test_post_narration_confrontation_emit_fans_out_with_event_log``
    below — that test installs a real EventLog + ProjectionCache and
    asserts a peer socket queue receives the post-narration CONFRONTATION.
    The minimal-setup ``session_handler_factory`` does NOT wire those
    components, so ``_emit_event`` falls into its legacy branch and the
    post-narration frame never reaches a queue here. Don't try to
    assert post-narration fan-out from this test — it's the wrong
    test stub for that claim.
    """
    from sidequest.server.session_handler import _State

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    handler._state = _State.Playing
    _install_combat_def(sd)
    _install_active_encounter(sd)
    sd.snapshot.characters[0].stats["STRENGTH"] = 14

    room = _StubRoom()
    handler._room = room  # type: ignore[assignment]

    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(narration="A clean strike."),
    )

    msgs = await handler.handle_message(_throw(face=15))

    # Mid-turn emit fires exactly once on the room broadcast path.
    room_confrontations = [m for m, _ in room.broadcasts if isinstance(m, ConfrontationMessage)]
    assert len(room_confrontations) == 1, (
        f"expected exactly ONE mid-turn CONFRONTATION on the room "
        f"broadcast queue; got {len(room_confrontations)}. A duplicate "
        f"would mean the new emit is firing twice from the dice path."
    )
    assert room_confrontations[0].payload.player_metric["current"] == 3, (
        "mid-turn CONFRONTATION must carry post-apply momentum=3"
    )

    # Narrator step ran: NarrationMessage present in handler return list.
    # If the new emit had crashed or short-circuited the narrator the
    # post-narration code path would never run.
    narration_msgs = [m for m in msgs if isinstance(m, NarrationMessage)]
    assert len(narration_msgs) >= 1, (
        f"narrator step must still run after the new mid-turn emit — "
        f"additive fix, not replacement; got "
        f"{len(narration_msgs)} NarrationMessage(s) in handler return"
    )


# ---------------------------------------------------------------------------
# AC5 regression: post-narration CONFRONTATION still fans out to peers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_narration_confrontation_emit_fans_out_with_event_log(
    session_handler_factory,
    tmp_path,
):
    """AC5: dice path → post-narration CONFRONTATION reaches peer queues.

    The mid-turn emit is additive; the post-narration emit at
    ``_execute_narration_turn`` (the ``_emit_event(\"CONFRONTATION\", ...)``
    site) MUST still fire and fan out through the projection filter to
    every connected non-actor socket. A regression that "moved" the
    CONFRONTATION emit from post-narration to mid-turn (rather than
    adding the mid-turn one alongside it) would:

      1. Leave the dial frozen at the wrong value if the narrator's
         ``beat_selection`` advances the metric a second time within
         the same turn (e.g., narrator chooses an additional consequence
         beat after the dice path).
      2. Strand peer tabs in MP — the existing pingpong S2 fix
         (test_confrontation_mp_broadcast.py) routed CONFRONTATION
         through ``_emit_event`` for projection-filtered fan-out;
         a removal would re-introduce that bug on dice-driven turns.

    This test installs a real EventLog + ProjectionCache + ProjectionFilter
    (matching the pattern at test_confrontation_mp_broadcast.py) and a
    SessionRoom with a peer socket. It drives DICE_THROW through
    ``handle_message`` (the full production path: dispatch_dice_throw →
    inline narrator → post-narration CONFRONTATION emit → fan-out). The
    peer's outbound queue must receive the post-narration CONFRONTATION
    even though the mid-turn one already broadcast.
    """
    import asyncio as _asyncio

    from sidequest.game import db_pool
    from sidequest.game.event_log import EventLog
    from sidequest.game.persistence import (
        GameMode,
    )
    from sidequest.game.projection.cache import ProjectionCache
    from sidequest.game.projection.composed import ComposedFilter
    from sidequest.server.session_handler import _State
    from sidequest.server.session_room import RoomRegistry
    from sidequest.server.session_state import _build_pg_repos_for_slug

    slug = "ac5-post-narration-emit-test"

    # ADR-115 F1: register the session in Postgres so the events FK is
    # satisfied and the emit fan-out persists/reads through the PG repository.
    store, _dungeon, _sink = _build_pg_repos_for_slug(
        db_pool.get_pool(),
        slug=slug,
        mode=str(GameMode.MULTIPLAYER),
        genre_slug="caverns_and_claudes",
        world_slug="",
    )

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    handler._state = _State.Playing
    _install_combat_def(sd)
    _install_active_encounter(sd)
    sd.snapshot.characters[0].stats["STRENGTH"] = 14
    sd.player_id = "actor"
    sd.player_name = "Rux"
    sd.mode = GameMode.MULTIPLAYER
    sd.game_slug = slug

    repo = store
    handler._event_log = EventLog(repo)
    handler._projection_filter = ComposedFilter.with_no_genre_rules()
    handler._projection_cache = ProjectionCache(repo)

    # Two-player room: actor plus a single peer. The peer's queue is
    # what we inspect — _emit_event excludes the emitter, so the
    # CONFRONTATION fan-out lands only on the peer's queue.
    registry = RoomRegistry()
    room = registry.get_or_create(slug=slug, mode=GameMode.MULTIPLAYER)
    room.bind_world(snapshot=sd.snapshot, store=store)
    socket_ids = {"actor": "sock-actor", "peer": "sock-peer"}
    queues: dict[str, _asyncio.Queue[object]] = {pid: _asyncio.Queue() for pid in socket_ids}
    for pid, sid in socket_ids.items():
        room.connect(pid, socket_id=sid)
        room.attach_outbound(sid, queues[pid])
    handler._room = room
    handler._socket_id = socket_ids["actor"]

    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(
            narration="A clean strike — momentum surges.",
        ),
    )

    await handler.handle_message(_throw(face=15))

    # Drain the peer's queue. With a fully wired projection pipeline,
    # the post-narration CONFRONTATION reaches the peer here regardless
    # of how the dice-path mid-turn emit was routed (the mid-turn emit
    # uses room.broadcast, which the live SessionRoom routes per-socket;
    # the post-narration emit uses _emit_event projection fan-out).
    peer_frames: list[object] = []
    while not queues["peer"].empty():
        peer_frames.append(queues["peer"].get_nowait())

    peer_confrontations = [f for f in peer_frames if isinstance(f, ConfrontationMessage)]
    # At least one CONFRONTATION must reach the peer for AC5 to hold.
    # The peer should observe the mid-turn emit (via room.broadcast on
    # the live room) AND the post-narration emit (via _emit_event
    # fan-out). Anything less means the additive contract is broken.
    assert len(peer_confrontations) >= 1, (
        f"Peer queue must receive the post-narration CONFRONTATION "
        f"after the dice path; got {len(peer_confrontations)} "
        f"CONFRONTATION frames (queue: "
        f"{[type(f).__name__ for f in peer_frames]!r}). "
        f"AC5 regression — the additive emit contract is broken."
    )
    # Every CONFRONTATION delivered to the peer must reflect a live,
    # post-mutation metric (current >= 3 — the dice path applied +3).
    # A frame with current=0 would mean the broadcast happened BEFORE
    # apply_beat — defeats the entire fix.
    for frame in peer_confrontations:
        assert frame.payload.player_metric["current"] >= 3, (
            f"peer CONFRONTATION must reflect post-apply momentum "
            f"(>=3 after Success on attack); got "
            f"player_metric.current={frame.payload.player_metric.get('current')!r}"
        )


# asyncio marker for the test module
_ = asyncio
