"""Fate Core conflict exchange (ADR-144 F1c) — mirrors wn_round.py one tier over.

A Fate confrontation rides the SAME MP substrate as the WN sealed round: the
ADR-036 submit-and-wait barrier, expressed as a per-encounter sealed-commit
ledger (``encounter.fate_commits``, sibling to ``wn_commits``). Every seated PC
seals one proactive action; when the last commit arrives the exchange walks the
committed actors in Notice (physical) / Empathy (mental) order and resolves each
action through the F1a resolution primitive + the F1b stress/consequence
mutators. Defense is reactive — the engine rolls it for an attack's target.
Opponent actions are committed by the F2 narrator (F1c authors no opponent AI);
the barrier waits on PCs only (mirrors WN: engine-driven actors don't seal).

Every decision emits a ``fate.*`` OTEL span (the GM-panel lie detector). Native
and WN dispatch are untouched — this is a parallel engine selected by dispatch
(F1d) via ``isinstance(module, FateRulesetModule)``.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import TYPE_CHECKING

from opentelemetry import trace

from sidequest.game.encounter import (
    EncounterActor,
    EncounterPhase,
    FateAction,
    FateSealedCommit,
    StructuredEncounter,
)
from sidequest.game.fate_opponent import decide_opponent_action
from sidequest.game.fate_sheet import Aspect, FateSheet, StressTrackName
from sidequest.game.ruleset.base import RulesetModule
from sidequest.game.ruleset.fate import FateRulesetModule
from sidequest.game.ruleset.fate_resolution import FateOutcome, Opposition
from sidequest.game.session import GameSnapshot
from sidequest.protocol.fate import FateActionPayload
from sidequest.protocol.sanitize import sanitize_player_text
from sidequest.telemetry.spans import (
    fate_aspect_created_span,
    fate_conceded_span,
    fate_exchange_committed_span,
    fate_exchange_order_span,
    fate_exchange_resolved_span,
    fate_flavor_rider_span,
    fate_harm_routed_span,
    fate_opponent_decided_span,
    fate_taken_out_span,
)
from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

if TYPE_CHECKING:
    # fate_contest imports from fate_conflict at module level — use TYPE_CHECKING
    # to break the cycle. At runtime, FateContestResult is imported lazily inside
    # the barrier-close branch (Step 5). This guard keeps pyright happy on the
    # widened FateDispatchResult.exchange type without introducing a circular import.
    from sidequest.server.dispatch.fate_contest import FateContestResult

logger = logging.getLogger(__name__)

#: SRD defense skills, by conflict track. Refinable per genre as content (F4).
_DEFENSE_SKILL = {"physical": "Athletics", "mental": "Will"}
#: SRD initiative skills, by conflict track.
_ORDER_SKILL = {"physical": "Notice", "mental": "Empathy"}


class FateConflictError(ValueError):
    """A Fate exchange could not be resolved (double commit, attack with no
    target, a target without a Fate sheet, ...). Fail loud — No Silent
    Fallbacks (ADR-144 / SOUL.md)."""


def seal_fate_commit(
    *,
    encounter: StructuredEncounter,
    actor: EncounterActor,
    action: FateAction,
    skill: str,
    target: str | None = None,
    difficulty: int = 0,
    ladder_total: int = 0,
    dice: tuple[int, int, int, int] = (0, 0, 0, 0),
    aspect_text: str = "",
) -> None:
    """Seal one participant's proactive action onto the exchange ledger.

    Mirrors ``seal_wn_commit``. A double commit in the same exchange is a client
    bug, rejected loudly: Fate action economy is one proactive action per actor
    per exchange.
    """
    if any(c.actor == actor.name for c in encounter.fate_commits):
        raise FateConflictError(
            f"{actor.name!r} has already committed an action this exchange — "
            "the Fate exchange seals one action per participant"
        )
    encounter.fate_commits.append(
        FateSealedCommit(
            actor=actor.name,
            action=action,  # typed to the three-action Literal; pydantic also validates at runtime
            skill=skill,
            target=target,
            difficulty=difficulty,
            ladder_total=ladder_total,
            dice=dice,
            aspect_text=aspect_text,
        )
    )
    _watcher_publish(
        "state_transition",
        {
            "field": "encounter",
            "op": "fate_commit_sealed",
            "actor": actor.name,
            "action": action,
            "target": target or "",
            "committed_actors": ", ".join(c.actor for c in encounter.fate_commits),
            "source": "fate_action",
        },
        component="encounter",
    )


def _seated_pc_names(snapshot: GameSnapshot) -> set[str]:
    """PC names (``snapshot.characters``). Mirrors wn_round: only human-controlled
    PCs seal an action and hold the barrier; engine-driven NPC allies/opponents
    (in ``snapshot.npcs``) never seal."""
    return {ch.core.name for ch in snapshot.characters}


def fate_waiting_actors(*, encounter: StructuredEncounter, snapshot: GameSnapshot) -> list[str]:
    """Player-side PCs the barrier is still waiting on. Mirrors
    ``wn_waiting_actors``: skip withdrawn, already-committed, and non-PC (ally)
    actors."""
    pc_names = _seated_pc_names(snapshot)
    committed = {c.actor for c in encounter.fate_commits}
    waiting: list[str] = []
    for a in encounter.actors:
        if a.side != "player" or a.withdrawn or a.name in committed:
            continue
        if a.name not in pc_names:
            continue  # engine-driven ally — never seals
        waiting.append(a.name)
    return waiting


def fate_barrier_closed(*, encounter: StructuredEncounter, snapshot: GameSnapshot) -> bool:
    """True when every live, seated player-side PC has committed an action."""
    return not fate_waiting_actors(encounter=encounter, snapshot=snapshot)


def fate_turn_order(
    *, encounter: StructuredEncounter, snapshot: GameSnapshot, mental: bool
) -> list[str]:
    """Seated, non-withdrawn participant names ordered by the conflict's
    initiative skill (Notice physical / Empathy mental), highest first. Ties keep
    seating order (Python's sort is stable)."""
    skill = _ORDER_SKILL["mental" if mental else "physical"]

    def rating(name: str) -> int:
        core = snapshot.find_creature_core(name)
        sheet = core.fate_sheet if core is not None else None
        return sheet.skills.get(skill, 0) if sheet is not None else 0

    seated = [a for a in encounter.actors if not a.withdrawn and a.side in ("player", "opponent")]
    return [a.name for a in sorted(seated, key=lambda a: rating(a.name), reverse=True)]


def absorb_shifts(
    *,
    module: FateRulesetModule,
    sheet: FateSheet,
    track: StressTrackName,
    shifts: int,
    actor: str,
    source: str,
    _tracer: trace.Tracer | None = None,
) -> bool:
    """Absorb a ``shifts``-shift hit into ``track`` stress + consequences.

    SRD: a hit may be absorbed by AT MOST ONE stress box plus any number of
    consequence slots. Prefer the smallest single stress box that covers the
    whole hit; otherwise spend the largest available box to shave the hit and
    fill consequence slots smallest-first for the remainder. Returns ``True`` if
    fully absorbed (actor survives), ``False`` if capacity is exhausted (the
    caller takes them out). Delegates the atomic marks to the F1b mutators so
    each emits its own ``fate.stress.applied`` / ``fate.consequence.taken`` span.

    Precondition: ``shifts`` MUST be >= 1 — ties and misses are resolved by the
    caller before absorption is ever reached (a zero-or-negative hit, or an
    unknown track, fails loud per No Silent Fallbacks).
    """
    if shifts <= 0:
        raise FateConflictError(
            f"absorb_shifts requires shifts >= 1 (got {shifts}); "
            "ties and misses are handled by the caller before absorption"
        )
    if track not in sheet.stress:
        raise FateConflictError(
            f"absorb_shifts: {actor!r} has no {track!r} stress track (have: {sorted(sheet.stress)})"
        )
    remaining = shifts
    stress_track = sheet.stress[track]

    # One stress box (SRD: one per hit). Smallest box that alone covers the hit;
    # else the largest available box to shave it.
    covering = [b for b in stress_track.boxes if not b.checked and b.value >= remaining]
    chosen = (
        min(covering, key=lambda b: b.value)
        if covering
        else max(
            (b for b in stress_track.boxes if not b.checked),
            key=lambda b: b.value,
            default=None,
        )
    )
    if chosen is not None:
        module.mark_stress(
            sheet=sheet, track=track, box_value=chosen.value, actor=actor, _tracer=_tracer
        )
        remaining = max(0, remaining - chosen.value)

    # Consequences, smallest-first, until the hit is covered.
    for slot in sorted((c for c in sheet.consequences if c.aspect is None), key=lambda c: c.value):
        if remaining <= 0:
            break
        module.take_consequence(
            sheet=sheet,
            level=slot.level,
            aspect_text=f"{slot.level.title()} consequence inflicted by {source}",
            actor=actor,
            _tracer=_tracer,
        )
        remaining = max(0, remaining - slot.value)

    return remaining <= 0


@dataclass(frozen=True)
class FateExchangeResult:
    """What one exchange walk produced. ``resolution_order`` is the comma-joined
    token sequence walked (already on the ``fate.exchange.resolved`` span);
    ``narrator_hints`` are the mechanical-truth lines for the F2 narrator."""

    resolution_order: str
    resolved: bool
    narrator_hints: list[str]


def _roll_defense(
    *,
    ruleset: FateRulesetModule,
    snapshot: GameSnapshot,
    defender: str,
    mental: bool,
    rng: random.Random,
    _tracer: trace.Tracer | None = None,
) -> int:
    """Roll the target's reactive defense (4dF + defense skill) and return the
    ladder total. Emits ``fate.action_resolved`` for the defense roll (the GM
    panel sees the defender's number)."""
    core = snapshot.find_creature_core(defender)
    sheet = core.fate_sheet if core is not None else None
    skill = _DEFENSE_SKILL["mental" if mental else "physical"]
    rating = sheet.skills.get(skill, 0) if sheet is not None else 0
    outcome = ruleset.resolve_action(
        skill_rating=rating,
        opposition=Opposition(value=0, kind="active"),
        rng=rng,
        actor=defender,
        _tracer=_tracer,
    )
    return outcome.ladder_total


def _maybe_resolve_side_cleared(encounter: StructuredEncounter) -> None:
    """End the confrontation when one side is wholly withdrawn (ADR-116/-139).
    Uses the encounter's documented outcome labels: ``opponent_yielded`` (player
    victory) / ``yielded`` (player loss)."""
    if encounter.resolved:
        return
    players = [a for a in encounter.actors if a.side == "player"]
    opponents = [a for a in encounter.actors if a.side == "opponent"]
    if opponents and all(a.withdrawn for a in opponents):
        encounter.resolved = True
        encounter.outcome = "opponent_yielded"
        encounter.structured_phase = EncounterPhase.Resolution
    elif players and all(a.withdrawn for a in players):
        encounter.resolved = True
        encounter.outcome = "yielded"
        encounter.structured_phase = EncounterPhase.Resolution


def _seat_opponent_commits(
    *,
    encounter: StructuredEncounter,
    snapshot: GameSnapshot,
    ruleset: FateRulesetModule,
    rng: random.Random,
    mental: bool,
    _tracer: trace.Tracer | None = None,
) -> None:
    """Seat a proactive attack commit for every live opponent that lacks one
    (ADR-144 F2d). Runs at the TOP of the exchange so the committed span and the
    ``commits`` dict include the opponent and the existing reactive walk lands the
    opponent's stress on a PC. Mirrors ``dispatch_fate_action``'s PC seal+roll
    path: ``decide_opponent_action`` chooses target+skill, then the opponent's 4dF
    is rolled and sealed now (defense stays reactive at resolution).

    A pre-committed opponent is left untouched — never double-committed
    (``seal_fate_commit`` raises on a double; we skip rather than swallow it). A
    seated opponent with no Fate sheet is an impossible state caught loud by
    ``decide_opponent_action`` one line above (No Silent Fallbacks)."""
    committed = {c.actor for c in encounter.fate_commits}
    for opp in [a for a in encounter.actors if a.side == "opponent" and not a.withdrawn]:
        if opp.name in committed:
            continue
        decision = decide_opponent_action(
            encounter=encounter, snapshot=snapshot, opponent=opp, mental=mental
        )
        if decision is None:
            continue
        core = snapshot.find_creature_core(opp.name)
        # decide_opponent_action already raised if opp had no Fate sheet; re-narrow
        # for the rating lookup (the loudness lives in decide_opponent_action).
        assert core is not None and core.fate_sheet is not None
        rating = core.fate_sheet.skills.get(decision.skill, 0)
        outcome = ruleset.resolve_action(
            skill_rating=rating,
            opposition=Opposition(value=0, kind="active"),
            rng=rng,
            actor=opp.name,
            _tracer=_tracer,
        )
        seal_fate_commit(
            encounter=encounter,
            actor=opp,
            action="attack",
            skill=decision.skill,
            target=decision.target,
            ladder_total=outcome.ladder_total,
            dice=outcome.dice,
        )
        fate_opponent_decided_span(
            actor=opp.name,
            action="attack",
            skill=decision.skill,
            target=decision.target,
            ladder_total=outcome.ladder_total,
            _tracer=_tracer,
        )


def run_fate_exchange(
    *,
    encounter: StructuredEncounter,
    snapshot: GameSnapshot,
    ruleset: FateRulesetModule,
    rng: random.Random,
    round_number: int = 0,
    _tracer: trace.Tracer | None = None,
) -> FateExchangeResult:
    """Resolve one sealed Fate exchange in Notice/Empathy order.

    Walks the committed actors; per actor resolves the committed action (overcome
    / create-advantage / attack). Attacks roll the target's defense, compute
    shifts, and absorb via stress/consequences → taken-out when capacity is
    exhausted. Emits ``fate.exchange.committed`` → ``fate.exchange.order`` →
    ``fate.exchange.resolved`` (the GM-panel polygraph). Clears the ledger —
    commits never leak into the next exchange.

    F1d passes the current interaction count as ``round_number`` so the resolved
    span and watcher payload carry it for GM-panel telemetry.
    """
    mental = encounter.category == "social"
    _seat_opponent_commits(
        encounter=encounter,
        snapshot=snapshot,
        ruleset=ruleset,
        rng=rng,
        mental=mental,
        _tracer=_tracer,
    )

    committed = ", ".join(c.actor for c in encounter.fate_commits)
    fate_exchange_committed_span(committed_actors=committed, _tracer=_tracer)

    order = fate_turn_order(encounter=encounter, snapshot=snapshot, mental=mental)
    fate_exchange_order_span(
        order=", ".join(order),
        skill=_ORDER_SKILL["mental" if mental else "physical"],
        _tracer=_tracer,
    )

    commits = {c.actor: c for c in encounter.fate_commits}
    walked: list[str] = []
    hints: list[str] = []

    for name in order:
        walked.append(name)
        actor_obj = encounter.find_actor(name)
        if actor_obj is None or actor_obj.withdrawn:
            continue
        commit = commits.get(name)
        if commit is None:
            # No proactive action sealed for this slot (e.g. an opponent the
            # narrator did not commit). Reactive defense only.
            continue
        if encounter.resolved:
            continue

        if commit.action == "attack":
            _resolve_attack(
                encounter=encounter,
                snapshot=snapshot,
                ruleset=ruleset,
                commit=commit,
                mental=mental,
                rng=rng,
                hints=hints,
                _tracer=_tracer,
            )
        elif commit.action == "create_advantage":
            _resolve_create_advantage(
                encounter=encounter,
                snapshot=snapshot,
                ruleset=ruleset,
                commit=commit,
                mental=mental,
                rng=rng,
                hints=hints,
                _tracer=_tracer,
            )
        elif commit.action == "overcome":
            _resolve_overcome(
                encounter=encounter,
                snapshot=snapshot,
                ruleset=ruleset,
                commit=commit,
                mental=mental,
                rng=rng,
                hints=hints,
                _tracer=_tracer,
            )

    encounter.fate_commits.clear()
    encounter.narrator_hints.extend(hints)
    resolution_order = ", ".join(walked)
    fate_exchange_resolved_span(
        resolution_order=resolution_order,
        resolved=encounter.resolved,
        round_number=round_number,
        _tracer=_tracer,
    )
    _watcher_publish(
        "state_transition",
        {
            "field": "encounter",
            "op": "fate_exchange_resolved",
            "resolution_order": resolution_order,
            "encounter_type": encounter.encounter_type,
            "resolved": encounter.resolved,
            "round_number": round_number,
            "source": "fate_exchange",
        },
        component="encounter",
    )
    return FateExchangeResult(
        resolution_order=resolution_order, resolved=encounter.resolved, narrator_hints=hints
    )


def _opposition_total(
    *,
    ruleset: FateRulesetModule,
    snapshot: GameSnapshot,
    commit: FateSealedCommit,
    mental: bool,
    rng: random.Random,
    _tracer: trace.Tracer | None = None,
) -> int:
    """The opposition value for a committed action: an ACTIVE target's rolled
    defense, or the PASSIVE ``difficulty``."""
    if commit.target is not None:
        return _roll_defense(
            ruleset=ruleset,
            snapshot=snapshot,
            defender=commit.target,
            mental=mental,
            rng=rng,
            _tracer=_tracer,
        )
    return commit.difficulty


def _resolve_attack(
    *,
    encounter: StructuredEncounter,
    snapshot: GameSnapshot,
    ruleset: FateRulesetModule,
    commit: FateSealedCommit,
    mental: bool,
    rng: random.Random,
    hints: list[str],
    _tracer: trace.Tracer | None = None,
) -> None:
    if commit.target is None:
        raise FateConflictError("an attack must name a target (No Silent Fallbacks)")
    target_core = snapshot.find_creature_core(commit.target)
    if target_core is None or target_core.fate_sheet is None:
        raise FateConflictError(f"attack target {commit.target!r} has no Fate sheet to defend with")
    defense_total = _roll_defense(
        ruleset=ruleset,
        snapshot=snapshot,
        defender=commit.target,
        mental=mental,
        rng=rng,
        _tracer=_tracer,
    )
    shifts = commit.ladder_total - defense_total
    track = "mental" if mental else "physical"
    if shifts <= 0:
        if shifts == 0:
            boost = Aspect(text=f"Momentum vs {commit.actor}", kind="boost", free_invokes=1)
            encounter.situation_aspects.append(boost)
            fate_aspect_created_span(
                actor=commit.target, aspect=boost.text, free_invokes=1, _tracer=_tracer
            )
            hints.append(
                f"{commit.actor}'s attack on {commit.target} tied — "
                f"{commit.target} gains a boost ({boost.text})."
            )
        else:
            hints.append(f"{commit.actor}'s attack on {commit.target} missed (shifts={shifts}).")
        return
    # Story 126-1: record the harm-routing decision (GM-panel lie detector) BEFORE
    # the marks. The hit is directed at the Fate sheet (stress/consequences), never
    # the legacy core.hp track — sink="fate_sheet" is the ADR-144 binding invariant.
    fate_harm_routed_span(
        actor=commit.target, by=commit.actor, track=track, shifts=shifts, _tracer=_tracer
    )
    survived = absorb_shifts(
        module=ruleset,
        sheet=target_core.fate_sheet,
        track=track,
        shifts=shifts,
        actor=commit.target,
        source=commit.actor,
        _tracer=_tracer,
    )
    if survived:
        hints.append(
            f"{commit.target} absorbs {commit.actor}'s {shifts}-shift hit (stress/consequences)."
        )
        return
    target_actor = encounter.find_actor(commit.target)
    if target_actor is None:
        raise FateConflictError(f"attack target {commit.target!r} is not seated in this encounter")
    target_actor.withdrawn = True
    fate_taken_out_span(actor=commit.target, by=commit.actor, shifts=shifts, _tracer=_tracer)
    hints.append(f"{commit.target} is TAKEN OUT by {commit.actor} ({shifts} unabsorbed shifts).")
    _maybe_resolve_side_cleared(encounter)


def _resolve_create_advantage(
    *,
    encounter: StructuredEncounter,
    snapshot: GameSnapshot,
    ruleset: FateRulesetModule,
    commit: FateSealedCommit,
    mental: bool,
    rng: random.Random,
    hints: list[str],
    _tracer: trace.Tracer | None = None,
) -> None:
    opposition = _opposition_total(
        ruleset=ruleset,
        snapshot=snapshot,
        commit=commit,
        mental=mental,
        rng=rng,
        _tracer=_tracer,
    )
    shifts = commit.ladder_total - opposition
    if shifts >= 1:
        free = 2 if shifts >= 3 else 1  # Succeed-with-Style → two free invokes
        aspect = Aspect(
            text=commit.aspect_text or f"Advantage by {commit.actor}",
            kind="situation",
            free_invokes=free,
        )
        encounter.situation_aspects.append(aspect)
        fate_aspect_created_span(
            actor=commit.actor, aspect=aspect.text, free_invokes=free, _tracer=_tracer
        )
        # F2c (116-4): the engine silently placed a situation aspect on success but
        # told the narrator nothing (only the failure branch below appended a hint),
        # so the advantage never reached the prose. Surface it — mirrors the
        # failure-hint style — so encounter_render.py:44-45 carries it to the prompt.
        # aspect.text is client-supplied (payload.aspect_text), and narrator_hints
        # reach the narrator prompt UNSANITIZED via render_encounter_summary — apply
        # the ADR-047 boundary here, exactly as build_fate_projection does for the
        # parallel scene_aspects path (116-4 review [HIGH][SEC]).
        hints.append(
            f"{commit.actor} created an advantage: "
            f"{sanitize_player_text(aspect.text)} ({free} free invoke(s))."
        )
    elif shifts == 0:
        boost = Aspect(
            text=commit.aspect_text or f"Fleeting Opening by {commit.actor}",
            kind="boost",
            free_invokes=1,
        )
        encounter.situation_aspects.append(boost)
        fate_aspect_created_span(
            actor=commit.actor, aspect=boost.text, free_invokes=1, _tracer=_tracer
        )
        # F2c (116-4): a tie still places a boost (1 free invoke) — surface it too.
        # Same ADR-047 sanitization as the success branch (116-4 review [HIGH][SEC]).
        hints.append(
            f"{commit.actor} created an advantage: "
            f"{sanitize_player_text(boost.text)} (1 free invoke(s))."
        )
    else:
        hints.append(f"{commit.actor}'s create-advantage failed (shifts={shifts}).")


def _resolve_overcome(
    *,
    encounter: StructuredEncounter,
    snapshot: GameSnapshot,
    ruleset: FateRulesetModule,
    commit: FateSealedCommit,
    mental: bool,
    rng: random.Random,
    hints: list[str],
    _tracer: trace.Tracer | None = None,
) -> None:
    opposition = _opposition_total(
        ruleset=ruleset,
        snapshot=snapshot,
        commit=commit,
        mental=mental,
        rng=rng,
        _tracer=_tracer,
    )
    shifts = commit.ladder_total - opposition
    if shifts >= 1:
        hints.append(f"{commit.actor} overcomes the obstacle (shifts={shifts}).")
    elif shifts == 0:
        hints.append(f"{commit.actor} overcomes at a minor cost (tie).")
    else:
        hints.append(f"{commit.actor} fails to overcome (shifts={shifts}).")


def concede_in_conflict(
    *,
    encounter: StructuredEncounter,
    snapshot: GameSnapshot,
    ruleset: FateRulesetModule,
    actor: str,
    _tracer: trace.Tracer | None = None,
) -> int:
    """Player-initiated concession (pre-roll): the actor leaves on their terms,
    withdrawing and earning 1 fate point + 1 per consequence taken this conflict
    (SRD). Returns the fate points earned. Fails loud without a Fate sheet."""
    core = snapshot.find_creature_core(actor)
    if core is None or core.fate_sheet is None:
        raise FateConflictError(f"{actor!r} has no Fate sheet to concede with")
    actor_obj = encounter.find_actor(actor)
    if actor_obj is None:
        raise FateConflictError(f"{actor!r} is not seated in this encounter — cannot concede")
    filled = sum(1 for c in core.fate_sheet.consequences if c.aspect is not None)
    earned = 1 + filled
    for _ in range(earned):
        ruleset.earn_fate_point(
            sheet=core.fate_sheet, reason="concede", actor=actor, _tracer=_tracer
        )
    actor_obj.withdrawn = True
    fate_conceded_span(actor=actor, fate_points_earned=earned, _tracer=_tracer)
    _maybe_resolve_side_cleared(encounter)
    return earned


def resolve_compel(
    *,
    action: str,
    payload: FateActionPayload,
    encounter: StructuredEncounter,
    snapshot: GameSnapshot,
    ruleset: FateRulesetModule,
    actor_name: str,
    _tracer: trace.Tracer | None = None,
) -> int:
    """Accept or refuse a narrator-offered compel (ADR-144 F3e). Pre-roll and
    non-committing (like concede). Returns the fate-point delta (+1 accept /
    -1 refuse). Fails loud if the compel was never offered, or — on refuse — if
    the actor cannot pay the declining fate point (No Silent Fallbacks)."""
    compel = encounter.find_pending_compel(target=actor_name, aspect=payload.aspect_text)
    if compel is None:
        verb = "accept" if action == "compel_accept" else "refuse"
        raise FateConflictError(
            f"{actor_name!r} has no pending compel on aspect {payload.aspect_text!r} "
            f"to {verb} — a compel must be offered before it can be resolved "
            "(No Silent Fallbacks)"
        )
    core = snapshot.find_creature_core(actor_name)
    if core is None or core.fate_sheet is None:
        raise FateConflictError(f"{actor_name!r} has no Fate sheet to resolve a compel with")
    before = core.fate_sheet.fate_points
    if action == "compel_accept":
        ruleset.accept_compel(
            sheet=core.fate_sheet, aspect_text=compel.aspect, actor=actor_name, _tracer=_tracer
        )
    else:
        # refuse_compel pays one fate point and fails loud at zero. The pending
        # compel is consumed ONLY after the spend succeeds (the line below is
        # unreached on a rejected refusal), so a 0-fate refusal leaves the compel
        # on the table for the player to (have to) accept.
        ruleset.refuse_compel(
            sheet=core.fate_sheet, aspect_text=compel.aspect, actor=actor_name, _tracer=_tracer
        )
    encounter.remove_pending_compel(compel)
    return core.fate_sheet.fate_points - before


@dataclass(frozen=True)
class FateDispatchResult:
    """What one FATE_ACTION dispatch produced. ``commitment_pending`` mirrors the
    WN ``DiceThrowOutcome.commitment_pending`` idiom: True when the action sealed
    and the barrier is still open; False when this action fired the exchange (or
    was a concession). ``exchange`` is the walk's result, or None when pending /
    conceded."""

    commitment_pending: bool
    exchange: FateExchangeResult | FateContestResult | None
    #: The acting PC's own 4dF roll (ADR-144 F3c / Story 118-3) — surfaced to the
    #: player as a FATE_ROLL the moment they act, whether or not the exchange
    #: fired. None on a concession (pre-roll, non-committing).
    action_roll: FateOutcome | None = None
    #: The fate-point delta this action applied (ADR-144 F3e): +1 on a compel
    #: accept, -1 on a compel refuse, 0 otherwise. Lets the player surface show the
    #: mechanical outcome inline (Sebastien/Jade legibility mandate).
    fate_point_delta: int = 0


def dispatch_fate_action(
    *,
    payload: FateActionPayload,
    actor_name: str,
    encounter: StructuredEncounter | None,
    ruleset: RulesetModule,
    snapshot: GameSnapshot,
    rng: random.Random,
    round_number: int = 0,
    thrown_faces: tuple[int, int, int, int] | None = None,
    _tracer: trace.Tracer | None = None,
) -> FateDispatchResult:
    """Route a player's Fate action to the exchange engine (ADR-144 F1d).

    ``thrown_faces`` (ADR-148, Story 126-7): when the player physically threw the
    4dF (arriving via ``FateThrowHandler``), the proactive action resolves from
    those settled faces — the faces ARE the roll, no ``roll_4df`` on the player
    path. ``None`` is the transitional legacy/internal path (server rolls the
    player's action); it is removed when 126-8 lands and no server-rolled player
    path remains. NPC/opponent and defense rolls (``_seat_opponent_commits`` /
    ``_roll_defense``) stay server-side regardless.

    The routing decision is ``isinstance(ruleset, FateRulesetModule)`` — exactly
    how ``dispatch_dice_throw`` gates WN combat on ``WithoutNumberRulesetModule``.
    A FATE_ACTION under a non-Fate ruleset is a config/client bug, rejected loud
    (No Silent Fallbacks). Concede is pre-roll and routes to
    ``concede_in_conflict``; the three proactive actions seal via
    ``seal_fate_commit`` and fire ``run_fate_exchange`` when the barrier closes.
    """
    if not isinstance(ruleset, FateRulesetModule):
        raise FateConflictError(
            f"FATE_ACTION dispatched under non-Fate ruleset "
            f"{type(ruleset).__name__!r}; the Fate channel is only valid for a "
            "pack bound 'ruleset: fate' (No Silent Fallbacks — ADR-144)"
        )
    if encounter is None or encounter.resolved:
        raise FateConflictError("FATE_ACTION requires an active, unresolved encounter")
    actor_obj = encounter.find_actor(actor_name)
    if actor_obj is None:
        raise FateConflictError(f"{actor_name!r} is not seated in this encounter")

    # Bind a local so pyright narrows the action Literal past the concede guard
    # (member-access narrowing would not survive the resolve_action call).
    action = payload.action

    # spec 2026-06-17 §2: a Contest has no harm — attacks are a Conflict action.
    if encounter.contest is not None and action == "attack":
        raise FateConflictError(
            "'attack' is a Conflict action; this encounter is a Contest (no stress, "
            "no consequences) — use 'overcome' (spec 2026-06-17 §2)"
        )

    # Concession is pre-roll, non-committing.
    if action == "concede":
        concede_in_conflict(
            encounter=encounter,
            snapshot=snapshot,
            ruleset=ruleset,
            actor=actor_name,
            _tracer=_tracer,
        )
        return FateDispatchResult(commitment_pending=False, exchange=None)

    # Compel accept/refuse (ADR-144 F3e) is pre-roll and non-committing, like
    # concede — it resolves the narrator's offered compel and never seals onto the
    # exchange ledger or rolls 4dF. The fate-point delta rides the result so the
    # player surface can show the +/-1 inline.
    if action in ("compel_accept", "compel_refuse"):
        delta = resolve_compel(
            action=action,
            payload=payload,
            encounter=encounter,
            snapshot=snapshot,
            ruleset=ruleset,
            actor_name=actor_name,
            _tracer=_tracer,
        )
        return FateDispatchResult(commitment_pending=False, exchange=None, fate_point_delta=delta)

    core = snapshot.find_creature_core(actor_name)
    if core is None or core.fate_sheet is None:
        raise FateConflictError(f"{actor_name!r} has no Fate sheet to act with")

    # Story 118-10 (Fate analog of 108-5): the RP-flavor rider lie-detector. When
    # the player typed freeform text alongside the action tile — the "chandelier
    # swing" riding ``payload.player_action`` — emit ``fate.action.flavor_rider``
    # proving the text was attached as narrator color ONLY and never entered the
    # 4dF resolution below (the roll is computed from skill/opposition/invoke; the
    # rider is downstream cosmetic context). Fires here, at the shared dispatch
    # engagement point reached by both the F1d explicit channel and the F2a
    # router channel — exactly where the dice path emits its rider span at beat
    # commit. ``affected_mechanics=False`` is the structural attestation, proven
    # at runtime by ``test_player_action_is_mechanically_inert``.
    if payload.player_action and payload.player_action.strip():
        fate_flavor_rider_span(actor=actor_name, affected_mechanics=False, _tracer=_tracer)
        # Story 118-6 (freeform-text-rides-the-tile): the typed flourish rides into
        # the narrator's prose as COLOR only ("the chandelier swing for free" — Rule
        # of Cool / Yes And, no mechanical advantage). It is sanitized at THIS seam
        # because ``narrator_hints`` reach the narrator prompt UNSANITIZED via
        # ``render_encounter_summary`` — the same ADR-047 boundary the 116-4
        # [HIGH][SEC] fix applies to ``aspect.text`` and the seal site applies to
        # ``payload.skill``, NOT the raw dice self-action path. The rider stays
        # mechanically inert: it is appended as a hint string and is never consulted
        # by ``resolve_action`` below (``test_player_action_is_mechanically_inert``).
        # Gate the append on the SANITIZED result, not the pre-sanitization strip:
        # an all-injection rider (e.g. ``<system></system>``) sanitizes to "" and
        # must NOT append a contentless "(flourish):" line (Reviewer 118-6 LOW).
        sanitized_rider = sanitize_player_text(payload.player_action)
        if sanitized_rider:
            encounter.narrator_hints.append(f"{actor_name} (flourish): {sanitized_rider}")

    # Optional pre-roll invoke (+2 for 'bonus', a reroll for 'reroll' — F1b). The
    # KIND is the client's ``invoke_mode`` (Story 118-10): the dispatch threads the
    # wire value through instead of hardcoding 'bonus', which is what unblocks the
    # reroll half of F3d. ``invoke_aspect`` fails loud on an unknown mode, but the
    # Literal on ``FateActionPayload.invoke_mode`` already rejects one at the wire.
    invoke_bonus = 0
    invoked_reroll = False
    if payload.invoke_aspect:
        invoke_bonus = ruleset.invoke_aspect(
            sheet=core.fate_sheet,
            aspect_text=payload.invoke_aspect,
            mode=payload.invoke_mode,
            actor=actor_name,
            _tracer=_tracer,
        )
        # Whether an invocation actually fired with mode='reroll' — the gate for the
        # reroll below. Keyed on a real invocation, never the bare wire flag, so a
        # reroll cannot happen without the free invoke / fate point it costs.
        invoked_reroll = payload.invoke_mode == "reroll"

    # All three proactive actions seal the attacker's 4dF roll now (mirrors WN
    # sealing the to-hit at commit); concede already returned above. Defense is
    # reactive — the engine rolls it for the target at resolution, never a
    # committed action (there is no full_defense — not in the Fate SRD).
    rating = core.fate_sheet.skills.get(payload.skill, 0)
    opposition = Opposition(
        value=payload.difficulty,
        kind="active" if payload.target is not None else "passive",
    )
    if thrown_faces is not None:
        # ADR-148 (Story 126-7): the player physically threw — the four settled dF
        # faces ARE the roll. Resolve from them; NEVER call roll_4df on this path.
        # Reroll semantics under determinism: the client already RE-THREW and
        # ``thrown_faces`` are the final faces, so we do NOT resolve a second time.
        # The fate-point / free-invoke accounting for the reroll already happened in
        # ``invoke_aspect`` above (the spend); ``invoke_bonus`` is 0 for a reroll, so
        # the faces resolve with no +2 — the player-visible behavior (spend a fate
        # point, get new dice) is unchanged, only the dice SOURCE moved to the client.
        outcome = ruleset.resolve_action_from_faces(
            skill_rating=rating,
            opposition=opposition,
            faces=thrown_faces,
            invoke_bonus=invoke_bonus,
            actor=actor_name,
            _tracer=_tracer,
        )
    else:
        outcome = ruleset.resolve_action(
            skill_rating=rating,
            opposition=opposition,
            rng=rng,
            invoke_bonus=invoke_bonus,
            actor=actor_name,
            _tracer=_tracer,
        )
        # Story 118-6 AC#1 (F3d reroll execution): ``invoke_aspect`` returns 0 for
        # 'reroll' — "the reroll itself is the caller's job" (fate.py:280). Perform
        # it HERE on the legacy server-rolled path: re-roll the 4dF and KEEP the new
        # outcome (SRD — a reroll REPLACES, it is not take-better). The second
        # ``resolve_action`` emits its own ``fate.action_resolved`` span, so the GM
        # panel sees the kept reroll rather than the discarded first roll — without
        # this the ``fate.aspect.invoked{mode='reroll'}`` span would report a reroll
        # the engine never performed (the Illusionism the OTEL lie-detector exists to
        # catch). ``invoke_bonus`` is 0 for a reroll, so the re-roll carries no +2.
        if invoked_reroll:
            outcome = ruleset.resolve_action(
                skill_rating=rating,
                opposition=opposition,
                rng=rng,
                invoke_bonus=invoke_bonus,
                actor=actor_name,
                _tracer=_tracer,
            )
    ladder_total, dice = outcome.ladder_total, outcome.dice

    seal_fate_commit(
        encounter=encounter,
        actor=actor_obj,
        action=action,
        # Story 118-8 / ADR-047: ``payload.skill`` is client-authored free text;
        # the rating lookup above keyed on the raw value (dict-key match), but the
        # value STORED on the commit is sanitized at the seal site to match the
        # ``aspect_text``/boost defensive posture before any future narrator hint
        # interpolates ``commit.skill``.
        skill=sanitize_player_text(payload.skill),
        # Story 118-9 / ADR-047: ``target`` and ``aspect_text`` are sealed beside
        # the now-sanitized ``skill`` (118-8) and reach the narrator UNSANITIZED via
        # ``render_encounter_summary`` — ``commit.target`` interpolates into the
        # ``_resolve_attack`` hints, and ``commit.aspect_text`` survives raw on the
        # F3a display projection. Sanitize both at the seal site for one consistent
        # defensive posture across all three sealed player-text fields, KEEPING the
        # hint-time + projection-time aspect sanitization as defense-in-depth.
        # ``target`` is ``str | None`` (None = passive action); guard the None —
        # ``sanitize_player_text(None)`` returns "", which would flip a passive
        # action into a broken active one (``_opposition_total`` treats a non-None
        # target as a real defender and rolls ``find_creature_core("")`` → raises).
        target=(sanitize_player_text(payload.target) if payload.target is not None else None),
        difficulty=payload.difficulty,
        ladder_total=ladder_total,
        dice=dice,
        aspect_text=sanitize_player_text(payload.aspect_text),
    )

    if fate_barrier_closed(encounter=encounter, snapshot=snapshot):
        if encounter.contest is not None:
            # Lazy import breaks the fate_conflict <-> fate_contest cycle.
            from sidequest.server.dispatch.fate_contest import run_fate_contest_exchange

            result = run_fate_contest_exchange(
                encounter=encounter,
                snapshot=snapshot,
                ruleset=ruleset,
                rng=rng,
                round_number=round_number,
                _tracer=_tracer,
            )
        else:
            result = run_fate_exchange(
                encounter=encounter,
                snapshot=snapshot,
                ruleset=ruleset,
                rng=rng,
                round_number=round_number,
                _tracer=_tracer,
            )
        return FateDispatchResult(commitment_pending=False, exchange=result, action_roll=outcome)
    return FateDispatchResult(commitment_pending=True, exchange=None, action_roll=outcome)
