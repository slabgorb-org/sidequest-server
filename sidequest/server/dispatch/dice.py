"""DICE_THROW dispatch — physics-is-the-roll resolution + beat application.

Port of the DICE_THROW arm of sidequest-api/crates/sidequest-server/src/lib.rs
(and the pure helper at dice_dispatch.rs::handle_dice_throw).

Wire flow (matches Rust):
1. Rolling client clicks a confrontation beat, UI builds ``DiceRequestPayload``
   locally (no server round-trip), auto-rolls in Rapier, reads settled faces.
2. UI sends ``DICE_THROW { request_id, throw_params, face, beat_id? }``.
3. Server (here) validates inputs, resolves dice from the client-reported
   faces, and applies the beat to the active encounter — or, on a WN
   sealed round (story 102-4: SwnRulesetModule family, hp_depletion, a
   persisted initiative order), SEALS the commit until every seated
   player-side participant is in, then walks the whole round in initiative
   order. Either way it broadcasts DICE_REQUEST + DICE_RESULT to the room,
   stashes the resolved outcome + a replay-action on the session, and
   synthesizes a narrator input that describes what happened mechanically.
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

from sidequest.game.beat_filter import (
    WN_ATTACK_BEAT_ID,
    WN_CAST_SPELL_BEAT_ID,
    WN_TOTAL_DEFENSE_BEAT_ID,
    is_item_use_beat,
    is_wn_action_beat,
    wn_action_beat,
    wn_cast_beat,
)
from sidequest.game.beat_kinds import (
    ApplyResult,
    _opposite_side_first_actor,
    apply_beat_hp_channel,
)
from sidequest.game.dice import ResolveError, generate_dice_seed, resolve_dice_with_faces
from sidequest.game.encounter import (
    EncounterActor,
    EncounterPhase,
    StructuredEncounter,
    WnSealedCommit,
)
from sidequest.game.hp_depletion import check_hp_depletion
from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.ruleset.base import RulesetModule
from sidequest.game.ruleset.without_number import WithoutNumberRulesetModule
from sidequest.game.ruleset.wwn import WwnRulesetModule
from sidequest.game.session import GameSnapshot
from sidequest.game.status import status_roll_modifier
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
from sidequest.server.ability_invocation_telemetry import (
    emit_ability_invocation_unrouted,
)
from sidequest.server.dispatch.confrontation import (
    build_confrontation_payload,
    make_confrontation_frame_supplier,
    make_confrontation_portrait_resolver,
)
from sidequest.server.dispatch.damage_roll import (
    _DAMAGE_THROW_PARAMS,
    damage_request_from_spec,
    parity_damage_total,
)
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
    wn_flavor_rider_span,
    wn_native_scaffolding_suppressed_span,
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

    WN sealed-round field (story 102-4):

    - ``commitment_pending``: True when this throw SEALED in a WN
      confrontation (a seated peer is still uncommitted) — the to-hit is
      resolved but the beat will apply at the actor's initiative slot once
      the barrier closes. False when this commit fired the round (or on
      every non-WN path). Mirrors the ``opposed_pending`` defer idiom.
    """

    replay_action_text: str
    outcome: RollOutcome
    encounter_resolved: bool
    # Story 106-4 Part C: an item-use commit ("Drink <potion>") rolls NO d20 —
    # it is auto-success — so it carries no dice request/result pair. These are
    # always present on the real dice paths; None ONLY on the item-use outcome.
    request: DiceRequestPayload | None = None
    result: DiceResultPayload | None = None
    opposed_pending: bool = False
    opposed_player_d20: int | None = None
    opposed_player_beat_id: str | None = None
    commitment_pending: bool = False


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
        # Carry the request's role so the damage follow-on result is tagged
        # "damage" and the primary beat/check result stays "check" — the UI
        # overlay keys on this to avoid rendering a damage roll as the primary.
        roll_role=request.roll_role,
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


def _format_commit_replay_action(
    *,
    beat_label: str,
    stat_check: str,
    total: int,
    outcome: RollOutcome,
    waiting_on: list[str],
    player_action: str | None = None,
) -> str:
    """Synthetic narrator input for a SEALED WN commit (story 102-4).

    The Main Action is locked in but nothing has resolved — the narrator may
    describe intent and table state, never an outcome. Mirrors
    ``_format_replay_action``'s PLAYER_ACTION prepend behavior.
    """
    summary = (
        f"[ACTION_COMMITTED] {beat_label} ({stat_check}): roll {total} "
        f"({outcome.value}) is SEALED — the round resolves in initiative "
        f"order once every participant commits (waiting on: "
        f"{', '.join(waiting_on) or 'nobody'}). Do NOT narrate the action "
        "landing, missing, or any other mechanical outcome yet."
    )
    if player_action and player_action.strip():
        return f"PLAYER_ACTION: {player_action.strip()}\n{summary}"
    return summary


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
    """Apply or seal a beat, resolve dice, broadcast wire messages, return outcome.

    On the WN sealed-round path (SwnRulesetModule family + hp_depletion +
    persisted initiative) the beat SEALS — application and the damage
    broadcast defer until the barrier-closing commit walks the round.

    Raises ``DiceDispatchError`` when the throw can't be resolved — no
    partial state mutation leaks because beat apply only runs after ALL
    request validation succeeds: stat canonicalization AND the WN
    cast-shape checks (spell_id presence on a wwn cast_spell, catalog
    membership, no spell_id on non-cast beats, no cast on opposed_check
    cdefs — story 102-2).

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

    # Sealed-letter dogfight (story 158-49): a ``sealed_letter_lookup`` confrontation
    # resolves by SIMULTANEOUS maneuver commit (``resolve_sealed_letter_lookup``), and
    # its gun roll arrives via the pending-shot path in the DICE_THROW handler (which
    # returns before this dispatcher) — NEVER the personal d20 resolution below. A beat
    # reaching here for a sealed-letter cdef is the 158-49 bug: the forced-dispatch
    # dogfight handed the player the WN personal-combat menu, and committing "attack"
    # synthesized a STR strike that crashed ``without_number.attack_params`` on the SWN
    # stat block (the 2026-06-27 coyote_star ws-teardown soft-lock). Reject LOUD at the
    # seam (No Silent Fallbacks / AC3) instead of routing a ship duel through the
    # ground-combat dice path.
    if cdef.resolution_mode == ResolutionMode.sealed_letter_lookup:
        raise DiceDispatchError(
            f"beat_id {payload.beat_id!r} committed via DICE_THROW on sealed-letter "
            f"confrontation {encounter.encounter_type!r}: dogfight maneuvers resolve "
            "through the sealed-letter commit path, not the d20 dice path"
        )

    # Story 106-4 Part C: a "Drink <potion>" item-use beat is not authored on
    # the cdef — it's a transient beat synthesized from the actor's inventory.
    # It resolves auto-success (no d20) and costs the Main Action; route it to
    # the dedicated handler BEFORE the cdef beat lookup (which would otherwise
    # reject the unknown id).
    if is_item_use_beat(payload.beat_id):
        return _dispatch_item_use(
            payload=payload,
            rolling_player_id=rolling_player_id,
            character_name=character_name,
            encounter=encounter,
            cdef=cdef,
            ruleset=ruleset,
            pack=pack,
            genre_slug=genre_slug,
            session_id=session_id,
            round_number=round_number,
            room_broadcast=room_broadcast,
            snapshot=snapshot,
            emit_confrontation=emit_confrontation,
        )

    # Story 108-8 (ADR-143): under a Without-Number binding the WN engine OWNS the
    # action set — synthesize a transient beat for a core WN action (attack) instead
    # of looking it up in cdef.beats, which 108-3 strips to [] on WWN combat defs.
    # Mirrors the item-use transient-beat intercept above, BEFORE the cdef lookup
    # that would otherwise reject the id (the total-combat-outage this story fixes).
    # Gated to the WN binding so native packs — which may author their own "attack"
    # beat — keep the authored-beat lookup unchanged.
    if isinstance(ruleset, WithoutNumberRulesetModule) and is_wn_action_beat(payload.beat_id):
        beat = wn_action_beat(payload.beat_id)
    elif (
        payload.beat_id == WN_CAST_SPELL_BEAT_ID
        and pack
        and pack.rules
        and pack.rules.ruleset == "wwn"
    ):
        # Story 152-2 (ADR-143): cast_spell is a synthesized WWN action — 108-3
        # stripped it from cdef.beats, so it misses the is_wn_action_beat attack
        # synthesis above and would otherwise fall into the empty-cdef raise below
        # BEFORE the (already-correct) cast validations + spine ever run. Synthesize
        # the transient cast beat here, mirroring the is_item_use_beat intercept, so
        # the request reaches the cast-shape guards (spell_id/catalog/opposed) and
        # the cast spine in _apply_committed_player_beat. WWN-gated: a cast_spell on
        # any other ruleset stays a loud unknown-beat raise (No Silent Fallbacks).
        beat = wn_cast_beat()
    else:
        beat = next((b for b in cdef.beats if b.id == payload.beat_id), None)
        if beat is None:
            available = ",".join(b.id for b in cdef.beats)
            raise DiceDispatchError(
                f"unknown beat_id {payload.beat_id!r} for encounter "
                f"{encounter.encounter_type!r} — available: [{available}]"
            )

    # WN cast routing (story 102-2): a wwn cast_spell commit names WHICH
    # prepared spell via ``payload.spell_id`` so the dice path can reach the
    # same cast spine the narrator apply_beat path uses. Validate the request
    # shape HERE, before any state mutation, so a malformed commit is a loud
    # typed rejection (No Silent Fallbacks) with zero half-applied state:
    #   - spell_id on a non-cast beat is a client bug — silently ignoring a
    #     mechanical request field is exactly the silent-fallback failure mode;
    #   - a cast commit with no spell_id is the pre-102-2 bug (the generic
    #     stat throw) — the picker always sends one, so its absence means a
    #     stale/buggy client;
    #   - a spell_id unknown to the resolved (world-first) catalog is a
    #     client/content bug — never improvise a cast.
    # Economy refusals (no casts remaining, not prepared) are NOT validated
    # here: those are valid requests the spine refuses-but-records on
    # ``wwn.spell.cast`` (refused=True), in parity with apply_beat refusals.
    is_wwn_cast = bool(
        payload.beat_id == "cast_spell" and pack and pack.rules and pack.rules.ruleset == "wwn"
    )
    if payload.spell_id is not None and not is_wwn_cast:
        raise DiceDispatchError(
            f"spell_id {payload.spell_id!r} is only valid on a wwn cast_spell "
            f"beat commit; got beat_id {payload.beat_id!r} on ruleset "
            f"{pack.rules.ruleset if pack and pack.rules else None!r}"
        )
    if is_wwn_cast:
        if payload.spell_id is None:
            raise DiceDispatchError(
                "cast_spell commit missing spell_id — the spell picker must "
                "name the prepared spell being cast (story 102-2); a generic "
                "stat throw is not a valid cast resolution"
            )
        # Review round 2: the cast spine runs in the non-opposed branch
        # below. A wwn cast on an opposed_check cdef would pass validation
        # and then SILENTLY skip the spine (no span, no spend) — the exact
        # silent-fallback shape this epic kills. No current content ships
        # the combination; reject loudly until a story defines opposed-cast
        # semantics.
        if cdef.resolution_mode == ResolutionMode.opposed_check:
            raise DiceDispatchError(
                f"cast_spell with spell_id {payload.spell_id!r} on an "
                f"opposed_check confrontation {cdef.confrontation_type!r} — "
                "the WN cast spine has no opposed-check arm; author the "
                "cast beat on a beat_selection/hp_depletion confrontation "
                "(No Silent Fallbacks)"
            )
        from sidequest.server.dispatch.wwn_spell_catalog_resolve import (
            resolve_wwn_spell_catalog,
        )

        catalog = resolve_wwn_spell_catalog(pack, snapshot.world_slug)
        if catalog is None:
            raise DiceDispatchError(
                f"cast_spell commit for {payload.spell_id!r} but no WWN spell "
                f"catalog resolves for world {snapshot.world_slug!r} (pack "
                "data bug — CLAUDE.md 'no silent fallback')"
            )
        try:
            catalog.get(payload.spell_id)
        except KeyError as exc:
            available_spells = ",".join(s.id for s in catalog.spells)
            raise DiceDispatchError(
                f"unknown spell_id {payload.spell_id!r} for cast_spell — "
                f"available: [{available_spells}]"
            ) from exc

    # AWN mutation routing (story 158-54): a mutation_resolution-marked beat
    # commit names WHICH owned mutation via ``payload.mutation_id`` so the
    # dice path can reach the same use_ops spine the freeplay/narrator paths
    # use — the 102-2 cast-guard contract, retold for mutations. Validate the
    # request shape HERE, before any state mutation (No Silent Fallbacks):
    #   - mutation_id on an unmarked beat or a non-AWN ruleset is a client
    #     bug — silently ignoring a mechanical request field is the exact
    #     silent-fallback failure mode;
    #   - a mutation-beat commit with no mutation_id is the pre-158-54 bug
    #     (the silent bare-strike resolution) — the picker always sends one.
    # Economy refusals (not owned, limit exhausted, strain over max) are NOT
    # validated here: those are valid requests the spine refuses-but-records
    # on ``awn.mutation.refused``, in parity with the freeplay refusals.
    is_awn_mutation_beat = bool(
        getattr(beat, "mutation_resolution", False)
        and pack
        and pack.rules
        and pack.rules.ruleset == "awn"
    )
    if payload.mutation_id is not None and not is_awn_mutation_beat:
        raise DiceDispatchError(
            f"mutation_id {payload.mutation_id!r} is only valid on an awn "
            f"mutation_resolution beat commit; got beat_id {payload.beat_id!r} "
            f"on ruleset {pack.rules.ruleset if pack and pack.rules else None!r}"
        )
    if is_awn_mutation_beat and payload.mutation_id is None:
        raise DiceDispatchError(
            "mutation beat commit missing mutation_id — the mutation picker "
            "must name WHICH owned mutation manifests (story 158-54); a "
            "generic stat throw is not a valid mutation resolution"
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
        runner_core = snapshot.find_creature_core(character_name)
        modifier = int_mod + program_skill + status_roll_modifier(runner_core)
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
    # WWN/CWN Shock chip HP actually removed this beat (miss-damage seam).
    # Initialized at this level so the persisted beat-applied forensics event
    # and the resolution close below can read it on every branch — barsoom-2
    # playtest 2026-06-10: a shock chip killed the seated Other on a CritFail
    # and NO persisted surface recorded the ablation, so the (correct)
    # hp_depletion resolution read as the engine fabricating a win.
    shock_hp_removed = 0
    # HP the player's own strike removed via the damage channel (apply_beat).
    # Initialized at the same level for the same reason: the kill-overclaim
    # anchor below the resolution close reads it (evropi 2/14 + barsoom 3/10,
    # 2026-06-10: the narrator rendered kill prose for an Other the engine
    # correctly kept alive, because nothing anchored the post-strike HP).
    strike_hp_removed = 0
    # Story 71-21: opponent reprisal dice (to-hit + damage), built when the
    # seated opponent takes its server-driven attack turn. Broadcast AFTER the
    # player's own dice pair so the overlay shows the player's roll, then the
    # enemy's answer. Empty when no reprisal fires (opposed/dial/native paths).
    opponent_reprisal_messages: list[object] = []
    # WN sealed round (story 102-4): blind commitment, initiative-ordered
    # resolution. Capability binding is isinstance against the module class
    # (epic invariant — no genre branches); the walk rides the persisted P4
    # initiative order, so a WN hp_depletion combat with NO persisted order
    # (pre-P4 saves, direct-construction fixtures) keeps the legacy
    # immediate-resolution path — loudly, never silently.
    commitment_pending = False
    wn_waiting: list[str] = []
    wn_round_messages: list[object] = []
    wn_sealed_round = (
        not opposed_pending
        and isinstance(ruleset, WithoutNumberRulesetModule)
        and cdef.win_condition == "hp_depletion"
        and bool(encounter.initiative)
    )
    if (
        not opposed_pending
        and not wn_sealed_round
        and isinstance(ruleset, WithoutNumberRulesetModule)
        and cdef.win_condition == "hp_depletion"
    ):
        # Story 106-2 (Option A): WWN combat resolves the opponent attack ONLY
        # through the sealed initiative walk — a committed full-defend can make
        # the opponent's slot miss, which the legacy unconditional reprisal can
        # never honor. So a WWN hp_depletion fight with no persisted initiative
        # is a real failure, not a cue to silently degrade to the legacy rider
        # (No Silent Fallbacks). Bound to the WWN module class (ADR-117), NOT the
        # whole SWN family: space_opera/perseus_cloud (story 71-21) deliberately
        # reprises with no initiative on the legacy path and must keep doing so.
        if isinstance(ruleset, WwnRulesetModule):
            raise DiceDispatchError(
                "WWN hp_depletion combat dispatched without a persisted "
                f"initiative order (encounter={encounter.encounter_type!r}). The "
                "WWN reprisal model (story 106-2, Option A) resolves the opponent "
                "attack only through the sealed initiative walk so a defensive "
                "beat can blunt or prevent it — there is no WWN fallback to the "
                "legacy unconditional reprisal. Seat the fight with a rolled "
                "initiative order (the P4 spine) before dispatching a beat."
            )
        logger.warning(
            "dice.wn_round_skipped reason=no_persisted_initiative encounter=%s "
            "— WN combat dispatched without a P4 initiative order; resolving "
            "on the legacy immediate path",
            encounter.encounter_type,
        )

    # Story 108-5 (ADR-143): the RP-flavor rider lie-detector. When a WN combat
    # throw carries freeform ``player_action`` text — the "chandelier swing" the
    # player typed alongside the action button — emit {slug}.action.flavor_rider
    # proving the text was attached as narrator color ONLY and never entered
    # mechanical resolution (the roll is resolved on the button; the rider is
    # downstream cosmetic context). The text still rides into replay_action_text
    # below, but the d20/weapon dice are computed without consulting it. Pairs
    # with 108-1's native_scaffolding_suppressed: together they prove the WN
    # round resolved on the button and ONLY on the button. Scoped to WN
    # hp_depletion combat — dial confrontations under a WN pack keep the native
    # engine and are out of this feature's scope.
    if (
        isinstance(ruleset, WithoutNumberRulesetModule)
        and cdef.win_condition == "hp_depletion"
        and payload.player_action
        and payload.player_action.strip()
    ):
        wn_flavor_rider_span(
            slug=ruleset.slug,
            actor=character_name,
            beat_id=payload.beat_id or "",
        )

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
        encounter_resolved = False
    elif wn_sealed_round:
        # WN turn model (story 102-4): the throw SEALS — the to-hit resolved
        # above, but the beat applies at the actor's initiative slot only
        # after every seated player-side participant has committed. The
        # barrier-closing commit walks the whole round (committed →
        # initiative → resolved) in the same dispatch; a solo table is a
        # 1-participant barrier, never a special case.
        from sidequest.server.dispatch.wn_round import (
            run_wn_round,
            seal_wn_commit,
            wn_barrier_closed,
            wn_waiting_actors,
        )

        seal_wn_commit(
            encounter=encounter,
            actor=actor,
            beat=beat,
            outcome=resolved.outcome,
            spell_id=payload.spell_id,
            mutation_id=payload.mutation_id,
        )
        wn_waiting = wn_waiting_actors(encounter=encounter, snapshot=snapshot)
        if wn_barrier_closed(encounter=encounter, snapshot=snapshot):
            round_result = run_wn_round(
                encounter=encounter,
                cdef=cdef,
                ruleset=ruleset,
                pack=pack,
                snapshot=snapshot,
                session_id=session_id,
                round_number=round_number,
                rolling_player_id=rolling_player_id,
                rng=random,
            )
            wn_round_messages = round_result.messages
        else:
            commitment_pending = True
        encounter_resolved = encounter.resolved
    else:
        _application = _apply_committed_player_beat(
            beat_id=payload.beat_id,
            spell_id=payload.spell_id,
            mutation_id=payload.mutation_id,
            character_name=character_name,
            rolling_player_id=rolling_player_id,
            beat=beat,
            actor=actor,
            outcome_tier=resolved.outcome,
            encounter=encounter,
            cdef=cdef,
            ruleset=ruleset,
            pack=pack,
            snapshot=snapshot,
            session_id=session_id,
            round_number=round_number,
        )
        encounter_resolved = _application.encounter_resolved
        strike_hp_removed = _application.strike_hp_removed
        shock_hp_removed = _application.shock_hp_removed
        damage_request_payload = _application.damage_request_payload
        damage_result_payload = _application.damage_result_payload

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
    if not wn_sealed_round:
        # On the WN sealed path the per-beat resolution close runs inside the
        # round walk (one close per applied slot) — running it again here
        # would double-stamp directives. Opposed/legacy paths close here.
        _emit_player_beat_resolution_close(
            encounter=encounter,
            snapshot=snapshot,
            character_name=character_name,
            beat=beat,
            actor_side=actor.side,
            outcome_tier=resolved.outcome,
            strike_hp_removed=strike_hp_removed,
            shock_hp_removed=shock_hp_removed,
            encounter_resolved=encounter_resolved,
            win_condition=cdef.win_condition,
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

        # Story 102-4: the WN round walk's messages (per-slot damage pairs,
        # opponent attack dice, incapacitation surfaces), in slot order.
        for _round_msg in wn_round_messages:
            room_broadcast(_round_msg)

        # Story 45-3 / 59-20: Mid-turn CONFRONTATION emit. The metric mutation
        # already landed via apply_beat (or, on a sealed WN commit, the frame
        # carries the committed_actors ledger so the table sees who the round
        # waits on); without this the UI sits on the prior turn's
        # CONFRONTATION snapshot through the entire dice + narration cycle
        # (5–15s). Skipped on the opposed branch where deltas are deferred to
        # narration_apply.
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

    if commitment_pending:
        # A sealed commit resolved NOTHING — handing the narrator the
        # [BEAT_RESOLVED] shape would invite prose for a hit that hasn't
        # landed (the engine's version of winging it). Name the seal and the
        # actors the barrier waits on instead.
        replay_text = _format_commit_replay_action(
            beat_label=beat.label,
            stat_check=beat.stat_check,
            total=resolved.total,
            outcome=resolved.outcome,
            waiting_on=wn_waiting,
            player_action=payload.player_action,
        )
    else:
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
        "deferred_opposed=%s commitment_pending=%s",
        request.request_id,
        rolling_player_id,
        resolved.total,
        resolved.outcome.value,
        payload.beat_id,
        encounter.player_metric.current,
        encounter.opponent_metric.current,
        encounter_resolved,
        opposed_pending,
        commitment_pending,
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

    # TEA 102-4 finding: the two defer-the-beat mechanisms must never both
    # fire. The wn_sealed_round gate requires ``not opposed_pending``, so this
    # can only trip if someone edits the gate — fail loud, not implicit.
    assert not (opposed_pending and commitment_pending), (
        "opposed_pending and commitment_pending are mutually exclusive beat "
        "deferrals — both set means the dispatch gate regressed"
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
        commitment_pending=commitment_pending,
    )


def _dispatch_item_use(
    *,
    payload: DiceThrowPayload,
    rolling_player_id: str,
    character_name: str,
    encounter: StructuredEncounter,
    cdef: ConfrontationDef,
    ruleset: RulesetModule,
    pack: GenrePack,
    genre_slug: str,
    session_id: str,
    round_number: int,
    room_broadcast: Callable[[object], None] | None,
    snapshot: GameSnapshot,
    emit_confrontation: Callable[[object, Callable[[str], object]], None] | None,
) -> DiceThrowOutcome:
    """Resolve a 'Drink <potion>' item-use beat (Story 106-4 Part C).

    Auto-success, no d20: the carried consumable is consumed and its effect
    (heal) applied. It COSTS the Main Action — on the WN sealed-round path the
    seated opponent still acts at its own initiative slot; on the legacy
    immediate hp_depletion path the opponent reprisal fires after. No player
    dice pair is broadcast (nothing was rolled); the heal rides ``state_patch.hp``
    + ``confrontation.item_used`` spans, and the round/reprisal messages + a fresh
    CONFRONTATION frame are fanned out so the overlay updates.
    """
    from sidequest.game.beat_filter import item_use_beats
    from sidequest.server.dispatch.item_use import apply_item_use, resolve_item_use

    if cdef.win_condition != "hp_depletion":
        raise DiceDispatchError(
            f"item-use beat {payload.beat_id!r} on a non-hp_depletion confrontation "
            f"{cdef.confrontation_type!r} — item beats are only offered in hp_depletion "
            "combat (No Silent Fallbacks)"
        )
    # Validate the carried consumable up front so a bad commit fails loud BEFORE
    # any state mutation (parity with the dice path's cast-shape validation).
    character, item_index = resolve_item_use(snapshot, character_name, payload.beat_id)
    display_name = str(character.core.inventory.items[item_index].get("name", "") or "")

    actor = encounter.find_actor(character_name)
    if actor is None:
        actor = next((a for a in encounter.actors if a.side == "player"), None)
    if actor is None:
        raise DiceDispatchError(
            f"item-use beat {payload.beat_id!r}: no player-side actor for "
            f"{character_name!r} in encounter {encounter.encounter_type!r}"
        )

    wn_sealed_round = (
        isinstance(ruleset, WithoutNumberRulesetModule)
        and cdef.win_condition == "hp_depletion"
        and bool(encounter.initiative)
    )

    commitment_pending = False
    wn_waiting: list[str] = []
    round_messages: list[object] = []

    if wn_sealed_round:
        from sidequest.server.dispatch.wn_round import (
            run_wn_round,
            seal_wn_commit,
            wn_barrier_closed,
            wn_waiting_actors,
        )

        # The transient beat the ledger reads at the actor's slot (id-only).
        item_beat = item_use_beats([character.core.inventory.items[item_index]])[0]
        seal_wn_commit(
            encounter=encounter,
            actor=actor,
            beat=item_beat,
            outcome=RollOutcome.Success,
            spell_id=None,
            mutation_id=None,
        )
        wn_waiting = wn_waiting_actors(encounter=encounter, snapshot=snapshot)
        if wn_barrier_closed(encounter=encounter, snapshot=snapshot):
            # The walk applies the item at this PC's slot (consume + heal) and
            # runs the opponent's own attack at its slot.
            round_result = run_wn_round(
                encounter=encounter,
                cdef=cdef,
                ruleset=ruleset,
                pack=pack,
                snapshot=snapshot,
                session_id=session_id,
                round_number=round_number,
                rolling_player_id=rolling_player_id,
                rng=random,
            )
            round_messages = round_result.messages
        else:
            commitment_pending = True
    else:
        # Legacy immediate hp_depletion (SWN-family non-WWN): apply the item now,
        # then the seated opponent reprises (drinking still costs the action).
        item_name, healed = apply_item_use(
            character=character, item_index=item_index, turn_num=round_number
        )
        encounter.narrator_hints.append(
            f"ITEM USED: {character_name} drank {item_name}, restoring {int(healed or 0)} HP "
            f"(now {character.core.hp.current}/{character.core.hp.max}). Describe the drink "
            "and its relief in fiction; the heal already applied mechanically."
        )
        module_has_opponent_turn = (
            type(ruleset).resolve_opponent_attack is not RulesetModule.resolve_opponent_attack
        )
        if not encounter.resolved and module_has_opponent_turn:
            round_messages = _resolve_opponent_reprisal(
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

    # Broadcast: NO player dice pair (no roll). Fan out the opponent's answer
    # (reprisal / round walk) then a fresh CONFRONTATION frame so the overlay
    # reflects the new HP + consumed item without waiting for the narrator.
    if room_broadcast is not None:
        for _msg in round_messages:
            room_broadcast(_msg)
        union_payload = ConfrontationPayload(
            **build_confrontation_payload(
                encounter=encounter,
                cdef=cdef,
                genre_slug=genre_slug,
                recipient_pc=None,
                core_resolver=snapshot.find_creature_core,
                active_stakes=snapshot.active_stakes,
                portrait_resolver=make_confrontation_portrait_resolver(
                    snapshot=snapshot, genre_pack=pack, genre_slug=genre_slug
                ),
                rules=pack.rules,
            )
        )
        with encounter_momentum_broadcast_span(
            encounter_type=encounter.encounter_type,
            player_metric_after=encounter.player_metric.current,
            opponent_metric_after=encounter.opponent_metric.current,
            source="item_use",
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
            else:
                room_broadcast(ConfrontationMessage(payload=union_payload, player_id="server"))

    if commitment_pending:
        replay_text = (
            f"[ITEM COMMITTED] {character_name} moves to drink {display_name}; "
            f"the round waits on {', '.join(wn_waiting) or 'no one'}."
        )
    else:
        replay_text = f"[ITEM USED] {character_name} drinks {display_name}."

    logger.info(
        "dice.item_use_resolved beat_id=%s actor=%s item=%r commitment_pending=%s "
        "resolved_encounter=%s",
        payload.beat_id,
        character_name,
        display_name,
        commitment_pending,
        encounter.resolved,
    )

    return DiceThrowOutcome(
        replay_action_text=replay_text,
        outcome=RollOutcome.Success,
        encounter_resolved=encounter.resolved,
        commitment_pending=commitment_pending,
    )


@dataclass(frozen=True)
class _PlayerBeatApplication:
    """What one committed player beat did when it applied.

    Extracted from the legacy in-dispatch flow (story 102-4) so the WN round
    walk can resolve the same beat at the actor's initiative slot — one beat
    implementation, two call sites (legacy immediate dispatch + the sealed
    round walk in ``wn_round.py``).
    """

    encounter_resolved: bool
    strike_hp_removed: int
    shock_hp_removed: int
    damage_request_payload: DiceRequestPayload | None
    damage_result_payload: DiceResultPayload | None


def _resolve_wn_committed_action(
    *,
    encounter: StructuredEncounter,
    actor: EncounterActor,
    beat: BeatDef,
    beat_id: str,
    slug: str,
    outcome_tier: RollOutcome,
    damage_resolver: Callable[[], int] | None,
    edge_resolver: Callable[[str], object | None],
) -> ApplyResult:
    """Resolve one committed player action under a Without-Number binding
    WITHOUT the native beat engine (story 108-1, ADR-143).

    We bind Without Number so we never balance combat (SOUL: "Bind the Ruleset,
    Don't Balance It"). The native ``apply_beat`` scaffolding is REMOVED from
    the WN combat path, not tuned to fit it — no fleeting-tag grant (Opening /
    Counter Stance), no dial-metric advance, no composure/edge rider, no
    Brace-as-an-action, no taunt activation. What remains is the WN math: the
    strike's already-rolled weapon dice land on the target's ablative HP
    (ADR-114) through the same ``apply_beat_hp_channel`` helper the native
    engine used, and a 0-HP drop fires the hp_depletion win condition via
    ``check_hp_depletion`` — the exact HP-resolution tail of
    ``beat_kinds.apply_beat``, with every native dial/tag rider cut.

    Story 153-12 — the ✦ resolution-beat exit: the native ``apply_beat``
    resolution branch (which ends the confrontation on a ✦ beat) is one of the
    riders cut above, so under ``hp_depletion`` a succeeded Fall Back / Disengage
    no-op'd and combat had no voluntary exit (aureate_span playtest). We
    re-introduce ONLY the WN-SRD-faithful result — a successful Disengage / Full
    Retreat dissolves the engagement — NOT a native dial: a ✦-marked beat
    (``beat.resolution``) that SUCCEEDS (``Success``/``CritSuccess``) ends the
    confrontation non-lethally (``outcome="resolution_beat:<id>"``, distinct from
    an hp_depletion win — nobody was defeated). Tier-gated: a FAILED disengage
    (``Fail``/``CritFail``) leaves combat live — the ✦ beat has a DC, a botched
    withdrawal is a failed skill check, not a free exit.

    Emits ``{slug}.native_scaffolding_suppressed`` (the GM-panel lie-detector
    that the native engine is OFF) and returns an ``ApplyResult`` with
    ``deltas=None`` (no dial deltas exist on this path).
    """
    damage_channel = str(getattr(beat, "damage_channel", "none") or "none")
    hp_removed = 0
    if damage_channel == "strike" and damage_resolver is not None:
        damage_total = damage_resolver()
        primary_target = _opposite_side_first_actor(encounter, actor.side)
        if primary_target is not None:
            hp_target = edge_resolver(primary_target)
            if hp_target is not None:
                # Mitigation parity with apply_beat's strike channel: a
                # ``mitigation_override`` on the beat wins; otherwise the first
                # armor item on the target supplies flat mitigation.
                mitigation_override = getattr(beat, "mitigation_override", None)
                if mitigation_override is not None:
                    target_mitigation = int(mitigation_override)
                else:
                    target_mitigation = 0
                    for item_dict in hp_target.inventory.items:
                        mit = item_dict.get("mitigation")
                        if mit is not None:
                            target_mitigation = int(mit)
                            break
                hp_removed = apply_beat_hp_channel(
                    target=hp_target,
                    channel="strike",
                    damage_total=damage_total,
                    target_mitigation=target_mitigation,
                    source_beat_id=beat_id,
                )

    resolved = check_hp_depletion(encounter, edge_resolver, beat_id=beat_id) is not None

    # Story 153-12 — succeeded ✦ resolution beat ends the confrontation as a
    # non-lethal exit (WN SRD Disengage / Full Retreat). HP depletion above takes
    # precedence (a kill wins over a withdrawal in the same beat); this only fires
    # when the engagement is still live. Gate: the ✦ marker (beat.resolution) AND a
    # successful outcome — a failed disengage leaves combat live (153-12 AC2). The
    # outcome is "resolution_beat:<id>", NOT a player/opponent victory: the fight
    # dissolved, nobody was defeated, so downstream win handlers stay clear (AC3).
    # No dial is moved (AC4 — the inert hp_depletion dials are untouched; ADR-143).
    if (
        not resolved
        and not encounter.resolved
        and getattr(beat, "resolution", False)
        and outcome_tier in (RollOutcome.Success, RollOutcome.CritSuccess)
    ):
        encounter.resolved = True
        encounter.outcome = f"resolution_beat:{beat_id}"
        encounter.structured_phase = EncounterPhase.Resolution
        resolved = True
        # OTEL lie-detector (AC5): the GM panel must distinguish a resolution-beat
        # disengage from an hp_depletion kill. The downstream encounter.resolved
        # close carries outcome="resolution_beat:<id>"; this marks the decision itself.
        _watcher_publish(
            "state_transition",
            {
                "field": "encounter",
                "op": "wn_resolution_beat_exit",
                "actor": actor.name,
                "beat_id": beat_id,
                "outcome_tier": outcome_tier.value
                if hasattr(outcome_tier, "value")
                else str(outcome_tier),
                "reason": "resolution_beat",
                "rationale": (
                    "win_condition=hp_depletion — a succeeded ✦ resolution beat is the "
                    "WN SRD Disengage/Full Retreat result; the engagement dissolved "
                    "non-lethally (no kill, no dial victory)"
                ),
            },
            component="encounter",
            severity="info",
        )

    wn_native_scaffolding_suppressed_span(
        slug=slug,
        actor=actor.name,
        beat_id=beat_id,
        hp_removed=hp_removed,
        suppressed="fleeting_tag,dial_advance,composure_rider,brace_action,taunt",
    )
    return ApplyResult(
        deltas=None,
        resolved=resolved,
        skipped_reason=None,
        impact=None,
        hp_removed=hp_removed,
    )


def _apply_committed_player_beat(
    *,
    beat_id: str,
    spell_id: str | None,
    mutation_id: str | None = None,
    character_name: str,
    rolling_player_id: str,
    beat: BeatDef,
    actor: EncounterActor,
    outcome_tier: RollOutcome,
    encounter: StructuredEncounter,
    cdef: ConfrontationDef,
    ruleset: RulesetModule,
    pack: GenrePack,
    snapshot: GameSnapshot,
    session_id: str,
    round_number: int,
) -> _PlayerBeatApplication:
    """Apply one player's resolved beat to the encounter (strike damage,
    Shock, downed seam, WN cast spine, beat_applied telemetry). The to-hit
    is already resolved — ``outcome_tier`` is the tier the dice produced at
    commit time. Request-shape validation (cast spell_id, stat) happened
    before any mutation, at dispatch."""
    damage_request_payload: DiceRequestPayload | None = None
    damage_result_payload: DiceResultPayload | None = None
    shock_hp_removed = 0
    is_wwn_cast = bool(
        beat_id == "cast_spell" and pack and pack.rules and pack.rules.ruleset == "wwn"
    )
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
        _acting_char = next((c for c in snapshot.characters if c.core.name == character_name), None)
        _class_def = (
            next(
                (cls for cls in pack.classes if cls.display_name == _acting_char.char_class),
                None,
            )
            if _acting_char is not None
            else None
        )
        _is_wwn_warrior = bool(_class_def is not None and _class_def.warrior)

    if damage_channel == "strike" and outcome_tier not in (
        RollOutcome.Fail,
        RollOutcome.CritFail,
    ):
        actor_core = snapshot.find_creature_core(character_name)
        damage_spec = ruleset.resolve_damage(
            beat=beat,
            actor_core=actor_core,
            pack=pack,
            world_slug=snapshot.world_slug,
        )
        if damage_spec is None:
            logger.warning(
                "dice.damage_spec_missing beat=%r actor=%r encounter=%r — "
                "strike beat has no resolvable weapon, damage_override, or "
                "unarmed default; HP damage skipped (CLAUDE.md no-fabricate)",
                beat_id,
                character_name,
                encounter.encounter_type,
            )
            _watcher_publish(
                "state_transition",
                {
                    "field": "encounter",
                    "op": "damage_spec_missing",
                    "beat_id": beat_id,
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
            # Lie-detector: when the strike resolved no weapon and fell back
            # to the genre unarmed floor, emit a span so the GM panel can tell
            # a real unarmed hit from narrator improvisation. Identity match —
            # the resolver returns the exact pack.rules.unarmed_damage object.
            _unarmed_floor = pack.rules.unarmed_damage if pack and pack.rules else None
            if _unarmed_floor is not None and damage_spec is _unarmed_floor:
                _watcher_publish(
                    "state_transition",
                    {
                        "field": "encounter",
                        "op": "unarmed_strike_floor",
                        "beat_id": beat_id,
                        "actor": character_name,
                        "dice": damage_spec.dice,
                        "rationale": (
                            "strike resolved no weapon/override/catalog damage; "
                            "fell back to pack.rules.unarmed_damage floor"
                        ),
                    },
                    component="encounter",
                    severity="info",
                )
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
            # A parity (d2) spec threw a backing d6 for the overlay; map the
            # settled faces to d2 values for the HP total so the raw d6 never
            # leaks into damage. dmg_resolved.rolls still carries the real d6
            # faces, so the broadcast/readout shows the physical throw.
            if damage_spec.is_parity_die:
                dmg_total = parity_damage_total(dmg_faces, damage_request_payload.modifier)
            else:
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
                    "beat_id": beat_id,
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
    if damage_channel == "strike" and outcome_tier in (
        RollOutcome.Fail,
        RollOutcome.CritFail,
    ):
        actor_core = snapshot.find_creature_core(character_name)
        shock_spec = ruleset.resolve_damage(
            beat=beat,
            actor_core=actor_core,
            pack=pack,
            world_slug=snapshot.world_slug,
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
                shock_hp_removed = apply_beat_hp_channel(
                    target=shock_target_core,
                    channel="strike",
                    damage_total=chip,
                    target_mitigation=0,
                    source_beat_id=f"{beat_id}:shock",
                )
                if shock_hp_removed > 0:
                    # Text-log + GM-timeline visibility for the chip. The
                    # wwn.shock.applied span (resolve_shock) is live-OTEL
                    # only — log greps and the persisted /encounter_events
                    # feed are structurally blind to it, which is how the
                    # barsoom-2 shock kill masqueraded as a fabricated win.
                    logger.info(
                        "dice.shock_chip_applied actor=%s target=%s beat_id=%s "
                        "chip=%d hp_after=%d/%d",
                        character_name,
                        shock_target_name,
                        beat_id,
                        shock_hp_removed,
                        shock_target_core.hp.current,
                        shock_target_core.hp.max,
                    )
                    _watcher_publish(
                        "state_transition",
                        {
                            "field": "encounter",
                            "op": "shock_chip_applied",
                            "actor": character_name,
                            "target": shock_target_name,
                            "beat_id": beat_id,
                            "outcome_tier": outcome_tier.value
                            if hasattr(outcome_tier, "value")
                            else str(outcome_tier),
                            "chip": shock_hp_removed,
                            "target_hp_after": shock_target_core.hp.current,
                            "target_hp_max": shock_target_core.hp.max,
                            "source": "dice_throw_shock",
                        },
                        component="encounter",
                    )

    # Story 108-1 / ADR-143 — the engine core cut. Under a Without-Number
    # binding the native beat engine is REMOVED from the WN COMBAT path (not
    # tuned to fit it): resolve the committed action with WN math only — weapon
    # dice → ablative HP + the hp_depletion win check — and emit the
    # native-scaffolding-suppressed lie-detector span. SCOPE is hp_depletion
    # COMBAT only (epic 108): dial confrontations under a WN pack — CWN net-run,
    # chase, negotiation — KEEP the native ADR-033 dial engine, so the gate
    # requires win_condition=="hp_depletion". Native packs (the isinstance gate
    # excludes them) keep the full dial/tag engine for every confrontation.
    if isinstance(ruleset, WithoutNumberRulesetModule) and cdef.win_condition == "hp_depletion":
        apply_result = _resolve_wn_committed_action(
            encounter=encounter,
            actor=actor,
            beat=beat,
            beat_id=beat_id,
            slug=ruleset.slug,
            outcome_tier=outcome_tier,
            damage_resolver=damage_resolver_fn,
            edge_resolver=snapshot.find_creature_core,
        )
    else:
        apply_result = ruleset.apply_beat(
            encounter=encounter,
            actor=actor,
            beat=beat,
            outcome=outcome_tier,
            turn=round_number,
            edge_resolver=snapshot.find_creature_core,
            damage_resolver=damage_resolver_fn,
        )

    if apply_result.skipped_reason:
        raise DiceDispatchError(f"beat {beat_id!r} skipped: {apply_result.skipped_reason}")

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

    # WN cast spine (story 102-2): route the committed cast through the
    # SAME resolution the narrator apply_beat path uses — one cast
    # implementation, two entry points (epic 102 "Reuse-first"). The d20
    # throw that produced ``outcome_tier`` is NOT a to-hit gate: WWN High Magic casting is
    # automatic (SRD §4.2 — the DEFENDER saves), so the spine runs
    # regardless of ``outcome_tier``. The spine spends the cast
    # (refused-but-recorded on an economy failure), applies rolled spell
    # damage through the HP channel, runs the shared downed seam, fires
    # the hp_depletion win condition, and emits ``wwn.spell.cast`` on
    # every call — the GM-panel lie detector. Function-level import:
    # narration_apply is a heavy module and dispatch must not pull it at
    # import time.
    if is_wwn_cast:
        from sidequest.agents.orchestrator import BeatSelection
        from sidequest.server.narration_apply import _resolve_wwn_cast_for_beat

        _resolve_wwn_cast_for_beat(
            sel=BeatSelection(
                actor=actor.name,
                beat_id="cast_spell",
                outcome=outcome_tier,
                spell_id=spell_id,
            ),
            actor=actor,
            snapshot=snapshot,
            pack=pack,
            encounter=encounter,
            cdef=cdef,
        )

    # AWN mutation spine (story 158-54): route the committed mutation beat
    # through the SAME use_ops resolution the freeplay/narrator paths use —
    # one mutation implementation, three entry points (the 102-2 cast-spine
    # doctrine, retold). The d20 throw that produced ``outcome_tier`` is NOT
    # a to-hit gate: an AWN mutation power fires and the TARGET saves
    # (use_ops — cost paid, then save resolved), so the spine runs regardless
    # of the face. Ownership/limit/strain refusals are recorded on
    # ``awn.mutation.refused`` by the spine itself — engagement, never
    # silence. Function-level import: narration_apply is a heavy module and
    # dispatch must not pull it at import time.
    is_awn_mutation = bool(
        getattr(beat, "mutation_resolution", False)
        and pack
        and pack.rules
        and pack.rules.ruleset == "awn"
    )
    if is_awn_mutation:
        from sidequest.agents.orchestrator import BeatSelection
        from sidequest.server.narration_apply import _resolve_mutation_for_beat

        _resolve_mutation_for_beat(
            sel=BeatSelection(
                actor=actor.name,
                beat_id=beat_id,
                target=_opposite_side_first_actor(encounter, actor.side),
                mutation_id=mutation_id,
            ),
            actor=actor,
            snapshot=snapshot,
            pack=pack,
        )

    own_delta = apply_result.deltas.own if apply_result.deltas else 0

    with encounter_beat_applied_span(
        encounter_type=encounter.encounter_type,
        actor=character_name,
        beat_id=beat_id,
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
            "beat_id": beat_id,
            "beat_kind": str(beat.kind.value) if hasattr(beat.kind, "value") else str(beat.kind),
            "outcome_tier": outcome_tier.value
            if hasattr(outcome_tier, "value")
            else str(outcome_tier),
            "own_delta": own_delta,
            "opponent_delta": apply_result.deltas.opponent if apply_result.deltas else 0,
            # ADR-114 §2 / forensics lie-detector: the HP actually removed from
            # the target by the strike damage channel. ``opponent_delta`` above is
            # the *dial* delta and is suppressed to 0 under hp_depletion, so without
            # this field a post-hoc reader of ENCOUNTER_BEAT_APPLIED sees a CritSuccess
            # strike that "did nothing" while the CreatureCore HpPool actually dropped.
            # The TOTAL includes the Shock chip (miss-damage) — barsoom-2 2026-06-10:
            # a shock chip removed the Other's last 3 HP on a CritFail and the
            # hit-path-only field read 0, making the correct resolution look
            # fabricated. ``shock_hp_removed`` attributes the shock share.
            "opponent_hp_removed": apply_result.hp_removed + shock_hp_removed,
            "shock_hp_removed": shock_hp_removed,
            "metric_target": encounter.encounter_type,
            "source": "dice_throw",
        },
        component="encounter",
    )
    # Story 45-9: bump total_beats_fired counter + OTEL.
    snapshot.record_beat_fired(
        beat_id=beat_id,
        encounter_type=encounter.encounter_type,
        turn=round_number,
        source="dice_throw",
    )

    # Review round 2 (102-2): derive the post-beat resolution from the
    # AUTHORITATIVE encounter state, not just apply_beat's return — a
    # killing CAST resolves the encounter in the spine (check_hp_depletion)
    # AFTER apply_beat, so apply_result.resolved is stale-False there and
    # the dead opponent would take its reprisal swing in a fight already
    # won (ADR-139 win-condition liveness). Strikes resolve inside
    # apply_beat, so this is a strict widening, never a narrowing.
    encounter_resolved = apply_result.resolved or encounter.resolved
    strike_hp_removed = apply_result.hp_removed

    return _PlayerBeatApplication(
        encounter_resolved=encounter_resolved,
        strike_hp_removed=strike_hp_removed,
        shock_hp_removed=shock_hp_removed,
        damage_request_payload=damage_request_payload,
        damage_result_payload=damage_result_payload,
    )


def _emit_player_beat_resolution_close(
    *,
    encounter: StructuredEncounter,
    snapshot: GameSnapshot,
    character_name: str,
    beat: BeatDef,
    actor_side: str,
    outcome_tier: RollOutcome,
    strike_hp_removed: int,
    shock_hp_removed: int,
    encounter_resolved: bool,
    win_condition: str = "",
    source: str = "dice_throw_beat",
) -> None:
    """Per-beat resolution close: the [ENCOUNTER RESOLVED] narrator signal on
    a resolving beat, the kill-overclaim HP anchor on a damaging one, or the
    failed-strike anchor on a 0-damage non-resolving one (HP combat only).
    Shared by the legacy in-dispatch flow and the WN round walk (story 102-4)
    so the two closes cannot drift.

    ``win_condition`` is the confrontation's ``cdef.win_condition``; the
    0-damage failed-strike anchor fires ONLY when it is ``"hp_depletion"`` so
    the HP-framed "unharmed / still standing" prose never leaks into a dial
    confrontation (negotiation, chase, poker) where a 0-strike-damage beat is
    the normal case, not a missed blow.

    ``source`` labels which seam closed the fight on the ``encounter.resolved``
    span and the persisted op="resolved" watcher row — "dice_throw_beat" for
    the legacy in-dispatch close, "wn_round" when the WN round walk applies
    the beat at its initiative slot (review rework r1: the lie-detector's own
    label must not lie about the seam)."""
    if encounter_resolved:
        with encounter_resolved_span(
            encounter_type=encounter.encounter_type,
            outcome=encounter.outcome or "",
            source=source,
        ):
            pass
        _watcher_publish(
            "state_transition",
            {
                "field": "encounter",
                "op": "resolved",
                "encounter_type": encounter.encounter_type,
                "outcome": encounter.outcome or "",
                "source": source,
                "final_player_metric": encounter.player_metric.current,
                "final_opponent_metric": encounter.opponent_metric.current,
            },
            component="encounter",
        )
        # barsoom-2 playtest 2026-06-10: the PLAYER-beat resolution close told
        # the narrator NOTHING — unlike the reprisal close (2026-06-07 fix,
        # below in _resolve_opponent_reprisal), it stamped no resolution signal
        # and appended no directive. The narrator saw "strike CritFail" and
        # narrated a vivid player DEFEAT while the engine had (correctly)
        # resolved player_victory via the Shock chip. Mirror the reprisal
        # close: stamp pending_resolution_signal (renders the [ENCOUNTER
        # RESOLVED] zone) + a MECHANICAL TRUTH directive, with an explicit
        # Shock attribution when the kill landed on a missed swing so the
        # prose renders the actual mechanism.
        from sidequest.server.narration_apply import _build_resolution_signal

        snapshot.pending_resolution_signal = _build_resolution_signal(encounter)
        _shock_rider = ""
        if (
            shock_hp_removed > 0
            and encounter.outcome == "player_victory"
            and outcome_tier in (RollOutcome.Fail, RollOutcome.CritFail)
        ):
            _shock_rider = (
                f" The killing damage came from weapon Shock on a MISSED swing: "
                f"{character_name}'s attack failed, but the blade's pressure "
                f"still removed the final {shock_hp_removed} HP. Narrate the "
                "kill that way — the swing goes wide yet the opponent falls. "
                "Do NOT narrate the missed swing as a player defeat."
            )
        snapshot.next_turn_directives.append(
            f"MECHANICAL TRUTH (weave into the narration): the "
            f"{encounter.encounter_type} confrontation has RESOLVED — outcome: "
            f"{encounter.outcome}. Narrate the close of the engagement; do NOT "
            f"continue narrating it as a live, ongoing fight.{_shock_rider}"
        )
    elif strike_hp_removed + shock_hp_removed > 0:
        # Kill-overclaim anchor (evropi 2/14 + barsoom 3/10, 2026-06-10): a
        # damaging player hit that does NOT end the fight invites kill prose —
        # twice this playtest the narrator rendered an unambiguous death for
        # an Other the engine correctly kept alive, because the replay text
        # carries the beat/outcome but never the target's resulting HP. Anchor
        # the real pool + aliveness so the prose cannot overclaim. Mirrors the
        # reprisal hit/miss anchors; skipped when the target core is
        # unresolvable or already at 0 (a 0-HP unresolved state is a
        # multi-combatant partial down — "still standing" would be false).
        _anchor_target = _opposite_side_first_actor(encounter, actor_side)
        _anchor_core = (
            snapshot.find_creature_core(_anchor_target) if _anchor_target is not None else None
        )
        if _anchor_core is not None and _anchor_core.hp.current > 0:
            _missed_shock_only = (
                strike_hp_removed == 0
                and shock_hp_removed > 0
                and outcome_tier in (RollOutcome.Fail, RollOutcome.CritFail)
            )
            if _missed_shock_only:
                # WWN Shock on a MISSED to-hit (sq-playtest 2026-06-27, story
                # 158-44): the swing went wide but the weapon's Shock still
                # chipped HP. The player-side twin of the opponent-reprisal shock
                # directive (the "missed … but its Shock still chipped" hint in
                # _resolve_opponent_reprisal) — without it the narrator sees
                # Roll=Fail + a silent HP tick and renders a clean "miss" over
                # real damage, invisible to mechanics-first players. Reconcile the
                # miss explicitly so the "dealt damage" and "missed" signals do not
                # contradict and default the prose back to a clean whiff.
                snapshot.next_turn_directives.append(
                    f"MECHANICAL TRUTH (weave into the narration): {character_name}'s "
                    f"{beat.label} MISSED the to-hit, but its Shock still chipped "
                    f"{shock_hp_removed} damage — {_anchor_target} is now at "
                    f"{_anchor_core.hp.current}/{_anchor_core.hp.max} HP and STILL "
                    "STANDING; the fight continues. Narrate the graze that drew "
                    "blood as the swing went wide (the blade's edge/pressure caught "
                    "them) — do NOT narrate a clean miss, and do NOT describe their "
                    "death, collapse, or incapacitation."
                )
            else:
                snapshot.next_turn_directives.append(
                    f"MECHANICAL TRUTH (weave into the narration): {character_name}'s "
                    f"{beat.label} dealt {strike_hp_removed + shock_hp_removed} damage "
                    f"to {_anchor_target} — {_anchor_target} is at "
                    f"{_anchor_core.hp.current}/{_anchor_core.hp.max} HP and STILL "
                    "STANDING; the fight continues. Narrate a wound, not a kill — do "
                    "NOT describe their death, collapse, or incapacitation."
                )
    elif win_condition == "hp_depletion":
        # Failed-strike anchor (sq-playtest 2026-06-13, beneath_sunden round 8):
        # a 0-damage player beat that does NOT resolve the fight (Fail/CritFail,
        # a Tie, or a hit that ablated nothing) invites KILL prose exactly like
        # the damaging-hit case above — the replay text carries the beat +
        # outcome_tier but never the target's surviving HP, so the narrator
        # rendered "the spear buries itself through its chest … the passage goes
        # quiet" on a tier=Fail / opponent_hp_removed=0 strike against an Other
        # the engine kept alive at 7/10. The damaging-hit branch only fires when
        # HP actually moved; this branch covers the SYMMETRIC miss/whiff so a
        # failed swing cannot be narrated as a kill. Gated to hp_depletion so the
        # HP-framed wording never reaches a dial confrontation, where a
        # 0-strike-damage beat is the norm, not a missed blow.
        _anchor_target = _opposite_side_first_actor(encounter, actor_side)
        _anchor_core = (
            snapshot.find_creature_core(_anchor_target) if _anchor_target is not None else None
        )
        if _anchor_core is not None and _anchor_core.hp.current > 0:
            _tier = outcome_tier.value if hasattr(outcome_tier, "value") else str(outcome_tier)
            _missed = outcome_tier in (RollOutcome.Fail, RollOutcome.CritFail)
            _verb = "MISSED — the blow did not connect" if _missed else "landed no damage"
            snapshot.next_turn_directives.append(
                f"MECHANICAL TRUTH (weave into the narration): {character_name}'s "
                f"{beat.label} {_verb} (outcome: {_tier}); 0 damage dealt to "
                f"{_anchor_target}, who is UNHARMED at {_anchor_core.hp.current}/"
                f"{_anchor_core.hp.max} HP and STILL STANDING; the fight continues. "
                "Narrate the failed attack — do NOT describe a hit, a wound, a "
                "killing thrust, their death, collapse, or incapacitation."
            )


# WWN SRD §2.4.4 — Total Defense grants +2 Melee & Ranged AC (verbatim, never invented).
_TOTAL_DEFENSE_AC_BONUS = 2


def _defensive_posture_for_reprisal(
    defender_commit: WnSealedCommit | None,
) -> tuple[str, int, bool]:
    """Read the reprisal target's sealed WWN defensive action into an AC posture
    (story 152-1, WWN SRD §2.4.4).

    Returns ``(defender_beat_id, ac_bonus, shock_immune)``:
    - ``defender_beat_id`` — the target's committed defensive action id
      (``"total_defense"``), else ``""`` (the GM-panel span label).
    - ``ac_bonus`` — the Melee/Ranged AC bonus the opponent's to-hit must clear:
      ``+2`` for a committed **Total Defense** (SRD §2.4.4), else 0.
    - ``shock_immune`` — True for Total Defense (immune to Shock until the start of
      the defender's next turn), else False.

    WWN defense is **Armor Class manipulation**, not damage mitigation: the native
    ``brace`` flat-HP mitigation and ``break_contact`` whole-attack prevention are
    REMOVED from the WN path (ADR-143 — "Bind the Ruleset, Don't Balance It"). Any
    other commit (attack, run, fighting_withdrawal, item-use, or ``None`` — the
    legacy SWN path) grants no defensive posture; the opponent's attack resolves
    against the unmodified AC.
    """
    if defender_commit is None:
        return "", 0, False
    if defender_commit.beat_id == WN_TOTAL_DEFENSE_BEAT_ID:
        return WN_TOTAL_DEFENSE_BEAT_ID, _TOTAL_DEFENSE_AC_BONUS, True
    return "", 0, False


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
    attacker_name: str | None = None,
    defender_commit: WnSealedCommit | None = None,
    source: str = "opponent_reprisal",
) -> list[object]:
    """Server-driven opponent attack turn (story 71-21, SWN hp_depletion combat).

    ``source`` labels the attack on the GM-panel span: ``"opponent_reprisal"`` for
    the opponent's own-turn slot attack, ``"opportunity_attack"`` for the free
    attack a plain Run out of melee provokes (story 152-1, WWN SRD §2.4.4).

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

    # Story 102-4: the WN round walk attacks AS the slot token (each seated
    # opponent acts at its own initiative slot); legacy callers keep the
    # first-Other resolution.
    opponent_name = attacker_name or _opposite_side_first_actor(encounter, "player")
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
        # Story 152-1 (ADR-143): the WN engine OWNS the action set, so under a WN
        # binding the opponent's strike is SYNTHESIZED too — 108-3 strips the native
        # combat beats to [] (``cdef.beats == []``), and the opponent must still
        # attack ONCE on its slot vs the defender's AC. Mirrors the player-side
        # ``wn_action_beat`` synthesis; the to-hit terms come from the synthesized
        # strike and the damage from ``cdef.opponent_damage`` (resolved below).
        if isinstance(ruleset, WithoutNumberRulesetModule):
            opponent_beat = wn_action_beat(WN_ATTACK_BEAT_ID)
        else:
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

    # Story 152-1 (ADR-143, WWN SRD §2.4.4): the reprisal reads the target's sealed
    # WWN defensive action. A committed Total Defense raises the AC the opponent's
    # to-hit must clear (+2) and grants Shock immunity — WWN defense is Armor Class,
    # so a connecting hit still deals FULL damage (no flat mitigation) and a defense
    # never PREVENTS the whole attack (the native brace/break_contact scaffolding is
    # REMOVED — "Bind the Ruleset, Don't Balance It"). The span proves the defense
    # fired (GM-panel lie detector; the boosted AC + ac_delta are mechanically backed).
    defender_beat_id, defense_ac_bonus, defense_shock_immune = _defensive_posture_for_reprisal(
        defender_commit
    )
    target_ac += defense_ac_bonus

    d20 = rng.randint(1, 20)
    outcome = ruleset.resolve_opponent_attack(
        attacker_stats=opponent_stats,
        stat_check=opponent_beat.stat_check,
        attack_bonus=int(getattr(opponent_beat, "attack_bonus", 0) or 0),
        combat_skill=int(getattr(opponent_beat, "combat_skill", 0) or 0),
        target_ac=target_ac,
        d20=d20,
    )

    # Lie-detector: the to-hit decision, every attempt (hit or miss). The defensive
    # fields (story 152-1) carry the target's committed WWN action and the AC delta
    # applied (``ac_delta`` — +2 under Total Defense) so a reviewer can confirm in
    # OTEL that the defense raised the AC the enemy's roll had to clear. ``source``
    # distinguishes the own-turn slot attack from a free opportunity attack on a flee.
    with encounter_opponent_attack_resolved_span(
        encounter_type=encounter.encounter_type,
        attacker=opponent_name,
        target=player_name,
        d20=outcome.d20,
        modifier=outcome.modifier,
        attack_total=outcome.attack_total,
        target_ac=outcome.target_ac,
        hit=outcome.hit,
        defender_beat=defender_beat_id,
        ac_delta=defense_ac_bonus,
        source=source,
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
            "defender_beat": defender_beat_id,
            "ac_delta": defense_ac_bonus,
            "source": source,
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
        # WN Shock seam (story 102-1): the signature CWN/WWN melee rule — a
        # weapon with a Shock rating chips fixed damage even on a MISS vs a
        # target whose Melee AC is at or under the weapon's shock_ac ceiling.
        # The strike path already applies this player→opponent (the Fail/
        # CritFail block in dispatch_dice_throw); the opponent's reprisal must
        # chip the PC the same way. No-op for native/swn (base resolve_shock
        # returns 0) and for a spec-less opponent (weaponless seeded mook with
        # no authored opponent_damage).
        shock_opponent_core = snapshot.find_creature_core(opponent_name)
        shock_spec = cdef.opponent_damage or ruleset.resolve_damage(
            beat=opponent_beat,
            actor_core=shock_opponent_core,
            pack=pack,
            world_slug=snapshot.world_slug,
        )
        # Story 152-1 (WWN SRD §2.4.4): Total Defense grants Shock immunity — the
        # guaranteed-graze chip is suppressed ENTIRELY, not merely dodged by the +2
        # AC (the weapon's shock_ac ceiling can exceed even the boosted AC, so the
        # immunity must be explicit, not an AC side effect).
        chip = (
            ruleset.resolve_shock(
                spec=shock_spec,
                target_melee_ac=int(player_core.armor_class),
                actor=opponent_name,
            )
            if (shock_spec is not None and not defense_shock_immune)
            else 0
        )
        if chip <= 0:
            # CLEAN miss (no Shock chip). barsoom playtest 2026-06-10: the
            # silent miss path let the narrator fabricate a hit, damage, and a
            # precise FALSE HP value ("One hit point left" while the engine
            # had the PC at 4/10). The narrator never sees the server-rolled
            # reprisal (the dice messages go to the table, not the prompt) —
            # anchor the miss AND the unchanged HP explicitly, mirroring the
            # hit directive below. INFO line completes the hit/miss text-log
            # forensics pair. (A shock-chipped miss takes the branch below,
            # whose directive anchors the chip + real HP instead.)
            logger.info(
                "dice.opponent_reprisal_miss attacker=%s target=%s beat=%s "
                "attack_total=%d target_ac=%d hp_unchanged=%s/%s",
                opponent_name,
                player_name,
                opponent_beat.id,
                outcome.attack_total,
                outcome.target_ac,
                player_core.hp.current,
                player_core.hp.max,
            )
            snapshot.next_turn_directives.append(
                f"MECHANICAL TRUTH (weave into the narration): {opponent_name}'s "
                f"{opponent_beat.label} MISSED {player_name} — no damage landed; "
                f"{player_name} remains at "
                f"{player_core.hp.current}/{player_core.hp.max} HP. "
                "Narrate the attack failing to connect; do NOT narrate it landing, "
                "do NOT invent damage, and do NOT state any other HP value."
            )
            return messages
        # Story 106-2: a committed Brace mitigates the DIRECT HIT (below), but NOT
        # the Shock chip. Shock is the WWN "even a miss draws blood" guaranteed-graze
        # mechanic — a fixed chip that bypasses the to-hit roll entirely; a Brace
        # (which blunts a connecting blow) does not negate the graze. A successful
        # Break Contact prevents the whole attack earlier (the prevention return
        # above), so no Shock fires on a full disengage. Intentional asymmetry, not
        # an oversight — target_mitigation stays 0 here.
        apply_beat_hp_channel(
            target=player_core,
            channel="strike",
            damage_total=chip,
            target_mitigation=0,
            source_beat_id=f"{opponent_beat.id}:opponent_shock",
        )
        logger.info(
            "dice.opponent_reprisal_shock attacker=%s target=%s beat=%s chip=%s hp_after=%s/%s",
            opponent_name,
            player_name,
            opponent_beat.id,
            chip,
            player_core.hp.current,
            player_core.hp.max,
        )
        # The narrator never sees server-applied chip damage (same channel gap
        # as the hit path below) — without this directive the prose narrates a
        # clean miss while the engine already drew blood.
        snapshot.next_turn_directives.append(
            f"MECHANICAL TRUTH (weave into the narration): {opponent_name}'s "
            f"{opponent_beat.label} missed {player_name}, but its Shock still "
            f"chipped {chip} damage — {player_name} is now at "
            f"{player_core.hp.current}/{player_core.hp.max} HP. Narrate the "
            "graze; do not soften or omit it."
        )
        # A chip can kill: run the SAME depletion close as the hit path.
        messages.extend(
            _close_reprisal_depletion(
                encounter=encounter,
                cdef=cdef,
                ruleset=ruleset,
                pack=pack,
                snapshot=snapshot,
                player_name=player_name,
                player_core=player_core,
                beat_id=f"{opponent_beat.id}:opponent_shock",
                round_number=round_number,
                rng=rng,
            )
        )
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
        beat=opponent_beat, actor_core=opponent_core, pack=pack, world_slug=snapshot.world_slug
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

    # WWN damage is gated by AC (the to-hit roll), not reduced after the fact.
    # Story 152-1 (ADR-143): WWN defense is Armor Class — a Total Defense that fails
    # to make the attack MISS (the d20 cleared even the +2 AC) takes the FULL weapon
    # damage; there is NO flat post-hit mitigation (the native ``brace`` reduction is
    # removed — "Bind the Ruleset, Don't Balance It"). ``target_mitigation`` stays 0,
    # in parity with the player-side strike channel.
    applied_damage = apply_beat_hp_channel(
        target=player_core,
        channel="strike",
        damage_total=dmg_total,
        target_mitigation=0,
        source_beat_id=f"{opponent_beat.id}:opponent_attack",
    )
    # Text-log forensics line (sq-playtest 2026-06-07 silent death-spiral): the
    # success path was span/watcher-only — a log grep on the dead session saw
    # NOTHING for a reprisal that ablated a PC. WARNING-on-skip already existed;
    # INFO-on-hit completes the pair. ``damage`` is the NET applied HP (after a
    # Brace's mitigation), not the raw roll — the log must not overstate the hit.
    logger.info(
        "dice.opponent_reprisal_hit attacker=%s target=%s beat=%s damage=%s "
        "rolled=%s hp_after=%s/%s",
        opponent_name,
        player_name,
        opponent_beat.id,
        applied_damage,
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
        f"{opponent_beat.label} struck {player_name} for {applied_damage} damage — "
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
    # player can actually lose the fight (shared close with the miss-Shock
    # branch above; emits encounter.resolved source=hp_depletion).
    messages.extend(
        _close_reprisal_depletion(
            encounter=encounter,
            cdef=cdef,
            ruleset=ruleset,
            pack=pack,
            snapshot=snapshot,
            player_name=player_name,
            player_core=player_core,
            beat_id=f"{opponent_beat.id}:opponent_attack",
            round_number=round_number,
            rng=rng,
        )
    )

    return messages


def _close_reprisal_depletion(
    *,
    encounter: StructuredEncounter,
    cdef: ConfrontationDef,
    ruleset: RulesetModule,
    pack: GenrePack,
    snapshot: GameSnapshot,
    player_name: str,
    player_core,
    beat_id: str,
    round_number: int,
    rng: random.Random,
) -> list[object]:
    """Shared reprisal close: resolve hp_depletion against the player and apply
    every consequence of the resolution. Called by BOTH reprisal damage channels
    (the hit's rolled damage and the miss's Shock chip) so the close cannot
    drift between them. Idempotent / no-op-safe above 0 HP.

    Order matters (story 102-1): the genre lethality verdict applies FIRST —
    a non-lethal verdict recovers the PC to the 1-HP floor, which the WN downed
    seam's own hp>0 gate then reads as "not down", so a recovering PC never
    gets a Mortal Injury death clock (Genre Truth: the genre's verdict outranks
    the module's lethality table). On a LETHAL verdict the PC stays at 0 and
    ``run_cwn_wwn_downed_seam`` (actor_side="opponent" → the downed defender is
    the PLAYER side) runs the same Mortal/Major Injury stack the strike path
    runs for a dropped opponent — emitting ``{ruleset}.mortal_injury.declared``
    (and, on a traumatic-scene failed save, ``{ruleset}.major_injury.roll``),
    the AC5b combat-half lie-detector.

    Returns the player-facing messages the caller must broadcast (the
    CHARACTER_INCAPACITATED death surface for each PC taken out).
    """
    messages: list[object] = []
    depletion = check_hp_depletion(
        encounter,
        snapshot.find_creature_core,
        beat_id=beat_id,
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
    from sidequest.server.post_resolution_lethality import (
        apply_post_resolution_lethality,
        build_incapacitated_message,
    )

    incapacitations = apply_post_resolution_lethality(
        snapshot=snapshot, encounter=encounter, pack=pack, turn=round_number
    )
    # Story 102-1 (90-3 AC5b combat half): a PC left DOWN by a LETHAL verdict
    # must run the same WN Mortal/Major Injury stack the strike path runs for a
    # dropped opponent — otherwise a dying PC emits only the generic verdict
    # with zero {ruleset}.* spans and the GM panel cannot show WN lethality
    # engaged. Runs AFTER the verdict so a non-lethal recovery (PC back at the
    # 1-HP floor) gates the seam off via its own hp>0 check; the seam's config
    # capability gate keeps native/swn silent. actor_side="opponent" resolves
    # the downed defender as the PLAYER side.
    run_cwn_wwn_downed_seam(
        ruleset=ruleset,
        snapshot=snapshot,
        encounter=encounter,
        cdef=cdef,
        pack=pack,
        actor_side="opponent",
        rng=rng,
    )
    # sq-playtest 2026-06-07 (barsoom-3, blocking): surface the death AT the
    # moment it happens. The turn-intake gate (handlers.player_action) is the
    # durable lock for the dead PC's SUBSEQUENT actions, but the kill turn itself
    # must tell the UI so the seat locks and the death banner / re-roll CTA
    # appears now — not only after the player tries (and fails) to act again.
    for event in incapacitations:
        messages.append(
            build_incapacitated_message(
                character_name=event.actor,
                verdict=event.verdict,
                status_text=event.status_text,
                player_id="server",
            )
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
