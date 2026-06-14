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

from opentelemetry import trace

from sidequest.game.encounter import (
    EncounterActor,
    EncounterPhase,
    FateAction,
    FateSealedCommit,
    StructuredEncounter,
)
from sidequest.game.fate_sheet import Aspect, FateSheet, StressTrackName
from sidequest.game.ruleset.fate import FateRulesetModule
from sidequest.game.ruleset.fate_resolution import Opposition
from sidequest.game.session import GameSnapshot
from sidequest.telemetry.spans import (
    fate_aspect_created_span,
    fate_conceded_span,
    fate_exchange_committed_span,
    fate_exchange_order_span,
    fate_exchange_resolved_span,
    fate_taken_out_span,
)
from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

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
    committed = ", ".join(c.actor for c in encounter.fate_commits)
    fate_exchange_committed_span(committed_actors=committed, _tracer=_tracer)

    mental = encounter.category == "social"
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
    filled = sum(1 for c in core.fate_sheet.consequences if c.aspect is not None)
    earned = 1 + filled
    for _ in range(earned):
        ruleset.earn_fate_point(
            sheet=core.fate_sheet, reason="concede", actor=actor, _tracer=_tracer
        )
    actor_obj = encounter.find_actor(actor)
    if actor_obj is not None:
        actor_obj.withdrawn = True
    fate_conceded_span(actor=actor, fate_points_earned=earned, _tracer=_tracer)
    _maybe_resolve_side_cleared(encounter)
    return earned
