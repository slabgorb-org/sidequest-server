"""PlayerActionHandler — handles PLAYER_ACTION messages."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from sidequest.game.incapacitation import find_incapacitating_status
from sidequest.game.ruleset.without_number import is_dying_window_status
from sidequest.game.turn import TurnPhase
from sidequest.protocol import sanitize_player_text
from sidequest.protocol.messages import (
    GamePausedMessage,
    GamePausedPayload,
    TurnStatusMessage,
    TurnStatusPayload,
)
from sidequest.protocol.types import NonBlankString
from sidequest.server.session_handler import _State
from sidequest.server.session_helpers import (
    _build_turn_context,
    _emit_unbound_rejection_event,
    _error_msg,
    _resolve_acting_character_name,
)
from sidequest.server.turn_status_roster import (
    build_turn_status_roster,
    project_all_submitted,
)
from sidequest.telemetry.phase_timing import PhaseTimings
from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

if TYPE_CHECKING:
    from sidequest.protocol import GameMessage
    from sidequest.server.websocket_session_handler import WebSocketSessionHandler

logger = logging.getLogger(__name__)


def _bound_wn_slug(sd) -> str | None:
    """The bound ruleset slug for OTEL namespacing, or None when unavailable."""
    rules = getattr(getattr(sd, "genre_pack", None), "rules", None)
    ruleset = getattr(rules, "ruleset", None)
    return ruleset if isinstance(ruleset, str) else None


def _dying_window_expired(sd, status) -> bool:
    """True when a WWN dying window has passed its deadline (story 108-6, AC4).

    The deadline is ``created_turn + cfg.trauma.mortal_injury_rounds`` against the
    current interaction round. Reads the bound WN cfg from the session's genre
    pack (a capability gate, not a slug string — same idiom as the downed seam):
    with no Cwn/Wwn config bound there is no deadline to enforce, so the window is
    not expired this turn (it still blocks normal play via ``incapacitating``).
    """
    from sidequest.genre.models.rules import CwnConfig, WwnConfig

    rules = getattr(getattr(sd, "genre_pack", None), "rules", None)
    cfg = rules.ruleset_config() if rules is not None else None
    if not isinstance(cfg, (CwnConfig, WwnConfig)):
        return False
    deadline = status.created_turn + cfg.trauma.mortal_injury_rounds
    return sd.snapshot.turn_manager.interaction >= deadline


def initiative_preamble(encounter: object | None) -> str | None:
    """SWN P4: the authoritative resolution-order line for the narrator turn.

    Returns None when the encounter has no initiative (native rulesets, non-combat),
    OR when the encounter has already resolved. The 'reduced to 0 HP' clause keeps
    the prose correct before P5's tool walk mechanically enforces dead_premise.

    sq-playtest 2026-06-07 barsoom (#177 defect b): a resolved encounter
    (``check_hp_depletion`` sets ``resolved=True`` on a kill but leaves
    ``snapshot.encounter`` populated for forensics) still carried its
    ``initiative`` list, so the scaffold kept stamping ~10 rounds of looting
    and resting past the opponent's death. Gate on ``resolved`` to match the
    "active encounter" semantics used everywhere else
    (``enc is not None and not enc.resolved``).
    """
    if getattr(encounter, "resolved", False):
        return None
    init = getattr(encounter, "initiative", None)
    if not init:
        return None
    order = ", ".join(f"{e.token_id}({e.value})" for e in init)
    return (
        f"[INITIATIVE ORDER] Resolve the committed actions strictly in this "
        f"1d8+DEX order: {order}. An actor reduced to 0 HP earlier in this "
        f"order does not act."
    )


def _broadcast_cleared_to_party(
    room: object,
    party_members: list[dict[str, str]],
    *,
    round_no: int,
    reason: str,
) -> None:
    """Emit ACTION_REVEAL cleared for every party member.

    Called at barrier-fire (reason="dispatch") and on disconnect
    (reason="disconnect"). reason flows into OTEL only — the wire payload
    is identical regardless of cause. Sent with exclude_socket_id=None
    because the last-submitter's own row also needs to clear.
    """
    from sidequest.protocol.messages import (
        ActionRevealMessage,
        ActionRevealPayload,
        ActionRevealStatus,
    )

    for member in party_members:
        payload = ActionRevealPayload(
            player_id=member["player_id"],
            character_name=member["character_name"],
            status=ActionRevealStatus.CLEARED,
            action="",
            aside=False,
            seq=0,
            round=round_no,
        )
        msg = ActionRevealMessage(payload=payload)
        room.broadcast(msg, exclude_socket_id=None)
        _watcher_publish(
            "action_reveal.cleared",
            {
                "slug": room.slug,
                "player_id": member["player_id"],
                "round": round_no,
                "reason": reason,
            },
            component="multiplayer",
        )


def _broadcast_player_speech_to_party(
    room: object,
    pending: list[tuple[str, object]],
    *,
    round_no: int,
) -> None:
    """Surface each PC's verbatim spoken dialogue to the whole party.

    Playtest 2026-05-17 (Keith + Sebby): a player who typed quoted
    dialogue to an NPC was the only one who ever saw those words. The
    narrator cannot echo player speech (SOUL.md Agency) and the
    ACTION_REVEAL strip is wiped by ``_broadcast_cleared_to_party`` on
    barrier-fire, so peers lost the line entirely. Called at dispatch,
    just before the cleared broadcast: for every drained pending action
    that contains quoted spans, emit one PLAYER_SPEECH per span,
    attributed to the speaking PC, to every socket (exclude_socket_id
    =None — the speaker's own transcript shows their line too). Actions
    with no quotes produce nothing — that was narration, not speech.
    """
    from sidequest.agents.pov_swap import extract_spoken_lines
    from sidequest.protocol.messages import PlayerSpeechMessage, SpokenLinePayload

    for pid, p in pending:
        character_name = getattr(p, "character_name", "")
        action = getattr(p, "action", "")
        spoken = extract_spoken_lines(action)
        for idx, line in enumerate(spoken):
            payload = SpokenLinePayload(
                character_name=character_name,
                text=line,
                round=round_no,
            )
            msg = PlayerSpeechMessage(payload=payload, player_id=pid)
            room.broadcast(msg, exclude_socket_id=None)
            _watcher_publish(
                "mp.player_speech",
                {
                    "slug": room.slug,
                    "player_id": pid,
                    "character_name": character_name,
                    "round": round_no,
                    "line_index": idx,
                    "text_length": len(line),
                },
                component="multiplayer",
            )


async def dispatch_fired_barrier(
    session: WebSocketSessionHandler,
    *,
    sd: object,
    snapshot: object,
    turn_context: object,
    playing_count: int,
    timings: PhaseTimings,
) -> list[object]:
    """Elect a single dispatcher, drain the buffer, and run the narrator turn.

    Shared by the PLAYER_ACTION barrier-fire path (the last submission flips
    the barrier) and the Story 67-1 CLIENT_ERROR crash-release path (a crash
    drops the last awaited player so the existing submissions satisfy the
    barrier). Both arrive here only once the barrier has already flipped off
    ``InputCollection``; this owns the one-dispatch-per-interaction CAS, the
    player-speech surfacing, the ``mp.round_dispatched`` span, and the combined
    multi-PC action assembly.
    """
    # Elect a single dispatcher per round via asyncio.Lock + the
    # last_dispatched_round CAS guard.
    async with session._room.dispatch_lock:
        # CAS guard uses interaction (monotonic per-narration counter) not
        # round (which advances on narrative beats, not every turn).
        current_interaction = snapshot.turn_manager.interaction
        if session._room.last_dispatched_round >= current_interaction:
            # Lost the race; another handler already dispatched.
            return []
        session._room.last_dispatched_round = current_interaction
        # Capture the barrier-wait BEFORE drain — drain clears it. The
        # timestamp was stamped on the first submission into an empty buffer;
        # "now minus that" is the wall-clock the dispatching player actually
        # waited for the slowest peer.
        barrier_wait_started_at = session._room.first_pending_at_monotonic()
        pending = session._room.drain_pending_actions()
    if barrier_wait_started_at is not None:
        wait_ms = max(
            0,
            round((time.monotonic() - barrier_wait_started_at) * 1000),
        )
        timings.record_phase("mp_barrier_wait", wait_ms)

    # Playtest 2026-05-17: surface each PC's verbatim spoken dialogue to the
    # whole party before the narrator dispatch, so peers see what was said
    # aloud (the narrator can't echo it per SOUL.md Agency).
    _broadcast_player_speech_to_party(
        session._room,
        pending,
        round_no=snapshot.turn_manager.round,
    )

    _watcher_publish(
        "mp.round_dispatched",
        {
            "slug": session._room.slug,
            "round": snapshot.turn_manager.round,
            # Story 45-2: report the count the barrier actually used (playing
            # peers), not the raw seat dict size.
            "player_count": playing_count,
            "action_lengths": {pid: len(p.action) for pid, p in pending},
            "combined_action_len": (
                sum(len(p.action) for _, p in pending)
                + sum(len(p.character_name) + 2 for _, p in pending)
            ),
        },
        component="multiplayer",
    )

    combined_action = "\n".join(f"{p.character_name}: {p.action}" for _, p in pending)
    _encounter = getattr(snapshot, "encounter", None)
    _preamble = initiative_preamble(_encounter)
    if _preamble is not None:
        combined_action = f"{_preamble}\n{combined_action}"
    elif (
        _encounter is not None
        and getattr(_encounter, "resolved", False)
        and getattr(_encounter, "initiative", None)
    ):
        # #177 defect (b) lie-detector: the encounter resolved (a kill set
        # ``resolved=True``) but still lingers in ``snapshot.encounter`` with a
        # populated initiative list. We correctly DECLINE to stamp the
        # [INITIATIVE ORDER] scaffold. Surface it so the GM panel can verify the
        # scaffold stopped firing post-resolution — and see the deeper smell of a
        # resolved encounter never cleared from the snapshot.
        _watcher_publish(
            "initiative_preamble_suppressed",
            {
                "slug": session._room.slug,
                "round": snapshot.turn_manager.round,
                "reason": "encounter_resolved",
                "encounter_type": getattr(_encounter, "encounter_type", ""),
                "outcome": getattr(_encounter, "outcome", "") or "",
            },
            component="confrontation",
        )
    # Tag the TurnContext so build_narrator_prompt renders a multi-PC
    # declaration block instead of attributing every line to the dispatch
    # winner.
    turn_context.merged_player_actions = [(p.character_name, p.action) for _, p in pending]
    return await session._execute_narration_turn(
        sd,
        combined_action,
        turn_context,
    )


class PlayerActionHandler:
    """Handle a PLAYER_ACTION message (player narration submission).

    Sanitizes the action text, enforces the MP pause gate, broadcasts
    TURN_STATUS{active}, manages the sealed-letter barrier in multiplayer,
    and dispatches to ``_execute_narration_turn`` once all seated players
    have submitted their actions.
    """

    async def handle(
        self,
        session: WebSocketSessionHandler,
        msg: GameMessage,
    ) -> list[object]:
        # Start the per-turn phase timer at handler entry so pre-narrator
        # work — lore RAG retrieval, MP barrier wait, turn context build —
        # is captured in the same `phase_durations_ms` the dashboard
        # already renders. Pre-fix this clock started inside
        # `_execute_narration_turn`, so any wait before it (especially
        # the MP barrier when one player is slow) was invisible to the
        # GM panel and to player-perceived-latency analysis.
        timings = PhaseTimings(action_received_monotonic=time.monotonic())
        if session._state not in (_State.Creating, _State.Playing):
            # Playtest 2026-04-30: uvicorn ``--reload`` zombies session
            # binding. The client transport reconnects automatically
            # but the SESSION_EVENT{connect} that would re-bind the
            # session sometimes never lands (effect timing, message
            # drop during close-then-reopen, etc.). Tag the rejection
            # with ``code="session_unbound"`` so the client can detect
            # the unbound state and re-fire SESSION_EVENT{connect}
            # from its saved slug — protocol-level recovery that works
            # regardless of the underlying race.
            logger.info(
                "session.message_rejected_unbound type=PLAYER_ACTION state=%s",
                session._state.name,
            )
            # Story 67-7 (AC5): surface to the GM panel so a genuine unbound
            # guard is distinguishable from reconnect churn. Co-located with the
            # session_unbound branch only — the Creating-state / data-missing
            # rejection below is a different class and must not borrow this tag.
            _emit_unbound_rejection_event("PLAYER_ACTION", session._state.name)
            return [
                _error_msg(
                    "Cannot process PLAYER_ACTION: not connected",
                    code="session_unbound",
                ),
            ]

        if session._session_data is None:
            return [_error_msg("Internal error: session data missing")]

        payload = msg.payload  # type: ignore[attr-defined]
        raw_action: str = str(payload.action)

        # Sanitize player input
        action = sanitize_player_text(raw_action)
        if not action:
            return [_error_msg("Player action is empty after sanitization")]

        # ADR-107 (story 50-25): out-of-band aside channel. An aside is OOC
        # table-talk, NOT a turn. Branch here — the earliest point the
        # server knows aside=true — *before* the pause gate, the
        # TURN_STATUS{active} broadcast, and the ADR-036 barrier. The
        # resolver is read-only (no write path), so "no turn consumed" is
        # structural, not a thing we remember not to do. We RETURN before
        # any of that machinery: pending_actions, dispatch_lock, turn
        # counter, scrapbook and world are never touched.
        if getattr(payload, "aside", False):
            from sidequest.server.dispatch.combat_brackets import (
                strip_combat_brackets,
            )

            question = strip_combat_brackets(action).strip()
            if not question:
                return [_error_msg("Player aside is empty after combat-bracket strip")]

            from sidequest.agents.aside_resolver import (
                AsideReadView,
                AsideResolver,
                resolve_aside_on_narrator_cache,
            )
            from sidequest.agents.llm_factory import build_aside_llm
            from sidequest.protocol.messages import (
                AsideAnswerMessage,
                AsideAnswerPayload,
            )
            from sidequest.telemetry.setup import tracer
            from sidequest.telemetry.spans import SPAN_ASIDE_RESOLVE

            sd_aside = session._session_data
            snap = sd_aside.snapshot
            # Story 91-4: key the aside's LLM spend to the canonical
            # session id (room slug first, sd.game_slug fallback — the same
            # resolution the narrator's cost machinery uses) so it runs the
            # ADR-134 detector and counts against the per-session cumulative
            # ceiling. Both None should be unreachable for a connected
            # player; fail loud rather than silently constructing an
            # uncovered spender (No Silent Fallbacks).
            if session._room is not None:
                aside_session_id = session._room.slug
            elif sd_aside.game_slug is not None:
                aside_session_id = sd_aside.game_slug
            else:
                raise RuntimeError(
                    "aside resolve fired without a bound session id (no room "
                    "and no sd.game_slug) — refusing to construct an "
                    "uncovered LLM caller (story 91-4, No Silent Fallbacks)."
                )
            # Aside-rides-the-cache (playtest 2026-06-07, ADR-107 re-scope):
            # prefer re-presenting the narrator's exact cached prompt prefix
            # (stashed by the orchestrator every SDK turn) so the aside
            # knows everything the narrator knows. Fall back to the legacy
            # thin read-view when no SDK turn has run yet or the backend
            # has no tool loop.
            orch = sd_aside.orchestrator
            stash = orch.aside_prompt_stash
            cache_client = orch.aside_cache_client
            with tracer().start_as_current_span(SPAN_ASIDE_RESOLVE) as span:
                t0 = time.monotonic()
                if stash is not None and cache_client is not None:
                    res, cache_read_tokens = await resolve_aside_on_narrator_cache(
                        client=cache_client,
                        stash=stash,
                        question=question,
                        session_id=aside_session_id,
                    )
                    span.set_attribute("path", "narrator_cache")
                    span.set_attribute("cache_hit", cache_read_tokens > 0)
                    span.set_attribute("cache_read_tokens", cache_read_tokens)
                    span.set_attribute("model", stash.model)
                else:
                    reason = "no_narrator_prompt_stash" if stash is None else "non_tooling_client"
                    logger.info("aside.legacy_read_view reason=%s", reason)
                    # Mirror the normal-path guard (Reviewer RT1): an aside
                    # must degrade like a real action if acting-name
                    # resolution raises, not crash the handler.
                    try:
                        char_name = _resolve_acting_character_name(sd_aside, session._room)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "session.aside_acting_name_resolve_failed error=%s falling_back_to=%s",
                            exc,
                            sd_aside.player_name,
                        )
                        char_name = sd_aside.player_name
                    core = next(
                        (c.core for c in snap.characters if c.core.name == char_name),
                        None,
                    )
                    character_summary = (
                        f"{core.name}: {core.description}" if core is not None else char_name
                    )
                    inventory: list[str] = []
                    if core is not None:
                        for item in core.inventory.items:
                            name = item.get("name") if isinstance(item, dict) else None
                            inventory.append(str(name) if name else str(item))
                    region = (
                        snap.party_location(perspective=char_name)
                        or snap.party_location()
                        or "(location unstated)"
                    )
                    confrontations = (
                        sd_aside.genre_pack.rules.confrontations
                        if sd_aside.genre_pack.rules
                        else []
                    )
                    rulebook = (
                        f"Genre {sd_aside.genre_slug}. Confrontations: "
                        + ", ".join(c.label for c in confrontations)
                        if confrontations
                        else f"Genre {sd_aside.genre_slug}."
                    )
                    recent = " ".join(e.content for e in snap.narrative_log[-4:] if e.content)
                    read_view = AsideReadView(
                        character_summary=character_summary,
                        region_summary=region,
                        inventory=inventory,
                        rulebook_summary=rulebook,
                        recent_narration=recent,
                    )
                    res = await AsideResolver(
                        llm=build_aside_llm(session_id=aside_session_id)
                    ).resolve(question=question, read_view=read_view)
                    span.set_attribute("path", "legacy_read_view")
                    span.set_attribute("model", "haiku")
                span.set_attribute("asker_id", sd_aside.player_id or "")
                span.set_attribute("outcome", res.outcome)
                span.set_attribute("grounded_on", ",".join(res.grounded_on))
                span.set_attribute("latency_ms", int((time.monotonic() - t0) * 1000))
            answer_msg = AsideAnswerMessage(
                payload=AsideAnswerPayload(
                    asker_id=sd_aside.player_id or "",
                    question=question,
                    answer=res.answer,
                    grounded_on=list(res.grounded_on),
                    round=snap.turn_manager.round,
                ),
                player_id=sd_aside.player_id or "",
            )
            # Table-visible (spec §5): mirror _broadcast_player_speech_to_party
            # — exclude_socket_id=None so the asker's own transcript shows the
            # Q&A too. Out-of-band: NEVER enqueue/advance/scrapbook/patch.
            if session._room is not None:
                session._room.broadcast(answer_msg, exclude_socket_id=None)
                return []
            return [answer_msg]

        logger.info(
            "session.player_action genre=%s world=%s player=%s action_len=%d",
            session._session_data.genre_slug,
            session._session_data.world_slug,
            session._session_data.player_name,
            len(action),
        )

        # Pause gate (MP-02 Task 6): if any seated player is absent, return
        # GAME_PAUSED and do NOT dispatch to the narrator. This must run
        # BEFORE _execute_narration_turn so the monkeypatch gate in tests
        # confirms the method is never reached when paused. _room is None
        # when slug-connect hasn't bound a room (legacy connect path or
        # pre-connect test paths) — pause gate is a no-op in that case.
        if session._room is not None and session._room.is_paused():
            from sidequest.telemetry.spans import mp_player_action_paused_span

            absent = session._room.absent_seated_player_ids()
            player_id_attr = session._session_data.player_id if session._session_data else ""
            with mp_player_action_paused_span(
                slug=session._room.slug,
                player_id=player_id_attr,
                absent_player_ids=absent,
            ):
                logger.info(
                    "session.player_action_blocked_paused absent=%s slug=%s",
                    absent,
                    session._room.slug,
                )
            return [GamePausedMessage(payload=GamePausedPayload(waiting_for=absent))]

        # Transition to Playing on first action (handles chargen via narration)
        if session._state == _State.Creating:
            session._state = _State.Playing

        sd = session._session_data
        # MP turn-ownership signal (ADR-036 sealed-letter pacing). Broadcast
        # TURN_STATUS{status="active"} to every socket in the room so peers
        # can flip MultiplayerTurnBanner to tone="peer" while this player's
        # narration runs. Without this signal, peer tabs stayed on tone="you"
        # and gave no indication that another player was acting (playtest
        # 2026-04-25 "No peer-turn signal"). exclude_socket_id=None — the
        # actor receives it too; their banner already prefers "thinking" over
        # "you" while ``thinking=true`` is local.
        # Hoist acting_name so the buffer write below can reference it even
        # when _resolve_acting_character_name raises (falls back to player_name).
        try:
            acting_name = (
                _resolve_acting_character_name(sd, session._room)
                if session._room is not None and sd.player_name
                else sd.player_name
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "session.acting_name_resolve_failed error=%s falling_back_to=%s",
                exc,
                sd.player_name,
            )
            acting_name = sd.player_name

        # Incapacitation gate (sq-playtest 2026-06-07 barsoom-3, blocking): a PC
        # the genre lethality policy ruled dead/dying must NOT keep submitting
        # actions to the narrator. post_resolution_lethality stamped an
        # ``incapacitating`` status on the downed PC; this gate is the durable
        # lock. It runs BEFORE the TURN_STATUS{active} broadcast and the narrator
        # dispatch so a dead seat consumes no turn and the band plays on (SOUL.md
        # The Guitar Solo — only THIS seat locks; peers keep acting). We re-send
        # the death surface so a reconnecting/confused client re-renders the
        # banner, and emit the lie-detector span.
        downed_core = next(
            (c.core for c in sd.snapshot.characters if c.core.name == acting_name),
            None,
        )
        downed_status = find_incapacitating_status(downed_core) if downed_core is not None else None
        # Story 108-6 dying-window carve. A stabilizable window is incapacitating
        # (the PC can't take normal actions) but the downed soloist may still ACT
        # (free-text) to try to stabilize — and each submission spends a round on
        # the engine-owned clock. Two outcomes on the player's own turn:
        #   - deadline passed → convert the window to terminal-dead and block
        #     (AC4: stalling can't pause the clock), then fall through to the
        #     existing terminal block below.
        #   - still within the window → emit the per-round tick (lie-detector for
        #     the clock) and PERMIT the action — route it to the narrator.
        if (
            downed_status is not None
            and downed_core is not None
            and is_dying_window_status(downed_status)
        ):
            from sidequest.game.status import Status, StatusSeverity
            from sidequest.telemetry.spans.wn import (
                dying_window_resolved_span,
                dying_window_tick_span,
            )

            slug = _bound_wn_slug(sd) or "wwn"
            rounds_elapsed = max(
                0, sd.snapshot.turn_manager.interaction - downed_status.created_turn
            )
            if _dying_window_expired(sd, downed_status):
                downed_core.statuses = [
                    s for s in downed_core.statuses if not is_dying_window_status(s)
                ]
                terminal = Status(
                    text="Downed — dead (mortally wounded)",
                    severity=StatusSeverity.Scar,
                    created_turn=downed_status.created_turn,
                    created_in_encounter=downed_status.created_in_encounter,
                    incapacitating=True,
                )
                downed_core.statuses.append(terminal)
                dying_window_resolved_span(
                    ruleset=slug,
                    actor=acting_name,
                    outcome="died",
                    final_rounds_elapsed=rounds_elapsed,
                    resulting_status="terminal-dead",
                )
                downed_status = terminal  # fall through to the terminal block
            else:
                dying_window_tick_span(
                    ruleset=slug,
                    actor=acting_name,
                    rounds_elapsed=rounds_elapsed,
                    difficulty=8 + rounds_elapsed,
                    action_was_stabilization=False,
                )
                downed_status = None  # permit — route to the narrator
        if downed_status is not None:
            from sidequest.server.post_resolution_lethality import (
                build_incapacitated_message,
                verdict_from_status_text,
            )
            from sidequest.telemetry.spans.encounter import (
                player_action_blocked_incapacitated_span,
            )

            verdict = verdict_from_status_text(downed_status.text)
            with player_action_blocked_incapacitated_span(
                character=acting_name,
                verdict=verdict,
                status_text=downed_status.text,
            ):
                pass
            logger.info(
                "session.player_action_blocked_incapacitated character=%s verdict=%s "
                "status=%s slug=%s",
                acting_name,
                verdict,
                downed_status.text,
                session._session_data.game_slug,
            )
            _watcher_publish(
                "state_transition",
                {
                    "field": "session.player_action_blocked_incapacitated",
                    "character": acting_name,
                    "verdict": verdict,
                    "status_text": downed_status.text,
                },
                component="encounter",
            )
            return [
                build_incapacitated_message(
                    character_name=acting_name,
                    verdict=verdict,
                    status_text=downed_status.text,
                    player_id=sd.player_id or "",
                )
            ]

        if session._room is not None and sd.player_name:
            try:
                # Canonical roster on the wire — every recipient agrees on
                # the denominator. The submitter hasn't called submit_input
                # yet at this point, so they appear as "pending" in the
                # roster — correct (active broadcast precedes the buffer
                # write / barrier.submit at lines below).
                active_roster = build_turn_status_roster(
                    sd.snapshot, session._room.playing_player_ids()
                )
                turn_active_msg = TurnStatusMessage(
                    payload=TurnStatusPayload(
                        player_name=NonBlankString(acting_name),
                        status="active",
                        entries=active_roster,
                    ),
                    player_id=sd.player_id or "",
                )
                session._room.broadcast(turn_active_msg, exclude_socket_id=None)
                logger.info(
                    "session.turn_status_active player=%s player_id=%s slug=%s",
                    acting_name,
                    sd.player_id,
                    session._room.slug,
                )
                _watcher_publish(
                    "turn_status",
                    {
                        "status": "active",
                        "player_name": acting_name,
                        "player_id": sd.player_id,
                        "slug": session._room.slug,
                    },
                    component="session",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("session.turn_status_active_broadcast_failed error=%s", exc)

        with timings.phase("lore_retrieval"):
            lore_context = await session._retrieve_lore_for_turn(sd, action)
        with timings.phase("entity_retrieval"):
            entity_retrieval = await session._retrieve_entities_for_turn(sd, action)
        with timings.phase("turn_context_build"):
            turn_context = _build_turn_context(
                sd,
                lore_context=lore_context,
                entity_retrieval=entity_retrieval,
                room=session._room,
            )
        # Attach the handler-entry timer so `_execute_narration_turn`
        # accumulates its in-turn phases into the same instance.
        turn_context.phase_timings = timings

        # ADR-036 Cinematic mode wiring. In multiplayer, every player's
        # submission goes into the per-room buffer and calls submit_input()
        # on the TurnManager barrier. If the barrier hasn't fired yet
        # (still in InputCollection), this handler returns []; another
        # player's later submission will fire the barrier and dispatch the
        # narrator with the combined action. Solo rooms (Story 45-2:
        # playing_player_count() == 1 — a solo player must reach PLAYING
        # via _chargen_confirmation before the barrier fires on their
        # first action) flip the barrier on the first submission and
        # continue into the elected branch immediately — zero overhead.
        if session._room is not None:
            snapshot = sd.snapshot
            session._room.record_pending_action(
                sd.player_id,
                acting_name,
                action,
            )
            # Story 45-2: barrier counts PLAYING peers only — chargen /
            # abandoned seats do not block. The non-abandoned count is
            # captured alongside for the GM panel's lie-detector (Sebastien
            # sees the lobby_participant_count vs active_turn_count
            # divergence — abandoned seats are NOT counted as participants
            # because they're reclaimable orphans, not active lobby members).
            # Story 67-1: the denominator also drops players crash-released
            # this interaction, so a client that crashed earlier this turn
            # does not keep the barrier waiting on a submission that will
            # never come (effective_barrier_count = PLAYING − crash-released).
            playing_count = session._room.effective_barrier_count()
            lobby_participant_count = session._room.non_abandoned_player_count()
            snapshot.turn_manager.set_player_count(playing_count)
            snapshot.turn_manager.submit_input(sd.player_id)
            submitted_count = len(object.__getattribute__(snapshot.turn_manager, "_submitted"))
            barrier_fired = snapshot.turn_manager.get_phase() != TurnPhase.InputCollection
            # Story 45-2 AC4: barrier.wait fires on EVERY barrier check —
            # not only on barrier_fired transitions. A wait that never
            # fires is exactly the bug being fixed; if the span only
            # emits on fire, the GM panel can't see why the wait persists.
            _watcher_publish(
                "barrier.wait",
                {
                    "slug": session._room.slug,
                    "interaction_id": snapshot.turn_manager.interaction,
                    "lobby_participant_count": lobby_participant_count,
                    "active_turn_count": playing_count,
                    "submitted_count": submitted_count if not barrier_fired else playing_count,
                    "fired": barrier_fired,
                    "submitter_player_id": sd.player_id,
                },
                component="multiplayer",
            )
            # Broadcast TURN_STATUS{status="submitted"} so the sealed-letter
            # strip on every peer tab can flip this player's entry from
            # "Composing…" (pending) to "✓ Sealed" (submitted). Without
            # this, the only TurnStatusPayload statuses the server ever
            # emitted were "active" (at action receipt) and "resolved" (at
            # narration completion) — there was no per-player submission
            # signal in between, so the panel stayed stuck on "(0/N)"
            # forever even after every PC had submitted (sq-playtest
            # 2026-05-11 [BUG-LOW] caverns_sunden MP). Emitting *after* the
            # buffer write and barrier.wait so submitted_count above is
            # already consistent with this player counting as sealed.
            if sd.player_name:
                try:
                    # Roster snapshot after submit_input — this submitter
                    # now appears as "submitted". If the barrier just fired
                    # the turn_manager._submitted set was cleared on phase
                    # transition (turn.py:98); in that case the roster
                    # would show everyone as "pending" again, which would
                    # contradict the per-tab visual of "all sealed". Use
                    # an explicit all-submitted projection when
                    # barrier_fired so the UI sees the round's terminal
                    # state on this final broadcast.
                    submitted_roster = build_turn_status_roster(
                        snapshot, session._room.playing_player_ids()
                    )
                    if barrier_fired:
                        # Barrier fired → _submitted was cleared on the phase
                        # transition; project the round's terminal all-submitted
                        # state (shared with the on-connect seal reconcile).
                        submitted_roster = project_all_submitted(submitted_roster)
                    submitted_msg = TurnStatusMessage(
                        payload=TurnStatusPayload(
                            player_name=NonBlankString(acting_name),
                            status="submitted",
                            entries=submitted_roster,
                        ),
                        player_id=sd.player_id or "",
                    )
                    session._room.broadcast(submitted_msg, exclude_socket_id=None)
                    _watcher_publish(
                        "turn_status",
                        {
                            "status": "submitted",
                            "player_name": acting_name,
                            "player_id": sd.player_id,
                            "slug": session._room.slug,
                        },
                        component="session",
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("session.turn_status_submitted_broadcast_failed error=%s", exc)
            if barrier_fired:
                # Barrier just fired on this submission — emit before the
                # dispatch CAS so a failed dispatch still leaves the
                # barrier-fired event visible.
                _watcher_publish(
                    "mp.barrier_fired",
                    {
                        "slug": session._room.slug,
                        "round": snapshot.turn_manager.round,
                        "player_count": playing_count,
                        "submitter_player_id": sd.player_id,
                    },
                    component="multiplayer",
                )
            if snapshot.turn_manager.get_phase() == TurnPhase.InputCollection:
                # Still waiting on other PLAYING players. Broadcasts already
                # delivered turn_status_active above; the dispatcher will
                # handle the actual narration when the last submission arrives.
                return []

            # Barrier fired — elect a dispatcher, drain, and narrate. Sealed
            # reveals are NOT cleared here (Playtest 2026-05-17, Keith): wiping
            # them blanked the whole table during the narrator-thinking gap.
            # They stay visible until the client flushes on NARRATION_END.
            return await dispatch_fired_barrier(
                session,
                sd=sd,
                snapshot=snapshot,
                turn_context=turn_context,
                playing_count=playing_count,
                timings=timings,
            )

        # Single-player path (room is None) — preserve original behavior.
        return await session._execute_narration_turn(sd, action, turn_context)


HANDLER = PlayerActionHandler()
