"""DICE_THROW dispatch — physics-is-the-roll resolution + beat application.

Port of the DICE_THROW arm of sidequest-api/crates/sidequest-server/src/lib.rs
(and the pure helper at dice_dispatch.rs::handle_dice_throw).

Wire flow (matches Rust):
1. Rolling client clicks a confrontation beat, UI builds ``DiceRequestPayload``
   locally (no server round-trip), auto-rolls in Rapier, reads settled faces.
2. UI sends ``DICE_THROW { request_id, throw_params, face, beat_id? }``.
3. Server (here) applies the beat to the active encounter, validates inputs,
   resolves dice from the client-reported faces, broadcasts DICE_REQUEST +
   DICE_RESULT to the room, stashes the resolved outcome + a replay-action
   on the session, and synthesizes a narrator input that describes what
   happened mechanically.
4. The session handler then runs the narrator inline so the playtest UX is
   one click → dice result + narration, not two separate user actions.

Broadcast (not return): the session room's broadcast() puts the message into
every connected socket's outbound queue. Returning these from handle_message
too would double-send to the rolling player.
"""

from __future__ import annotations

import logging
import random
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from sidequest.game.beat_kinds import apply_beat
from sidequest.game.dice import ResolveError, generate_dice_seed, resolve_dice_with_faces
from sidequest.game.encounter import EncounterPhase, StructuredEncounter
from sidequest.game.session import GameSnapshot
from sidequest.genre.models.inventory import DamageSpec
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.rules import BeatDef, ConfrontationDef, ResolutionMode
from sidequest.protocol.dice import (
    DiceRequestPayload,
    DiceResultPayload,
    DiceThrowPayload,
    DieGroupResult,
    DieSides,
    DieSpec,
    RollOutcome,
    ThrowParams,
)
from sidequest.protocol.messages import (
    ConfrontationMessage,
    ConfrontationPayload,
    DiceRequestMessage,
    DiceResultMessage,
)
from sidequest.protocol.types import Stat
from sidequest.server.dispatch.confrontation import (
    build_confrontation_payload,
    find_confrontation_def,
)
from sidequest.telemetry.spans import (
    combat_tick_span,
    emit_dice_request_sent,
    emit_dice_result_broadcast,
    emit_dice_throw_received,
    encounter_beat_applied_span,
    encounter_momentum_broadcast_span,
    encounter_resolved_span,
)
from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

logger = logging.getLogger(__name__)

_DICE_RE = re.compile(r"^(?P<count>\d+)d(?P<faces>\d+)$")


class DiceDispatchError(Exception):
    """A DICE_THROW could not be resolved.

    Wrapper for validation / resolution failures so the session handler can
    surface them as ERROR messages without string-matching the exception
    chain.
    """


@dataclass(frozen=True)
class DiceThrowOutcome:
    """Result of a successful DICE_THROW dispatch.

    Carries everything the session handler needs to: broadcast the dice
    messages, stash the outcome on session state, and synthesize a narrator
    replay action.

    Opposed-check fields (combat fairness, 2026-04-26):

    - ``opposed_pending``: True when this dispatch deferred beat
      application because the active confrontation declares
      ``resolution_mode: opposed_check``. The player rolled, but the
      engine has NOT yet applied the beat — it will run via
      ``narration_apply`` once the narrator picks the opponent's beat
      and the resolver derives the tier from the shift between rolls.
    - ``opposed_player_d20``: The raw d20 face value (1..=20) the player
      rolled. Stashed on ``_SessionData`` so ``narration_apply`` can feed
      it into ``resolve_opposed_check``.
    - ``opposed_player_beat_id``: The beat the player committed. Stashed
      so the dispatch branch can reconstruct which BeatDef to apply
      after the tier is derived.

    All three fields are ``None`` / ``False`` when the confrontation is
    NOT opposed_check — the legacy single-roll-vs-DC path is unchanged.
    """

    request: DiceRequestPayload
    result: DiceResultPayload
    replay_action_text: str
    outcome: RollOutcome
    encounter_resolved: bool
    opposed_pending: bool = False
    opposed_player_d20: int | None = None
    opposed_player_beat_id: str | None = None


def _stat_modifier(stats: dict[str, int], stat_check: str) -> int:
    """D&D-style modifier: ``floor((score - 10) / 2)``.

    Matches Rust ``(stat_val - 10) / 2`` with integer truncation. Missing
    stats default to 0 (stat score 10) — same as Rust's ``unwrap_or(0)``.
    """
    score = stats.get(stat_check)
    if score is None:
        # Try case-insensitive fallback since the character's stats dict
        # may be UPPERCASE or TitleCase depending on the genre pack.
        for k, v in stats.items():
            if k.upper() == stat_check.upper():
                score = v
                break
    if score is None:
        return 0
    return (score - 10) // 2


def _compute_dc(beat: BeatDef) -> int:
    """Derive DC from beat ``base`` magnitude, clamped 10..=30.

    Port of Rust ``(10u32 + beat_metric_delta.unsigned_abs() * 2).clamp(10, 30)``.
    Big metric swings need bigger checks — the clamp keeps any degenerate
    pack data from producing an unreachable or trivial DC.
    """
    return max(10, min(30, 10 + abs(beat.base) * 2))


def _build_request_payload(
    *,
    request_id: str,
    rolling_player_id: str,
    character_name: str,
    stat: Stat,
    modifier: int,
    difficulty: int,
    context: str,
) -> DiceRequestPayload:
    """Shape a DiceRequest matching the client-local build so overlays sync."""
    return DiceRequestPayload(
        request_id=request_id,
        rolling_player_id=rolling_player_id,
        character_name=character_name,
        dice=[DieSpec(sides=DieSides.D20, count=1)],
        modifier=modifier,
        stat=stat,
        difficulty=difficulty,
        context=context,
    )


def _compose_result_payload(
    *,
    request: DiceRequestPayload,
    rolls: list[DieGroupResult],
    total: int,
    outcome: RollOutcome,
    seed: int,
    throw_params: ThrowParams,
) -> DiceResultPayload:
    return DiceResultPayload(
        request_id=request.request_id,
        rolling_player_id=request.rolling_player_id,
        character_name=request.character_name,
        rolls=rolls,
        modifier=request.modifier,
        total=total,
        difficulty=request.difficulty,
        outcome=outcome,
        seed=seed,
        throw_params=throw_params,
    )


def _format_replay_action(
    *,
    beat_label: str,
    stat_check: str,
    actor_side: str,
    player_metric_after: int,
    opponent_metric_after: int,
    total: int,
    outcome: RollOutcome,
    player_action: str | None = None,
) -> str:
    """Synthetic narrator input describing the mechanical beat result.

    Keeps the shape Rust produces (``[BEAT_RESOLVED] ...``) plus a roll
    summary so the narrator knows the total and outcome without having to
    re-derive them from footnotes.

    When ``player_action`` is provided (the freeform text the player
    typed into the InputBar before clicking a beat tile, D2 mock
    2026-05-13), it's prepended as a ``PLAYER_ACTION:`` line so the
    narrator runs with both the mechanical context AND the player's
    invention. Empty / whitespace-only strings are treated the same as
    None — we don't surface a blank cue to the narrator.
    """
    beat_summary = (
        f"[BEAT_RESOLVED] {beat_label} ({stat_check}, side={actor_side}): "
        f"player_momentum={player_metric_after} | "
        f"opponent_momentum={opponent_metric_after} | "
        f"Roll: {total} ({outcome.value})"
    )
    if player_action and player_action.strip():
        return f"PLAYER_ACTION: {player_action.strip()}\n{beat_summary}"
    return beat_summary


def dispatch_dice_throw(
    *,
    payload: DiceThrowPayload,
    rolling_player_id: str,
    character_name: str,
    character_stats: dict[str, int],
    encounter: StructuredEncounter | None,
    pack: GenrePack,
    genre_slug: str,
    session_id: str,
    round_number: int,
    room_broadcast: Callable[[object], None] | None,
    snapshot: GameSnapshot,
    connected_player_ids: list[str] | None = None,
    per_recipient_emit: Callable[[str, object], None] | None = None,
) -> DiceThrowOutcome:
    """Apply a beat, resolve dice, broadcast wire messages, return outcome.

    Raises ``DiceDispatchError`` when the throw can't be resolved — no
    partial state mutation leaks because beat apply only runs after stat
    validation succeeds.

    ``room_broadcast`` is the room's broadcast(msg) callable. When None
    (no room bound — e.g., legacy single-socket test paths), the dice
    messages are still built and returned on the outcome but not fanned
    out. Callers that want single-socket delivery can read them off the
    outcome.

    ``genre_slug`` is forwarded to ``build_confrontation_payload`` for
    the mid-turn CONFRONTATION frame (story 45-3); it must match the
    active genre pack's slug — there is no fallback resolution.

    Story 49-7: ``connected_player_ids`` and ``per_recipient_emit`` (a
    targeted ``(player_id, msg) -> None`` callable) drive the per-PC
    CONFRONTATION overlay. After the canonical ``room_broadcast`` of the
    mid-turn CONFRONTATION (which delivers the full-union payload to
    every socket — matching pre-49-7 behavior), the dispatcher fans a
    class-filtered CONFRONTATION to each connected player so the
    Confrontation tab paints with only their class's legal beats.
    Both params optional: when either is None the per-PC overlay is
    skipped (legacy single-socket fixtures and tests that don't bind
    a multi-player room).
    """
    if payload.beat_id is None:
        raise DiceDispatchError(
            "DICE_THROW missing beat_id — server-initiated dice flow is not "
            "supported in the Python port yet (UI drives all rolls via "
            "beat selection)"
        )

    if encounter is None or encounter.resolved:
        raise DiceDispatchError("DICE_THROW with beat_id requires an active encounter")

    cdef: ConfrontationDef | None = find_confrontation_def(
        pack.rules.confrontations if pack.rules else [],
        encounter.encounter_type,
    )
    if cdef is None:
        raise DiceDispatchError(
            f"no ConfrontationDef for encounter_type {encounter.encounter_type!r} "
            f"(pack data bug — CLAUDE.md 'no silent fallback')"
        )

    beat = next((b for b in cdef.beats if b.id == payload.beat_id), None)
    if beat is None:
        available = ",".join(b.id for b in cdef.beats)
        raise DiceDispatchError(
            f"unknown beat_id {payload.beat_id!r} for encounter "
            f"{encounter.encounter_type!r} — available: [{available}]"
        )

    # Canonicalize the stat BEFORE applying the beat so a malformed
    # stat_check doesn't leave the encounter half-applied with no dice
    # gate ever opening. Matches Rust review-cycle-2 C1.
    try:
        stat = Stat(beat.stat_check)
    except ValueError as exc:
        raise DiceDispatchError(
            f"invalid stat_check {beat.stat_check!r} on beat {payload.beat_id!r}: {exc}"
        ) from exc

    modifier = _stat_modifier(character_stats, beat.stat_check)
    difficulty = _compute_dc(beat)

    request = _build_request_payload(
        request_id=payload.request_id,
        rolling_player_id=rolling_player_id,
        character_name=character_name,
        stat=stat,
        modifier=modifier,
        difficulty=difficulty,
        context=f"{beat.label} — {beat.stat_check} check",
    )

    emit_dice_request_sent(
        request_id=request.request_id,
        rolling_player_id=request.rolling_player_id,
        stat=str(request.stat),
        difficulty=request.difficulty,
        modifier=request.modifier,
    )
    emit_dice_throw_received(
        request_id=payload.request_id,
        rolling_player_id=rolling_player_id,
        face=list(payload.face),
    )

    # Resolve dice FIRST so beat application can honor Fail/CritFail and
    # apply the correct per-tier delta override. The earlier ordering
    # (apply → resolve) used the default delta unconditionally, so a failed
    # Flank still bumped momentum +3 instead of the Fail tier's -2
    # (playtest 2026-04-24 regression).
    try:
        resolved = resolve_dice_with_faces(
            request.dice,
            list(payload.face),
            request.modifier,
            request.difficulty,
        )
    except ResolveError as exc:
        raise DiceDispatchError(f"dice resolution failed: {exc}") from exc

    actor = encounter.find_actor(character_name)
    if actor is None:
        # Fall back to first player-side actor when character_name doesn't
        # appear in the encounter's actor list (e.g. encounter built without
        # explicit actor registration).
        actor = next(
            (a for a in encounter.actors if a.side == "player"),
            None,
        )
    if actor is None:
        raise DiceDispatchError(
            f"character {character_name!r} not found in encounter actors "
            "and no player-side actor is present"
        )

    # Opposed-check fork (combat fairness, 2026-04-26).
    # When the active confrontation declares ``resolution_mode:
    # opposed_check`` we DEFER beat application: the player has rolled
    # but the engine cannot derive the outcome tier yet — that requires
    # the opponent's roll AND the opponent's beat (narrator-picked).
    # ``narration_apply`` consumes the stashed player d20 face below,
    # rolls the opponent's d20 server-side, runs ``resolve_opposed_check``
    # to derive the tier, and then calls ``apply_beat`` for both sides.
    #
    # Skipping apply_beat here is intentional and load-bearing: the
    # player tier on opposed_check encounters is NEVER the legacy
    # roll-vs-DC tier (``resolved.outcome``). Letting the legacy path
    # run would double-apply with the wrong tier and re-introduce the
    # exact unfair-combat bug this branch is fixing.
    opposed_pending = cdef.resolution_mode == ResolutionMode.opposed_check
    opposed_player_d20: int | None = None
    # ADR-114 / Task 7: damage roll payloads built in the else branch below.
    # Initialized here so the broadcast section can reference them regardless
    # of which branch executes.
    damage_request_payload: DiceRequestPayload | None = None
    damage_result_payload: DiceResultPayload | None = None

    if opposed_pending:
        # Pull the raw d20 face for the resolver. The dice pool is
        # validated to be a single d20 group earlier; ``payload.face``
        # carries one face per die in pool order. We assert the
        # invariant explicitly so any future pool change surfaces here.
        if not payload.face:
            raise DiceDispatchError(
                "opposed_check dispatch missing dice face values — "
                "DICE_THROW payload must carry the player's d20 result"
            )
        opposed_player_d20 = int(payload.face[0])
        if not (1 <= opposed_player_d20 <= 20):
            raise DiceDispatchError(
                f"opposed_check: player d20 face {opposed_player_d20} not in 1..20"
            )
        # No beat application here; ``apply_result`` is None-equivalent.
        own_delta = 0
        encounter_resolved = False
    else:
        # ADR-114 / Task 7: damage roll for strike channel beats.
        # Resolve the weapon DamageSpec BEFORE apply_beat so the resolver
        # lambda captures a concrete total. The damage roll is server-side
        # (no UI physics round-trip); we generate random faces, resolve the
        # total, and broadcast a DICE_REQUEST + DICE_RESULT after the check
        # broadcast so Sebastien sees the weapon dice animate in the overlay.
        damage_resolver_fn = None

        damage_channel = str(getattr(beat, "damage_channel", "none") or "none")
        if damage_channel == "strike" and resolved.outcome not in (
            RollOutcome.Fail,
            RollOutcome.CritFail,
        ):
            actor_core = snapshot.find_creature_core(character_name)
            damage_spec = _resolve_damage_spec_from_beat_and_actor(
                beat=beat,
                actor_core=actor_core,
                pack=pack,
            )
            if damage_spec is None:
                logger.warning(
                    "dice.damage_spec_missing beat=%r actor=%r encounter=%r — "
                    "strike beat has no resolvable weapon, damage_override, or "
                    "unarmed default; HP damage skipped (CLAUDE.md no-fabricate)",
                    payload.beat_id,
                    character_name,
                    encounter.encounter_type,
                )
                _watcher_publish(
                    "state_transition",
                    {
                        "field": "encounter",
                        "op": "damage_spec_missing",
                        "beat_id": payload.beat_id,
                        "actor": character_name,
                        "rationale": (
                            "strike channel beat has no damage_override, no "
                            "weapon with a damage spec in inventory, and no "
                            "unarmed_damage default on the genre rules — "
                            "HP path skipped"
                        ),
                    },
                    component="encounter",
                    severity="warning",
                )
            else:
                dmg_request_id = str(uuid.uuid4())
                damage_request_payload = damage_request_from_spec(
                    damage_spec,
                    request_id=dmg_request_id,
                    rolling_player_id=rolling_player_id,
                    character_name=character_name,
                )
                dmg_faces = _generate_server_faces(damage_request_payload.dice)
                dmg_resolved = resolve_dice_with_faces(
                    damage_request_payload.dice,
                    dmg_faces,
                    damage_request_payload.modifier,
                    damage_request_payload.difficulty,
                )
                dmg_total = dmg_resolved.total
                dmg_seed = generate_dice_seed(session_id, round_number + 1)
                damage_result_payload = _compose_result_payload(
                    request=damage_request_payload,
                    rolls=dmg_resolved.rolls,
                    total=dmg_total,
                    outcome=RollOutcome.Success,  # damage rolls have no outcome tier
                    seed=dmg_seed,
                    throw_params=_DAMAGE_THROW_PARAMS,
                )
                _watcher_publish(
                    "state_transition",
                    {
                        "field": "encounter",
                        "op": "damage_roll_resolved",
                        "beat_id": payload.beat_id,
                        "actor": character_name,
                        "damage_spec": damage_spec.dice,
                        "bonus": damage_spec.bonus,
                        "faces": dmg_faces,
                        "total": dmg_total,
                        "source": "dice_throw_server_roll",
                    },
                    component="encounter",
                )
                damage_resolver_fn = lambda: dmg_total  # noqa: E731

        apply_result = apply_beat(
            encounter,
            actor,
            beat,
            resolved.outcome,
            turn=round_number,
            edge_resolver=snapshot.find_creature_core,
            damage_resolver=damage_resolver_fn,
        )

        if apply_result.skipped_reason:
            raise DiceDispatchError(
                f"beat {payload.beat_id!r} skipped: {apply_result.skipped_reason}"
            )

        own_delta = apply_result.deltas.own if apply_result.deltas else 0

        with encounter_beat_applied_span(
            encounter_type=encounter.encounter_type,
            actor=character_name,
            beat_id=payload.beat_id,
            metric_delta=own_delta,
        ):
            pass
        _watcher_publish(
            "state_transition",
            {
                "field": "encounter",
                "op": "beat_applied",
                "actor": character_name,
                "actor_side": actor.side,
                "beat_id": payload.beat_id,
                "beat_kind": str(beat.kind.value)
                if hasattr(beat.kind, "value")
                else str(beat.kind),
                "outcome_tier": resolved.outcome.value
                if hasattr(resolved.outcome, "value")
                else str(resolved.outcome),
                "own_delta": own_delta,
                "opponent_delta": apply_result.deltas.opponent if apply_result.deltas else 0,
                "metric_target": encounter.encounter_type,
                "source": "dice_throw",
            },
            component="encounter",
        )
        # Story 45-9: bump total_beats_fired counter + OTEL.
        snapshot.record_beat_fired(
            beat_id=payload.beat_id,
            encounter_type=encounter.encounter_type,
            turn=round_number,
            source="dice_throw",
        )

        encounter_resolved = apply_result.resolved

    with combat_tick_span(
        encounter_type=encounter.encounter_type,
        beat=encounter.beat,
        phase=(encounter.structured_phase or EncounterPhase.Setup).value,
    ):
        pass
    if encounter_resolved:
        with encounter_resolved_span(
            encounter_type=encounter.encounter_type,
            outcome=encounter.outcome or "",
            source="dice_throw_beat",
        ):
            pass
        _watcher_publish(
            "state_transition",
            {
                "field": "encounter",
                "op": "resolved",
                "encounter_type": encounter.encounter_type,
                "outcome": encounter.outcome or "",
                "source": "dice_throw_beat",
                "final_player_metric": encounter.player_metric.current,
                "final_opponent_metric": encounter.opponent_metric.current,
            },
            component="encounter",
        )

    # Seed drives spectator replay animation only — face values are already
    # authoritative from the rolling player's Rapier settle.
    seed = generate_dice_seed(session_id, round_number)
    result = _compose_result_payload(
        request=request,
        rolls=resolved.rolls,
        total=resolved.total,
        outcome=resolved.outcome,
        seed=seed,
        throw_params=payload.throw_params,
    )

    emit_dice_result_broadcast(
        request_id=result.request_id,
        rolling_player_id=result.rolling_player_id,
        total=result.total,
        outcome=result.outcome.value,
        seed=result.seed,
    )

    # Broadcast the dice pair (DICE_REQUEST → DICE_RESULT) first so
    # spectators' overlays open before the narration kicks off. The
    # server-side DiceRequest echoes the rolling player's local build
    # (same request_id); the UI is idempotent on request_id so the
    # rolling player's overlay doesn't double-open. Story 45-3 then
    # follows the pair with a third broadcast on the non-opposed
    # branch — a CONFRONTATION carrying post-apply momentum so the UI
    # dial advances as the dice settle, not after the narrator returns.
    # Opposed-pending defers metric mutation to ``narration_apply``, so
    # the third broadcast is gated on ``not opposed_pending`` and the
    # post-narration emit at session_handler handles the eventual
    # metric advance for that branch.
    if room_broadcast is not None:
        req_msg = DiceRequestMessage(payload=request, player_id="server")
        res_msg = DiceResultMessage(payload=result, player_id="server")
        room_broadcast(req_msg)
        room_broadcast(res_msg)

        # ADR-114 / Task 7: broadcast damage roll overlay immediately after
        # the check-roll result so the player sees weapon dice animate before
        # the CONFRONTATION dial update and the narrator response arrive.
        # Damage broadcast is gated on a resolved damage_result_payload (only
        # set when the beat has damage_channel=strike AND a DamageSpec was
        # found). Skipped on the opposed-pending branch — damage is deferred
        # alongside beat application on that path.
        if not opposed_pending and damage_result_payload is not None and damage_request_payload is not None:
            room_broadcast(DiceRequestMessage(payload=damage_request_payload, player_id="server"))
            room_broadcast(DiceResultMessage(payload=damage_result_payload, player_id="server"))

        # Story 45-3: Mid-turn CONFRONTATION emit. The metric mutation
        # already landed via apply_beat above; without this broadcast the
        # UI dial sits on the prior turn's CONFRONTATION snapshot through
        # the entire dice + narration cycle (5–15s). Sebastien's lie-
        # detector flag from playtest 2026-04-19. Skipped on the opposed
        # branch where deltas are deferred to narration_apply.
        if not opposed_pending:
            # Canonical (full-union) mid-turn payload — delivered via
            # ``room_broadcast`` so legacy stub-room test fixtures and
            # any non-MP fallback path see a CONFRONTATION carrying
            # post-apply momentum. ``recipient_pc=None`` is explicit:
            # this call deliberately does not project per-PC — the
            # overlay loop below overwrites each connected player's
            # Confrontation tab with class-filtered beats (Story 49-7).
            mid_turn_payload = build_confrontation_payload(
                encounter=encounter,
                cdef=cdef,
                genre_slug=genre_slug,
                recipient_pc=None,
            )
            with encounter_momentum_broadcast_span(
                encounter_type=encounter.encounter_type,
                player_metric_after=encounter.player_metric.current,
                opponent_metric_after=encounter.opponent_metric.current,
                source="dice_throw",
                beat_id=payload.beat_id,
            ):
                room_broadcast(
                    ConfrontationMessage(
                        payload=ConfrontationPayload(**mid_turn_payload),
                        player_id="server",
                    ),
                )

                # Story 49-7: per-PC beat projection overlay on the mid-
                # turn CONFRONTATION. The canonical room_broadcast above
                # delivers the full-union payload to every socket (matches
                # pre-49-7 behavior and what legacy single-PC tests
                # exercise). Following it, fan a class-filtered
                # CONFRONTATION to each connected player so the
                # Confrontation tab paints with only their class's legal
                # beats. UI renders whichever arrives last for a given
                # encounter — filtered wins. Skipped when the caller did
                # not bind both connected_player_ids and
                # per_recipient_emit (legacy non-MP fixtures).
                if connected_player_ids is not None and per_recipient_emit is not None:
                    from sidequest.server.dispatch.confrontation import (
                        resolve_recipient_pc,
                    )

                    for pid in connected_player_ids:
                        recipient_pc, recipient_actor = resolve_recipient_pc(
                            snapshot=snapshot,
                            genre_pack=pack,
                            player_id=pid,
                        )
                        if recipient_pc is None:
                            continue
                        per_pc_payload = build_confrontation_payload(
                            encounter=encounter,
                            cdef=cdef,
                            genre_slug=genre_slug,
                            recipient_pc=recipient_pc,
                            recipient_actor_name=recipient_actor,
                        )
                        per_recipient_emit(
                            pid,
                            ConfrontationMessage(
                                payload=ConfrontationPayload(**per_pc_payload),
                                player_id="server",
                            ),
                        )

    replay_text = _format_replay_action(
        beat_label=beat.label,
        stat_check=beat.stat_check,
        actor_side=actor.side,
        player_metric_after=encounter.player_metric.current,
        opponent_metric_after=encounter.opponent_metric.current,
        total=resolved.total,
        outcome=resolved.outcome,
        player_action=payload.player_action,
    )

    logger.info(
        "dice.throw_resolved request_id=%s rolling_player=%s total=%d outcome=%s "
        "beat_id=%s player_momentum=%d opponent_momentum=%d resolved_encounter=%s",
        request.request_id,
        rolling_player_id,
        resolved.total,
        resolved.outcome.value,
        payload.beat_id,
        encounter.player_metric.current,
        encounter.opponent_metric.current,
        encounter_resolved,
    )

    if opposed_pending:
        # GM-panel visibility for the deferral. Without this watcher
        # event the deferred-beat window is invisible — between
        # DICE_THROW and the narrator's beat_selections the encounter
        # state looks frozen, and a hung narrator would silently leave
        # the player's roll unconsumed.
        _watcher_publish(
            "state_transition",
            {
                "field": "encounter",
                "op": "opposed_check_pending",
                "encounter_type": encounter.encounter_type,
                "player_actor": character_name,
                "player_beat_id": payload.beat_id,
                "player_d20": opposed_player_d20,
                "source": "dice_throw",
            },
            component="encounter",
        )

    return DiceThrowOutcome(
        request=request,
        result=result,
        replay_action_text=replay_text,
        outcome=resolved.outcome,
        encounter_resolved=encounter_resolved,
        opposed_pending=opposed_pending,
        opposed_player_d20=opposed_player_d20 if opposed_pending else None,
        opposed_player_beat_id=payload.beat_id if opposed_pending else None,
    )


# Intentional re-export: callers commonly need uuid to synthesize request_ids
# when driving the dispatcher from tests / fixtures.
def new_request_id() -> str:
    """Return a fresh UUID4 string for a DiceRequest correlation id."""
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Damage-roll helpers (ADR-114 / Task 7)
# ---------------------------------------------------------------------------

_DAMAGE_STAT = Stat("DAMAGE")
_DAMAGE_THROW_PARAMS = ThrowParams(
    velocity=(0.0, 4.0, -1.0),
    angular=(0.5, 0.5, 0.5),
    position=(0.5, 0.5),
)


def damage_request_from_spec(
    spec: DamageSpec,
    *,
    request_id: str,
    rolling_player_id: str = "server",
    character_name: str = "server",
) -> DiceRequestPayload:
    """Build a ``DiceRequestPayload`` for a weapon damage roll.

    Parses ``spec.dice`` (NdM notation) into N individual ``DieSpec`` entries
    (one per die, each with count=1) so the overlay can animate each die
    independently. ``spec.bonus`` becomes ``modifier``.

    ``stat``, ``difficulty``, and ``context`` are set to damage-roll sentinels
    — the overlay renders them but the engine doesn't interpret them for
    outcome resolution (damage rolls have no DC; the check roll already
    determined hit/miss).

    ``rolling_player_id`` defaults to ``"server"`` for server-originated rolls;
    override when a specific player should animate the throw.

    Raises ``ValueError`` if the dice string is malformed or uses an
    unsupported face count (validated at ``DamageSpec`` construction, so this
    is a belt-and-suspenders guard).
    """
    m = _DICE_RE.match(spec.dice.strip())
    if not m:
        raise ValueError(
            f"damage_request_from_spec: malformed dice string {spec.dice!r} "
            f"(should have been caught at DamageSpec validation)"
        )
    count = int(m["count"])
    faces = int(m["faces"])
    sides = DieSides.from_wire(faces)
    if sides is DieSides.Unknown:
        raise ValueError(
            f"damage_request_from_spec: unsupported die face count d{faces} "
            f"in {spec.dice!r}"
        )
    # One DieSpec per die so the overlay renders individual dice.
    dice_pool = [DieSpec(sides=sides, count=1) for _ in range(count)]
    return DiceRequestPayload(
        request_id=request_id,
        rolling_player_id=rolling_player_id,
        character_name=character_name,
        dice=dice_pool,
        modifier=spec.bonus,
        stat=_DAMAGE_STAT,
        difficulty=1,  # damage rolls have no DC
        context="weapon damage",
    )


def _generate_server_faces(dice: list[DieSpec]) -> list[int]:
    """Roll server-side faces for a damage dice pool.

    Each die in the pool gets a random face in ``1..=sides``. The faces are
    used to both: (a) build the authoritative ``DiceResultPayload`` for the
    broadcast, and (b) feed ``resolve_dice_with_faces`` to compute the total.

    Uses ``random.randint`` — no physics, no seed. The seed in the result
    payload drives spectator replay animation; here it's derived from the
    session and round like the check-roll path.
    """
    faces: list[int] = []
    for spec in dice:
        sides = spec.sides.faces()
        assert sides is not None, f"Unknown die in damage pool: {spec.sides!r}"
        for _ in range(spec.count):
            faces.append(random.randint(1, sides))
    return faces


def _resolve_damage_spec_from_beat_and_actor(
    *,
    beat: BeatDef,
    actor_core: object | None,
    pack: GenrePack,
) -> DamageSpec | None:
    """Resolve the weapon DamageSpec for a strike beat.

    Resolution priority (CLAUDE.md no-silent-fallback — skip loudly, never fabricate):
    1. ``beat.damage_override`` — explicit spec on the beat (natural attack / creature).
    2. Actor's equipped weapon item dict carrying a ``damage`` dict (from inventory).
    3. Pack catalog lookup: find the actor's first equipped weapon item by id,
       then read ``CatalogItem.damage`` from the pack's item catalog.
    4. No match — returns None; caller must log and skip.

    ``actor_core`` is the actor's ``CreatureCore`` (may be None for actors without
    a resolved core). ``pack`` is the live genre pack (provides the item catalog).
    """
    # Priority 1: beat-level override (natural attack, creature).
    if beat.damage_override is not None:
        return beat.damage_override

    # Priority 2 & 3: actor's inventory.
    if actor_core is None:
        return None

    inventory_items: list[dict] = getattr(
        getattr(actor_core, "inventory", None), "items", []
    )
    if not inventory_items:
        return None

    # Priority 2: item dict already carries a serialised damage field.
    # (This path fires for materialised NPCs whose item dicts were built
    # with a ``damage`` key.)
    for item_dict in inventory_items:
        dmg_raw = item_dict.get("damage")
        if dmg_raw is not None:
            if isinstance(dmg_raw, dict):
                try:
                    return DamageSpec.model_validate(dmg_raw)
                except Exception:
                    logger.warning(
                        "damage_spec: item %r has unparseable damage dict %r — skipping",
                        item_dict.get("id"),
                        dmg_raw,
                    )
            elif isinstance(dmg_raw, str):
                try:
                    return DamageSpec.model_validate({"dice": dmg_raw})
                except Exception:
                    logger.warning(
                        "damage_spec: item %r has unparseable damage string %r — skipping",
                        item_dict.get("id"),
                        dmg_raw,
                    )

    # Priority 3: pack catalog lookup by item id.
    catalog = None
    if pack is not None:
        inv_config = getattr(pack, "inventory", None)
        if inv_config is not None:
            catalog = getattr(inv_config, "item_catalog", None)

    if catalog:
        catalog_by_id = {c.id: c for c in catalog}
        for item_dict in inventory_items:
            item_id = item_dict.get("id")
            if not item_id:
                continue
            catalog_item = catalog_by_id.get(item_id)
            if catalog_item is not None and catalog_item.damage is not None:
                return catalog_item.damage

    return None
