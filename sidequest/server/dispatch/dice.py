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

from sidequest.game.beat_kinds import _opposite_side_first_actor
from sidequest.game.dice import ResolveError, generate_dice_seed, resolve_dice_with_faces
from sidequest.game.encounter import EncounterPhase, StructuredEncounter
from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.session import GameSnapshot
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.rules import ConfrontationDef, ResolutionMode
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
)
from sidequest.server.dispatch.damage_roll import _DAMAGE_THROW_PARAMS, damage_request_from_spec
from sidequest.server.dispatch.damage_roll import (
    generate_server_faces as _generate_server_faces,
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

    ruleset = get_ruleset_module(pack.rules.ruleset)

    cdef: ConfrontationDef | None = ruleset.find_confrontation(
        pack.rules.confrontations,
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

    # Generalized attack setup: the module computes modifier + target number with the target
    # in hand, so SWN reads target AC. native ignores the cores and reproduces stat_mod vs DC.
    target_core = None
    if encounter is not None:
        target_name = _opposite_side_first_actor(encounter, "player")
        if target_name is not None:
            target_core = snapshot.find_creature_core(target_name)
    attacker_core = snapshot.find_creature_core(character_name)
    attack = ruleset.attack_params(
        beat=beat, attacker_stats=character_stats,
        attacker_core=attacker_core, target_core=target_core,
    )
    modifier = attack.modifier
    difficulty = attack.target_number

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
            damage_spec = ruleset.resolve_damage(
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
                # CWN Trauma seam (spec 2026-05-28): multiply rolled damage on a
                # Traumatic Hit, and flag the scene so a 0-HP drop this scene can
                # roll Major Injury. No-op for native/swn (base passthrough).
                _lethality = ruleset.resolve_trauma(
                    spec=damage_spec,
                    base_total=dmg_total,
                    cfg=pack.rules.ruleset_config() if pack and pack.rules else None,
                    rng=random,
                    actor=character_name,
                )
                dmg_total = _lethality.final_total
                if _lethality.traumatic:
                    from sidequest.game.encounter_tag import EncounterTag

                    if not any(t.text == "Traumatic Hit Landed" for t in encounter.tags):
                        encounter.tags.append(
                            EncounterTag(
                                text="Traumatic Hit Landed",
                                created_by=character_name,
                                target=None,
                                leverage=0,
                                fleeting=False,
                                created_turn=round_number,
                            )
                        )
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

        apply_result = ruleset.apply_beat(
            encounter=encounter,
            actor=actor,
            beat=beat,
            outcome=resolved.outcome,
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
        if (
            not opposed_pending
            and damage_result_payload is not None
            and damage_request_payload is not None
        ):
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
                core_resolver=snapshot.find_creature_core,
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
                            core_resolver=snapshot.find_creature_core,
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
# Damage-roll helpers (ADR-114 / Task 7 → Task 11)
# ---------------------------------------------------------------------------
# The three helpers were extracted to ``sidequest.server.dispatch.damage_roll``
# (Task 11) so that ``narration_apply._resolve_opposed_check_branch`` can also
# use them without copy-paste. They are re-imported at module top for use here.
#
# ``damage_request_from_spec`` (public) is re-exported from the import block.
# ``_generate_server_faces`` is re-aliased as a private name (the import block
# already does this) and used in the strike-damage branch above.
#
# Damage resolution itself now routes through ``ruleset.resolve_damage()`` (the
# bound RulesetModule), so ``resolve_damage_spec_from_beat_and_actor`` is no
# longer imported here directly.
#
# ``_DAMAGE_THROW_PARAMS`` is also imported from ``damage_roll`` and used in
# the broadcast composition below.
