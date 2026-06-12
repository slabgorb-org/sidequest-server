"""WN sealed round — commit barrier + initiative-ordered walk (story 102-4).

The WN turn model (SWN module design §6–7): every seated player-side
participant commits a sealed Main Action; when the LAST commit arrives the
round resolves actor-by-actor in the persisted 1d8+DEX initiative order —
including each seated opponent, which acts at its OWN slot instead of as an
immediate reprisal rider on a player's dispatch. A committed action whose
target is already down when the actor's slot arrives is a **dead premise**:
the engine emits the typed signal and the NARRATOR adjudicates
redirect-or-fizzle in fiction — the engine never auto-retargets and never
acts the player (SOUL: The Test).

This rides SideQuest's existing semantics (Keith directive 2026-06-10):
the ADR-036 submit-and-wait barrier is the MP substrate (sealed
*resolution*, not hidden submission — peer action text stays visible), the
module seam owns the behavior (isinstance against ``SwnRulesetModule``;
native dispatch is untouched), and the order walked is the P4 spine's
persisted ``encounter.initiative``.

The ``dice.py`` helpers are imported inside ``run_wn_round`` (function
level) because ``dice`` imports this module inside ``dispatch_dice_throw``
— deferring both directions keeps the pair cycle-proof regardless of which
module loads first.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass

from sidequest.game.beat_kinds import _opposite_side_first_actor
from sidequest.game.encounter import EncounterActor, StructuredEncounter, WnSealedCommit
from sidequest.game.ruleset.base import RulesetModule
from sidequest.game.session import GameSnapshot
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.rules import BeatDef, ConfrontationDef
from sidequest.protocol.dice import RollOutcome
from sidequest.protocol.messages import DiceRequestMessage, DiceResultMessage
from sidequest.server.dispatch.downed_seam import DiceDispatchError
from sidequest.telemetry.spans import (
    wn_dead_premise_span,
    wn_round_committed_span,
    wn_round_initiative_span,
    wn_round_resolved_span,
)
from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

logger = logging.getLogger(__name__)


def seal_wn_commit(
    *,
    encounter: StructuredEncounter,
    actor: EncounterActor,
    beat: BeatDef,
    outcome: RollOutcome,
    spell_id: str | None,
) -> None:
    """Seal one player's Main Action onto the encounter's commit ledger.

    The premise target is pinned at commit time (the engine's current
    opposite-side primary) so the round walk can detect a dead premise
    without re-deriving — and without auto-retargeting — at the slot.
    A double commit in the same round is a client bug, rejected loudly:
    WN action economy is one Main Action per round.
    """
    if any(c.actor == actor.name for c in encounter.wn_commits):
        raise DiceDispatchError(
            f"{actor.name!r} has already committed a Main Action this round — "
            "the WN turn model seals one action per participant per round"
        )
    target = _opposite_side_first_actor(encounter, actor.side)
    encounter.wn_commits.append(
        WnSealedCommit(
            actor=actor.name,
            beat_id=beat.id,
            outcome=outcome.value,
            target=target,
            spell_id=spell_id,
        )
    )
    _watcher_publish(
        "state_transition",
        {
            "field": "encounter",
            "op": "wn_commit_sealed",
            "actor": actor.name,
            "beat_id": beat.id,
            "target": target or "",
            "committed_actors": ", ".join(c.actor for c in encounter.wn_commits),
            "source": "dice_throw",
        },
        component="encounter",
    )


def _seated_pc_names(snapshot: GameSnapshot) -> set[str]:
    """Names of the human-controlled PCs (``snapshot.characters``).

    The discriminator between a player-side participant that SEALS a Main
    Action (a PC) and one that does not (an engine-driven friendly NPC ally,
    which story 59-35 seats on side="player"). PCs live in
    ``snapshot.characters``; NPC allies live in ``snapshot.npcs`` — a
    player-side EncounterActor whose name is not a PC name is an ally.
    """
    return {ch.core.name for ch in snapshot.characters}


def wn_waiting_actors(
    *,
    encounter: StructuredEncounter,
    snapshot: GameSnapshot,
) -> list[str]:
    """Player-side PCs the commit barrier is still waiting on.

    Only HUMAN-CONTROLLED player characters seal a Main Action and hold the
    barrier. Engine-driven friendly NPC allies (the story 59-35 / SOUL
    Guitar-Solo ally seater puts them on side="player") never commit — they
    carry no SWN ability scores, get no initiative slot, and act on narrator
    beats — so counting them dangles the barrier forever (the coyote_star solo
    ship_combat deadlock, 2026-06-10: the human commits the one PC, the crew
    ally never does, the round never fires). A PC is a name in
    ``snapshot.characters``; an NPC ally is in ``snapshot.npcs`` and is exempt.

    Withdrawn and downed (0-HP) PCs cannot commit and never hold the barrier;
    a PC whose CreatureCore does not resolve is counted as waiting (we cannot
    prove they are out of the fight).
    """
    pc_names = _seated_pc_names(snapshot)
    committed = {c.actor for c in encounter.wn_commits}
    waiting: list[str] = []
    for a in encounter.actors:
        if a.side != "player" or a.withdrawn or a.name in committed:
            continue
        if a.name not in pc_names:
            # Engine-driven NPC ally — never seals a Main Action (59-35).
            continue
        core = snapshot.find_creature_core(a.name)
        if core is not None and core.hp.current <= 0:
            continue
        waiting.append(a.name)
    return waiting


def wn_barrier_exempt_allies(
    *,
    encounter: StructuredEncounter,
    snapshot: GameSnapshot,
) -> list[str]:
    """Player-side NPC allies the commit barrier does NOT wait on.

    The complement of the PC set among live, seated player-side actors: friendly
    crew the engine drives via narrator beats, not sealed Main Actions (59-35).
    Surfaced on the ``{slug}.round.committed`` span so the GM panel can see why
    the barrier closed without every player-side actor committing.
    """
    pc_names = _seated_pc_names(snapshot)
    return [
        a.name
        for a in encounter.actors
        if a.side == "player" and not a.withdrawn and a.name not in pc_names
    ]


def wn_barrier_closed(
    *,
    encounter: StructuredEncounter,
    snapshot: GameSnapshot,
) -> bool:
    """True when every live, seated player-side PC has committed."""
    return not wn_waiting_actors(encounter=encounter, snapshot=snapshot)


@dataclass(frozen=True)
class WnRoundResult:
    """What one round walk produced.

    ``messages`` is what the dispatching caller fans out to the room —
    broadcast-ready dice pairs and incapacitation surfaces in slot order.
    ``resolution_order`` is the comma-joined token sequence the walk visited;
    it is already recorded on the ``{slug}.round.resolved`` span and is
    surfaced here for callers/tests, not re-broadcast."""

    messages: list[object]
    resolution_order: str


def run_wn_round(
    *,
    encounter: StructuredEncounter,
    cdef: ConfrontationDef,
    ruleset: RulesetModule,
    pack: GenrePack,
    snapshot: GameSnapshot,
    session_id: str,
    round_number: int,
    rolling_player_id: str,
    rng: random.Random,
) -> WnRoundResult:
    """Resolve one sealed WN round in descending persisted-initiative order.

    Per slot: a 0-HP actor does not act (§6, mechanically enforced); a
    seated opponent attacks the first live player-side actor unless the
    encounter has already resolved (ADR-139 win-condition liveness); a
    player commit whose pinned target is down emits ``{slug}.dead_premise``
    + a narrator hint and does NOT resolve mechanically (no corpse damage,
    no auto-retarget). Emits ``{slug}.round.committed`` →
    ``{slug}.round.initiative`` → ``{slug}.round.resolved`` with the honest
    binding slug (awn, not cwn) — the GM-panel polygraph for the round.
    """
    # Function-level: dice.py imports this module inside dispatch, so the
    # reverse import must wait until dice's module objects exist.
    from sidequest.server.dispatch.dice import (
        _apply_committed_player_beat,
        _emit_player_beat_resolution_close,
        _resolve_opponent_reprisal,
    )

    slug = pack.rules.ruleset
    committed = ", ".join(c.actor for c in encounter.wn_commits)
    # GM-panel lie-detector: when the barrier closed with fewer commits than
    # player-side actors, the difference is the engine-driven NPC allies (59-35)
    # that never seal a Main Action — record them so the short barrier is
    # explainable, not a mystery (coyote_star solo ship_combat deadlock fix).
    exempt_allies = ", ".join(
        wn_barrier_exempt_allies(encounter=encounter, snapshot=snapshot)
    )
    wn_round_committed_span(
        slug=slug, committed_actors=committed, exempt_allies=exempt_allies
    )

    order = sorted(encounter.initiative, key=lambda e: e.value, reverse=True)
    wn_round_initiative_span(
        slug=slug,
        initiative_order=", ".join(f"{e.token_id}:{e.value}" for e in order),
    )

    commits = {c.actor: c for c in encounter.wn_commits}
    walked: list[str] = []
    messages: list[object] = []

    for entry in order:
        token = entry.token_id
        walked.append(token)
        enc_actor = encounter.find_actor(token)
        if enc_actor is None:
            logger.warning(
                "wn_round.slot_skipped reason=token_not_seated token=%s encounter=%s",
                token,
                encounter.encounter_type,
            )
            continue
        core = snapshot.find_creature_core(token)
        if core is not None and core.hp.current <= 0:
            # §6: an actor at 0 HP before its slot does not act this round.
            logger.info(
                "wn_round.slot_skipped reason=actor_downed token=%s hp=0/%s",
                token,
                core.hp.max,
            )
            _watcher_publish(
                "state_transition",
                {
                    "field": "encounter",
                    "op": "wn_slot_skipped_downed",
                    "actor": token,
                    "source": "wn_round",
                },
                component="encounter",
            )
            continue
        if enc_actor.withdrawn:
            logger.info("wn_round.slot_skipped reason=withdrawn token=%s", token)
            continue

        if enc_actor.side == "opponent":
            if encounter.resolved:
                # ADR-139 win-condition liveness: the fight is over; nobody
                # keeps swinging in a resolved confrontation.
                logger.info("wn_round.slot_skipped reason=encounter_resolved token=%s", token)
                continue
            target_name = _first_live_actor(encounter, snapshot, "player")
            if target_name is None:
                logger.warning("wn_round.slot_skipped reason=no_live_player_target token=%s", token)
                continue
            messages.extend(
                _resolve_opponent_reprisal(
                    encounter=encounter,
                    cdef=cdef,
                    ruleset=ruleset,
                    pack=pack,
                    snapshot=snapshot,
                    player_name=target_name,
                    session_id=session_id,
                    round_number=round_number,
                    rng=rng,
                    attacker_name=token,
                )
            )
            continue

        if enc_actor.side != "player":
            continue  # neutral actors never act (ADR-116 seating semantics)

        commit = commits.get(token)
        if commit is None:
            logger.warning(
                "wn_round.slot_skipped reason=no_sealed_commit token=%s "
                "(player-side actor in initiative without a commit — "
                "barrier/seating drift)",
                token,
            )
            continue

        target_core = snapshot.find_creature_core(commit.target) if commit.target else None
        if commit.target is not None and target_core is not None and target_core.hp.current <= 0:
            # Dead premise: the committed target dropped earlier in the
            # order. Engine surfaces; narrator adjudicates. NO mechanical
            # resolution, NO auto-retarget (SOUL: The Test).
            wn_dead_premise_span(slug=slug, actor=token, target=commit.target)
            encounter.narrator_hints.append(
                f"DEAD PREMISE: {token}'s committed action targeted "
                f"{commit.target}, who was already down when {token}'s "
                "initiative slot arrived. Adjudicate it in fiction — a "
                "redirect or a fizzle is the player's/narrator's call. The "
                "action did NOT resolve mechanically and hit no one else."
            )
            _watcher_publish(
                "state_transition",
                {
                    "field": "encounter",
                    "op": "wn_dead_premise",
                    "actor": token,
                    "target": commit.target,
                    "beat_id": commit.beat_id,
                    "source": "wn_round",
                },
                component="encounter",
            )
            continue

        if encounter.resolved:
            logger.info("wn_round.slot_skipped reason=encounter_resolved token=%s", token)
            continue

        beat = next((b for b in cdef.beats if b.id == commit.beat_id), None)
        if beat is None:
            raise DiceDispatchError(
                f"sealed commit names unknown beat_id {commit.beat_id!r} for "
                f"encounter {encounter.encounter_type!r} — content changed "
                "mid-round (No Silent Fallbacks)"
            )
        application = _apply_committed_player_beat(
            beat_id=commit.beat_id,
            spell_id=commit.spell_id,
            character_name=token,
            rolling_player_id=rolling_player_id,
            beat=beat,
            actor=enc_actor,
            outcome_tier=RollOutcome(commit.outcome),
            encounter=encounter,
            cdef=cdef,
            ruleset=ruleset,
            pack=pack,
            snapshot=snapshot,
            session_id=session_id,
            round_number=round_number,
        )
        if (
            application.damage_request_payload is not None
            and application.damage_result_payload is not None
        ):
            messages.append(
                DiceRequestMessage(payload=application.damage_request_payload, player_id="server")
            )
            messages.append(
                DiceResultMessage(payload=application.damage_result_payload, player_id="server")
            )
        _emit_player_beat_resolution_close(
            encounter=encounter,
            snapshot=snapshot,
            character_name=token,
            beat=beat,
            actor_side=enc_actor.side,
            outcome_tier=RollOutcome(commit.outcome),
            strike_hp_removed=application.strike_hp_removed,
            shock_hp_removed=application.shock_hp_removed,
            encounter_resolved=application.encounter_resolved,
            # Review rework r1: the walk closed this fight — the
            # encounter.resolved span and the persisted op="resolved" row
            # must say so (honest seam label for the GM panel / ADR-124).
            source="wn_round",
        )

    # The round is spent — commits never leak into the next round.
    encounter.wn_commits.clear()

    resolution_order = ", ".join(walked)
    wn_round_resolved_span(slug=slug, resolution_order=resolution_order)
    _watcher_publish(
        "state_transition",
        {
            "field": "encounter",
            "op": "wn_round_resolved",
            "resolution_order": resolution_order,
            "encounter_type": encounter.encounter_type,
            "resolved": encounter.resolved,
            "source": "wn_round",
        },
        component="encounter",
    )
    return WnRoundResult(messages=messages, resolution_order=resolution_order)


def _first_live_actor(
    encounter: StructuredEncounter, snapshot: GameSnapshot, side: str
) -> str | None:
    """First non-withdrawn actor on ``side`` whose HP pool is above 0.

    An actor with no resolvable core counts as live — the opposite would
    silently exempt unseeded fixtures from being attacked.
    """
    for a in encounter.actors:
        if a.side != side or a.withdrawn:
            continue
        core = snapshot.find_creature_core(a.name)
        if core is None or core.hp.current > 0:
            return a.name
    return None
