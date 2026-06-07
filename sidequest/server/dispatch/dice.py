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

from sidequest.game.beat_kinds import _opposite_side_first_actor, apply_beat_hp_channel
from sidequest.game.dice import ResolveError, generate_dice_seed, resolve_dice_with_faces
from sidequest.game.encounter import EncounterPhase, StructuredEncounter
from sidequest.game.hp_depletion import check_hp_depletion
from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.ruleset.base import RulesetModule
from sidequest.game.ruleset.wwn import WwnRulesetModule
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
from sidequest.server.ability_invocation_telemetry import (
    emit_ability_invocation_unrouted,
)
from sidequest.server.dispatch.confrontation import (
    build_confrontation_payload,
    make_confrontation_frame_supplier,
    make_confrontation_portrait_resolver,
)
from sidequest.server.dispatch.damage_roll import _DAMAGE_THROW_PARAMS, damage_request_from_spec
from sidequest.server.dispatch.damage_roll import (
    generate_server_faces as _generate_server_faces,
)
from sidequest.server.dispatch.downed_seam import (
    DiceDispatchError,
    run_cwn_wwn_downed_seam,
)
from sidequest.server.dispatch.downed_seam import (
    physical_save_target_for as _physical_save_target_for,
)
from sidequest.telemetry.spans import (
    combat_tick_span,
    emit_dice_request_sent,
    emit_dice_result_broadcast,
    emit_dice_throw_received,
    encounter_beat_applied_span,
    encounter_momentum_broadcast_span,
    encounter_opponent_attack_resolved_span,
    encounter_resolved_span,
)
from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

logger = logging.getLogger(__name__)

_DICE_RE = re.compile(r"^(?P<count>\d+)d(?P<faces>\d+)$")

# ``DiceDispatchError``, ``_physical_save_target_for`` and ``run_cwn_wwn_downed_seam``
# are re-exported from ``downed_seam`` (the shared CWN/WWN seam, also used by the WWN
# cast path) to preserve their historical ``sidequest.server.dispatch.dice`` import
# path. Listing them in ``__all__`` keeps the re-exports from being flagged unused.
__all__ = [
    "DiceDispatchError",
    "_physical_save_target_for",
    "run_cwn_wwn_downed_seam",
]


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


def _build_check_request_payload(
    *,
    request_id: str,
    rolling_player_id: str,
    character_name: str,
    stat: Stat,
    modifier: int,
    difficulty: int,
    context: str,
) -> DiceRequestPayload:
    """Shape a 2d6 skill-check DiceRequest (CWN net_run hacking path).

    Same envelope as _build_request_payload but a 2d6 pool instead of a single
    d20 — the CWN cyberspace Program check (Tech + Program vs the security DC).
    The physics overlay throws whatever pool the request names (ADR-074/075)."""
    return DiceRequestPayload(
        request_id=request_id,
        rolling_player_id=rolling_player_id,
        character_name=character_name,
        dice=[DieSpec(sides=DieSides.D6, count=2)],
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
    emit_confrontation: Callable[[object, Callable[[str], object]], None] | None = None,
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

    Story 59-20: ``emit_confrontation`` is the handler's single-supplier
    emit callable ``(union_payload, per_recipient_supplier) -> None`` that
    routes the mid-turn CONFRONTATION through ``emit_event`` — the union is
    persisted to the EventLog only and each connected socket receives one
    class-filtered frame. When None (bare-dispatch callers with only
    ``room_broadcast`` wired — e2e/legacy fixtures), the dispatcher falls back
    to a single union ``room_broadcast``. This replaces the Story 49-7
    union-broadcast + per-PC overlay race.
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

    # Ability-invocation decline evidence (sq-playtest 2026-06-07 Reroute
    # Power): in-confrontation actions ride ``payload.player_action`` straight
    # into a router-SUPPRESSED replay turn (story 91-2) — the intent-router
    # pass never sees them, so the scan must happen HERE, at beat commit, on
    # the raw typed text. Shared emit seam with the router pass (one span name,
    # one GM-panel query).
    if payload.player_action and payload.player_action.strip():
        emit_ability_invocation_unrouted(action=payload.player_action, snapshot=snapshot)

    # Canonicalize the stat BEFORE applying the beat so a malformed
    # stat_check doesn't leave the encounter half-applied with no dice
    # gate ever opening. Matches Rust review-cycle-2 C1.
    try:
        stat = Stat(beat.stat_check)
    except ValueError as exc:
        raise DiceDispatchError(
            f"invalid stat_check {beat.stat_check!r} on beat {payload.beat_id!r}: {exc}"
        ) from exc

    is_net_run = bool(
        pack and pack.rules and pack.rules.ruleset == "cwn" and cdef.category == "hacking"
    )

    net_run_base_dc = 0
    net_run_alert_modifier = 0
    if is_net_run:
        # CWN net_run (spec 2026-05-29): resolve as 2d6 + INT mod + Program
        # skill vs the security DC, NOT d20 vs AC. The dice lib resolves any
        # pool; this selects the 2d6 shape — it does not build a new resolver.
        from sidequest.genre.models.rules import CwnConfig

        cfg = pack.rules.ruleset_config()
        if (
            not isinstance(cfg, CwnConfig)
            or cfg.hacking is None
            or encounter.security_tier is None
            or encounter.security_tier not in cfg.hacking.security_tiers
        ):
            raise DiceDispatchError(
                "net_run dispatch reached without a resolvable security tier "
                f"(tier={getattr(encounter, 'security_tier', None)!r}); the "
                "lifecycle seam should have stamped it (No Silent Fallbacks)"
            )
        net_run_base_dc = cfg.hacking.security_tiers[encounter.security_tier]
        # Alert escalation: each point of network alert (the opponent dial)
        # raises the effective DC by 1 (CWN situational modifier).
        net_run_alert_modifier = int(encounter.opponent_metric.current)
        int_mod = ruleset.stat_modifier(character_stats, beat.stat_check)
        program_skill = int(beat.combat_skill)
        modifier = int_mod + program_skill
        difficulty = net_run_base_dc + net_run_alert_modifier
        request = _build_check_request_payload(
            request_id=payload.request_id,
            rolling_player_id=rolling_player_id,
            character_name=character_name,
            stat=stat,
            modifier=modifier,
            difficulty=difficulty,
            context=f"Program check vs {encounter.security_tier} — DC {difficulty}",
        )
    else:
        # Generalized attack setup: the module computes modifier + target number with the target
        # in hand, so SWN reads target AC. native ignores the cores and reproduces stat_mod vs DC.
        target_core = None
        if encounter is not None:
            target_name = _opposite_side_first_actor(encounter, "player")
            if target_name is not None:
                target_core = snapshot.find_creature_core(target_name)
        attacker_core = snapshot.find_creature_core(character_name)
        attack = ruleset.attack_params(
            beat=beat,
            attacker_stats=character_stats,
            attacker_core=attacker_core,
            target_core=target_core,
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

    if is_net_run:
        # Lie-detector: record the resolved Program check. resolve_hacking
        # recomputes the same effective DC the request used and emits
        # cwn.hacking.security_check with the resolved tier. Fired here (after
        # resolve, before apply_beat) so the span carries the real outcome.
        ruleset.resolve_hacking(
            verb=beat.label,
            tier=encounter.security_tier or "",
            base_dc=net_run_base_dc,
            alert_modifier=net_run_alert_modifier,
            outcome=resolved.outcome.value,
            actor=character_name,
        )

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
    # Story 71-21: opponent reprisal dice (to-hit + damage), built when the
    # seated opponent takes its server-driven attack turn. Broadcast AFTER the
    # player's own dice pair so the overlay shows the player's roll, then the
    # enemy's answer. Empty when no reprisal fires (opposed/dial/native paths).
    opponent_reprisal_messages: list[object] = []

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

        # WWN Killing Blow warrior-detection (SRD §1.5.18, Plan 3 Task 9).
        # Resolved once here; reused in both the HIT and Shock (MISS) seams.
        # Gate order: wwn ruleset first, then Warrior-archetype class flag.
        # Non-wwn packs and non-Warrior classes are guaranteed byte-for-byte
        # inert — no span, no bonus, no mutation.
        _is_wwn_warrior = False
        if pack and pack.rules and pack.rules.ruleset == "wwn":
            _acting_char = next(
                (c for c in snapshot.characters if c.core.name == character_name), None
            )
            _class_def = (
                next(
                    (cls for cls in pack.classes if cls.display_name == _acting_char.char_class),
                    None,
                )
                if _acting_char is not None
                else None
            )
            _is_wwn_warrior = bool(_class_def is not None and _class_def.warrior)

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
                # WWN Warrior Killing Blow rider — HIT path (SRD §1.5.18,
                # Plan 3 Task 9). Adds ceil(level / divisor) to strike damage.
                # Gate: wwn pack AND Warrior-archetype actor; inert otherwise.
                if _is_wwn_warrior and actor_core is not None:
                    assert isinstance(ruleset, WwnRulesetModule)
                    cfg = pack.rules.ruleset_config()
                    dmg_total = ruleset.apply_killing_blow(
                        base_total=dmg_total,
                        level=int(actor_core.level),
                        cfg=cfg,
                        actor=character_name,
                    )
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

        # CWN Shock seam (spec 2026-05-28, Task 10): a melee weapon with a
        # Shock rating chips fixed damage on a MISS vs a low-Melee-AC target.
        # Sibling of the HIT damage block above — fires ONLY on Fail/CritFail,
        # so it never double-applies with the rolled-damage path. No-op for
        # native/swn (base resolve_shock returns 0, so chip == 0).
        if damage_channel == "strike" and resolved.outcome in (
            RollOutcome.Fail,
            RollOutcome.CritFail,
        ):
            actor_core = snapshot.find_creature_core(character_name)
            shock_spec = ruleset.resolve_damage(
                beat=beat,
                actor_core=actor_core,
                pack=pack,
            )
            shock_target_name = _opposite_side_first_actor(encounter, actor.side)
            shock_target_core = (
                snapshot.find_creature_core(shock_target_name)
                if shock_target_name is not None
                else None
            )
            if shock_spec is not None and shock_target_core is not None:
                chip = ruleset.resolve_shock(
                    spec=shock_spec,
                    target_melee_ac=int(getattr(shock_target_core, "armor_class", 10)),
                    actor=character_name,
                )
                # WWN Warrior Killing Blow rider — Shock path (SRD §1.5.18,
                # Plan 3 Task 9). Killing Blow adds to Shock too (same gate).
                # Only applies when chip > 0 (a genuine Shock hit).
                if chip > 0 and _is_wwn_warrior:
                    assert isinstance(ruleset, WwnRulesetModule)
                    _kb_cfg = pack.rules.ruleset_config()
                    _shock_actor_core = snapshot.find_creature_core(character_name)
                    chip = ruleset.apply_killing_blow(
                        base_total=chip,
                        level=int(_shock_actor_core.level) if _shock_actor_core else 1,
                        cfg=_kb_cfg,
                        actor=character_name,
                    )
                if chip > 0:
                    apply_beat_hp_channel(
                        target=shock_target_core,
                        channel="strike",
                        damage_total=chip,
                        target_mitigation=0,
                        source_beat_id=f"{payload.beat_id}:shock",
                    )

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

        # CWN/WWN downed seam (spec 2026-05-28, Task 11): if this strike dropped
        # a target to 0 HP, resolve the Mortal Injury (always) and Major Injury
        # (only when a Traumatic Hit landed this scene). Shared with the WWN cast
        # path (Plan 3 Task 7) via ``run_cwn_wwn_downed_seam`` so the two cannot
        # drift — the helper carries the cwn/wwn gate + the 0-HP check internally.
        run_cwn_wwn_downed_seam(
            ruleset=ruleset,
            snapshot=snapshot,
            encounter=encounter,
            cdef=cdef,
            pack=pack,
            actor_side=actor.side,
            rng=random,
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

        # --- Opponent reprisal: server-driven enemy attack turn (story 71-21) ---
        # SWN hp_depletion combat had no enemy turn — the player could attack but
        # the opponent never answered mechanically, so the player could never
        # lose a firefight (perseus_cloud playtest). After the player's beat
        # resolves — and unless it already ended the fight — the seated opponent
        # attacks back: d20 + mods vs the player's AC, server-rolled damage to the
        # player's HP, and hp_depletion resolution so the player can be downed.
        # Gated on hp_depletion combat (cdef is authoritative — the encounter may
        # not carry win_condition in every fixture) AND a ruleset that implements
        # the enemy turn. The capability check keys on the OVERRIDE, not a
        # ``== "swn"`` string, so wwn/cwn (which subclass SWN) inherit it and
        # native/dial rulesets (which resolve the opponent via the opposed-check
        # branch and leave the base NotImplementedError in place) never reach it.
        module_has_opponent_turn = (
            type(ruleset).resolve_opponent_attack is not RulesetModule.resolve_opponent_attack
        )
        if (
            not encounter_resolved
            and cdef.win_condition == "hp_depletion"
            and module_has_opponent_turn
        ):
            opponent_reprisal_messages = _resolve_opponent_reprisal(
                encounter=encounter,
                cdef=cdef,
                ruleset=ruleset,
                pack=pack,
                snapshot=snapshot,
                player_name=character_name,
                session_id=session_id,
                round_number=round_number,
                rng=random,
            )
            # NOTE: we intentionally do NOT fold the reprisal's resolution into
            # ``encounter_resolved`` here. The reprisal's own ``check_hp_depletion``
            # already emits the ``encounter.resolved`` span with
            # ``source="hp_depletion"`` when it downs the player; the outer block
            # below is keyed on the PLAYER-beat resolution (``source=
            # "dice_throw_beat"``) and must not double-emit. The return value
            # reads ``encounter.resolved`` directly to report the true state.

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
        # RW-2: on the opposed-pending branch the stamped outcome is the
        # provisional flat-DC tier — apply was deferred to the opposed
        # resolver, whose ``opposed_roll_resolved`` span carries the real
        # tier. Mark the deferral so the GM panel doesn't read a final
        # CritSuccess on a beat that may lose the exchange.
        deferred_opposed=opposed_pending,
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

        # Story 71-21: the opponent's reprisal dice (to-hit, +damage on a hit).
        # Fanned out AFTER the player's pair so the overlay reads "player rolls,
        # then the enemy answers." Empty on the opposed/dial/native paths.
        for _reprisal_msg in opponent_reprisal_messages:
            room_broadcast(_reprisal_msg)

        # Story 45-3 / 59-20: Mid-turn CONFRONTATION emit. The metric mutation
        # already landed via apply_beat above; without this the UI dial sits on
        # the prior turn's CONFRONTATION snapshot through the entire dice +
        # narration cycle (5–15s). Skipped on the opposed branch where deltas
        # are deferred to narration_apply.
        if not opposed_pending:
            # The canonical full-union payload. Story 59-20: when the handler
            # provides ``emit_confrontation`` (the production path), route it
            # through the single ``emit_event(per_recipient_payload=...)``
            # supplier — the union is persisted to the EventLog ONLY and each
            # connected socket (incl. the emitter) receives one class-filtered
            # frame. This replaces the Story 49-7 union-broadcast + per-PC
            # overlay race. Bare-dispatch callers (no handler emit wired — e.g.
            # e2e fixtures with only ``room_broadcast``) fall back to a single
            # union ``room_broadcast``.
            union_payload = ConfrontationPayload(
                **build_confrontation_payload(
                    encounter=encounter,
                    cdef=cdef,
                    genre_slug=genre_slug,
                    recipient_pc=None,
                    core_resolver=snapshot.find_creature_core,
                    # Story 85-3: stakes + opponent portrait on the canonical
                    # union (persisted + delivered to bare-dispatch room_broadcast
                    # fixtures). The supplier below re-projects per recipient.
                    active_stakes=snapshot.active_stakes,
                    portrait_resolver=make_confrontation_portrait_resolver(
                        snapshot=snapshot, genre_pack=pack, genre_slug=genre_slug
                    ),
                    # Story 97-3: server-authored per-beat difficulty on the offer.
                    rules=pack.rules,
                )
            )
            with encounter_momentum_broadcast_span(
                encounter_type=encounter.encounter_type,
                player_metric_after=encounter.player_metric.current,
                opponent_metric_after=encounter.opponent_metric.current,
                source="dice_throw",
                beat_id=payload.beat_id,
            ):
                if emit_confrontation is not None:
                    supplier = make_confrontation_frame_supplier(
                        snapshot=snapshot,
                        genre_pack=pack,
                        encounter=encounter,
                        cdef=cdef,
                        genre_slug=genre_slug,
                    )
                    emit_confrontation(union_payload, supplier)
                elif room_broadcast is not None:
                    room_broadcast(
                        ConfrontationMessage(payload=union_payload, player_id="server"),
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
        "beat_id=%s player_momentum=%d opponent_momentum=%d resolved_encounter=%s "
        "deferred_opposed=%s",
        request.request_id,
        rolling_player_id,
        resolved.total,
        resolved.outcome.value,
        payload.beat_id,
        encounter.player_metric.current,
        encounter.opponent_metric.current,
        encounter_resolved,
        opposed_pending,
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
        # Report the TRUE resolution state: the player's beat may have ended the
        # fight, or the opponent reprisal (story 71-21) may have downed the
        # player. ``encounter.resolved`` is the live source of truth.
        encounter_resolved=encounter_resolved or encounter.resolved,
        opposed_pending=opposed_pending,
        opposed_player_d20=opposed_player_d20 if opposed_pending else None,
        opposed_player_beat_id=payload.beat_id if opposed_pending else None,
    )


def _resolve_opponent_reprisal(
    *,
    encounter: StructuredEncounter,
    cdef: ConfrontationDef,
    ruleset: RulesetModule,
    pack: GenrePack,
    snapshot: GameSnapshot,
    player_name: str,
    session_id: str,
    round_number: int,
    rng: random.Random,
) -> list[object]:
    """Server-driven opponent attack turn (story 71-21, SWN hp_depletion combat).

    The seated opponent attacks the acting player: roll d20, resolve to-hit vs the
    player's AC through ``ruleset.resolve_opponent_attack`` (the pre-existing,
    unit-tested primitive — wired here, not reimplemented), and on a hit roll
    server-side damage into the player's HP. ``check_hp_depletion`` then resolves
    the encounter if the player is downed (the player can finally lose).

    Mutates the player's ``CreatureCore`` HP and may resolve ``encounter``. Emits
    ``encounter.opponent_attack_resolved`` on every attempt (the GM-panel
    lie-detector). Returns the opponent's DICE_REQUEST/DICE_RESULT messages
    (to-hit, plus damage on a hit) for the caller to broadcast after the player's
    own dice pair. Returns an empty list — and logs loudly — when the reprisal
    cannot proceed (no Other seated, no opponent strike beat, missing stats), per
    No Silent Fallbacks: the absence is visible, never a silent skip.
    """
    messages: list[object] = []

    opponent_name = _opposite_side_first_actor(encounter, "player")
    if opponent_name is None:
        # ADR-116: a confrontation requires an Other. None seated → no reprisal.
        logger.warning(
            "dice.opponent_reprisal_skipped reason=no_opponent_seated encounter=%s",
            encounter.encounter_type,
        )
        return messages

    opponent_beat = next(
        (b for b in cdef.beats if str(getattr(b, "damage_channel", "none") or "none") == "strike"),
        None,
    )
    if opponent_beat is None:
        logger.warning(
            "dice.opponent_reprisal_skipped reason=no_strike_beat encounter=%s",
            encounter.encounter_type,
        )
        return messages

    opponent_stats = cdef.opponent_ability_scores()
    if not opponent_stats:
        logger.warning(
            "dice.opponent_reprisal_skipped reason=no_opponent_stats encounter=%s "
            "(opponent_default_stats has no ability scores)",
            encounter.encounter_type,
        )
        return messages

    player_core = snapshot.find_creature_core(player_name)
    if player_core is None:
        logger.warning(
            "dice.opponent_reprisal_skipped reason=no_player_core player=%s",
            player_name,
        )
        return messages
    target_ac = int(player_core.armor_class)

    d20 = rng.randint(1, 20)
    outcome = ruleset.resolve_opponent_attack(
        attacker_stats=opponent_stats,
        stat_check=opponent_beat.stat_check,
        attack_bonus=int(getattr(opponent_beat, "attack_bonus", 0) or 0),
        combat_skill=int(getattr(opponent_beat, "combat_skill", 0) or 0),
        target_ac=target_ac,
        d20=d20,
    )

    # Lie-detector: the to-hit decision, every attempt (hit or miss).
    with encounter_opponent_attack_resolved_span(
        encounter_type=encounter.encounter_type,
        attacker=opponent_name,
        target=player_name,
        d20=outcome.d20,
        modifier=outcome.modifier,
        attack_total=outcome.attack_total,
        target_ac=outcome.target_ac,
        hit=outcome.hit,
    ):
        pass
    _watcher_publish(
        "state_transition",
        {
            "field": "encounter",
            "op": "opponent_attack_resolved",
            "attacker": opponent_name,
            "target": player_name,
            "beat_id": opponent_beat.id,
            "d20": outcome.d20,
            "modifier": outcome.modifier,
            "attack_total": outcome.attack_total,
            "target_ac": outcome.target_ac,
            "hit": outcome.hit,
            "source": "opponent_reprisal",
        },
        component="encounter",
    )

    # Broadcast the opponent's to-hit roll so the table sees the enemy answer
    # animate in the dice overlay (player-facing math).
    tohit_request = _build_request_payload(
        request_id=str(uuid.uuid4()),
        rolling_player_id="opponent",
        character_name=opponent_name,
        stat=Stat(opponent_beat.stat_check),
        modifier=outcome.modifier,
        difficulty=target_ac,
        context=f"{opponent_beat.label} — enemy attack vs AC {target_ac}",
    )
    tohit_resolved = resolve_dice_with_faces(
        tohit_request.dice, [d20], tohit_request.modifier, tohit_request.difficulty
    )
    tohit_result = _compose_result_payload(
        request=tohit_request,
        rolls=tohit_resolved.rolls,
        total=tohit_resolved.total,
        outcome=tohit_resolved.outcome,
        seed=generate_dice_seed(session_id, round_number),
        throw_params=_DAMAGE_THROW_PARAMS,
    )
    messages.append(DiceRequestMessage(payload=tohit_request, player_id="server"))
    messages.append(DiceResultMessage(payload=tohit_result, player_id="server"))

    if not outcome.hit:
        return messages

    # HIT: roll the opponent's weapon damage and ablate the player's HP.
    # Prefer the confrontation's authored ``opponent_damage`` (the enemy's own
    # weapon) over beat/inventory resolution: the opponent reuses the player's
    # strike beat only for the to-hit terms, and a beat like ``shoot`` resolves
    # damage from the ACTOR's inventory — which the seeded opponent NPC lacks, so
    # beat resolution returns None and the hit deals 0 HP (playtest 67-10). This
    # never caps the player's weapon: it is read only on the opponent's turn.
    opponent_core = snapshot.find_creature_core(opponent_name)
    damage_spec = cdef.opponent_damage or ruleset.resolve_damage(
        beat=opponent_beat, actor_core=opponent_core, pack=pack
    )
    if damage_spec is None:
        # The opponent's strike beat has no resolvable damage (no damage_override,
        # no weapon, no unarmed default). The hit lands but deals no HP — surfaced
        # loudly so a mook authored without a weapon is fixed, not silently inert.
        # (TEA delivery finding: the `shoot` beat needs a weapon; `overload` carries
        # its own damage_override.)
        logger.warning(
            "dice.opponent_reprisal_damage_spec_missing opponent=%s beat=%s encounter=%s "
            "— hit landed but no damage source; player took no HP damage",
            opponent_name,
            opponent_beat.id,
            encounter.encounter_type,
        )
        _watcher_publish(
            "state_transition",
            {
                "field": "encounter",
                "op": "opponent_damage_spec_missing",
                "opponent": opponent_name,
                "beat_id": opponent_beat.id,
                "rationale": (
                    "opponent strike beat has no damage_override, no weapon with a "
                    "damage spec, and no unarmed default — HP path skipped"
                ),
            },
            component="encounter",
            severity="warning",
        )
        return messages

    dmg_request = damage_request_from_spec(
        damage_spec,
        request_id=str(uuid.uuid4()),
        rolling_player_id="opponent",
        character_name=opponent_name,
    )
    dmg_faces = _generate_server_faces(dmg_request.dice)
    dmg_resolved = resolve_dice_with_faces(
        dmg_request.dice, dmg_faces, dmg_request.modifier, dmg_request.difficulty
    )
    dmg_total = dmg_resolved.total

    # SWN damage is gated by AC (the to-hit roll), not further reduced by armor —
    # mitigation is 0, matching the player-side strike/shock channel.
    apply_beat_hp_channel(
        target=player_core,
        channel="strike",
        damage_total=dmg_total,
        target_mitigation=0,
        source_beat_id=f"{opponent_beat.id}:opponent_attack",
    )
    # Text-log forensics line (sq-playtest 2026-06-07 silent death-spiral): the
    # success path was span/watcher-only — a log grep on the dead session saw
    # NOTHING for a reprisal that ablated a PC. WARNING-on-skip already existed;
    # INFO-on-hit completes the pair.
    logger.info(
        "dice.opponent_reprisal_hit attacker=%s target=%s beat=%s damage=%s hp_after=%s/%s",
        opponent_name,
        player_name,
        opponent_beat.id,
        dmg_total,
        player_core.hp.current,
        player_core.hp.max,
    )
    # The narrator never sees server-rolled reprisal damage (the dice messages
    # go to the table, not the prompt) — without this directive the prose
    # narrates around a hit the engine already applied, and state/story diverge
    # (SOUL: mechanical state must back the story; sq-playtest 2026-06-07).
    snapshot.next_turn_directives.append(
        f"MECHANICAL TRUTH (weave into the narration): {opponent_name}'s "
        f"{opponent_beat.label} struck {player_name} for {dmg_total} damage — "
        f"{player_name} is now at {player_core.hp.current}/{player_core.hp.max} HP. "
        "Narrate the hit landing; do not soften or omit it."
    )
    _watcher_publish(
        "state_transition",
        {
            "field": "encounter",
            "op": "opponent_damage_roll_resolved",
            "opponent": opponent_name,
            "target": player_name,
            "beat_id": opponent_beat.id,
            "damage_spec": damage_spec.dice,
            "bonus": damage_spec.bonus,
            "faces": dmg_faces,
            "total": dmg_total,
            "source": "opponent_reprisal",
        },
        component="encounter",
    )

    dmg_result = _compose_result_payload(
        request=dmg_request,
        rolls=dmg_resolved.rolls,
        total=dmg_total,
        outcome=RollOutcome.Success,  # damage rolls have no outcome tier
        seed=generate_dice_seed(session_id, round_number + 1),
        throw_params=_DAMAGE_THROW_PARAMS,
    )
    messages.append(DiceRequestMessage(payload=dmg_request, player_id="server"))
    messages.append(DiceResultMessage(payload=dmg_result, player_id="server"))

    # The player may now be at 0 HP — resolve hp_depletion against them so the
    # player can actually lose the fight (emits encounter.resolved source=
    # hp_depletion).
    depletion = check_hp_depletion(
        encounter,
        snapshot.find_creature_core,
        beat_id=f"{opponent_beat.id}:opponent_attack",
    )
    if depletion is not None:
        # sq-playtest 2026-06-07 SILENT death-spiral: the reprisal resolved the
        # encounter (opponent_victory, PC downed) and NOTHING surfaced it — the
        # narrator kept the fight alive in prose, the text log was silent, and
        # the forensic timeline had no ENCOUNTER_RESOLVED row. Three closes:
        #
        # 1. Stamp pending_resolution_signal (the same factory the dial-sweep /
        #    yield paths use) so the narrator's encounter context renders the
        #    [ENCOUNTER RESOLVED] zone on this turn's narration.
        # 2. Publish the op="resolved" watcher event — _maybe_persist_encounter_row
        #    maps it to a persisted ENCOUNTER_RESOLVED row (the dial path at the
        #    dice_throw_beat close below already does this; the reprisal close
        #    was the only resolution channel that never published one).
        # 3. A directive telling the narrator the fight is OVER — the resolution
        #    zone says what happened; this says "stop narrating a live fight".
        #
        # Function-level import: narration_apply imports dispatch modules at
        # module level (same pattern as apply_post_resolution_lethality below).
        from sidequest.server.narration_apply import _build_resolution_signal

        snapshot.pending_resolution_signal = _build_resolution_signal(encounter)
        logger.info(
            "dice.opponent_reprisal_resolved_encounter encounter=%s outcome=%s "
            "down_side=%s target=%s hp=%s/%s",
            encounter.encounter_type,
            encounter.outcome,
            depletion.down_side,
            player_name,
            player_core.hp.current,
            player_core.hp.max,
        )
        _watcher_publish(
            "state_transition",
            {
                "field": "encounter",
                "op": "resolved",
                "encounter_type": encounter.encounter_type,
                "outcome": encounter.outcome or "",
                "source": "hp_depletion",
                "down_side": depletion.down_side,
                "final_player_metric": encounter.player_metric.current,
                "final_opponent_metric": encounter.opponent_metric.current,
            },
            component="encounter",
        )
        snapshot.next_turn_directives.append(
            f"MECHANICAL TRUTH (weave into the narration): the {encounter.encounter_type} "
            f"confrontation has RESOLVED — outcome: {encounter.outcome}. "
            f"{player_name} has been taken out of the fight. Narrate the close of "
            "the engagement; do NOT continue narrating it as a live, ongoing fight."
        )

    # EH-2 burning_peace playtest (2026-06-05): a PC just dropped to 0 by the
    # reprisal must take the genre lethality policy's mechanical consequence —
    # otherwise the PC is parked at 0/10 with full agency, no status, no span
    # (the player-strike / player-cast paths run the CWN/WWN downed seam for the
    # OPPONENT they drop; nothing handled the PLAYER going down here). No-op
    # unless check_hp_depletion above resolved a PC-down outcome with the PC at 0.
    from sidequest.server.post_resolution_lethality import apply_post_resolution_lethality

    apply_post_resolution_lethality(
        snapshot=snapshot, encounter=encounter, pack=pack, turn=round_number
    )

    return messages


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
