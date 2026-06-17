"""fate_contest.py — the Fate Core Contest engine (ADR-144, spec 2026-06-17 §2).

Sibling to ``fate_conflict.py`` "one tier over": where a Conflict resolves harm
via stress/consequences, a Contest resolves a *goal* — opposed 4dF, first to N
victories, a tie grants a boost. No stress, no consequences. Reached through the
same FATE_ACTION channel and gated by ``isinstance(ruleset, FateRulesetModule)``;
``dispatch_fate_action`` selects this engine when ``encounter.contest is not None``
(stamped from the cdef's ``resolution_mode: contest`` at seating).

The GM panel is the lie detector: every exchange emits ``fate.contest.exchange``
and the win emits ``fate.contest.resolved`` (the OTEL Observability Principle).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from opentelemetry import trace

from sidequest.game.encounter import StructuredEncounter
from sidequest.game.fate_sheet import Aspect
from sidequest.game.ruleset.fate import FateRulesetModule
from sidequest.game.session import GameSnapshot

# Shared sealed-commit / opponent-seating substrate lives in fate_conflict; import
# the helpers (one direction only — fate_conflict imports THIS module lazily,
# inside dispatch_fate_action, to break the cycle).
from sidequest.server.dispatch.fate_conflict import (
    _seat_opponent_commits,
    _watcher_publish,
)
from sidequest.telemetry.spans import (
    fate_aspect_created_span,
    fate_contest_exchange_span,
    fate_contest_resolved_span,
)


class FateContestError(ValueError):
    """A Contest invariant was violated (e.g. run on a non-contest encounter)."""


@dataclass(frozen=True)
class FateContestResult:
    """What one contest exchange produced. Mirrors ``FateExchangeResult`` plus the
    running victory tally so the dispatch result and FateActionHandler can surface
    it (the Sebastien/Jade legibility mandate)."""

    resolution_order: str
    resolved: bool
    player_victories: int
    opponent_victories: int
    narrator_hints: list[object] = field(default_factory=list)


def run_fate_contest_exchange(
    *,
    encounter: StructuredEncounter,
    snapshot: GameSnapshot,
    ruleset: FateRulesetModule,
    rng: random.Random,
    round_number: int = 0,
    _tracer: trace.Tracer | None = None,
) -> FateContestResult:
    """Resolve one sealed Contest exchange.

    Each side's result is the best (max) ``ladder_total`` among its committed
    actors. The higher side scores 1 victory, or 2 on a 3+ margin (succeed with
    style). A tie at the top scores no victory and grants each top side a boost.
    First side to ``contest.target`` victories resolves the encounter.
    """
    contest = encounter.contest
    if contest is None:
        raise FateContestError(
            "run_fate_contest_exchange requires a contest-mode encounter "
            "(encounter.contest is None) — this is a Conflict; route to "
            "run_fate_exchange (spec 2026-06-17 §2)"
        )

    mental = encounter.category == "social"
    # The Other rolls 4dF from its seated FateSheet, exactly as in a Conflict.
    _seat_opponent_commits(
        encounter=encounter,
        snapshot=snapshot,
        ruleset=ruleset,
        rng=rng,
        mental=mental,
        _tracer=_tracer,
    )

    # Best committed total per side.
    best: dict[str, tuple[str, int]] = {}
    walked: list[str] = []
    for commit in encounter.fate_commits:
        actor = encounter.find_actor(commit.actor)
        if actor is None:
            continue
        walked.append(commit.actor)
        side = actor.side
        if side not in best or commit.ladder_total > best[side][1]:
            best[side] = (commit.actor, commit.ladder_total)

    player = best.get("player")
    opponent = best.get("opponent")
    hints: list[object] = []
    winner_side = ""
    victory_delta = 0

    if player is not None and opponent is not None:
        p_name, p_total = player
        o_name, o_total = opponent
        if p_total > o_total:
            winner_side = "player"
            victory_delta = 2 if (p_total - o_total) >= 3 else 1
            contest.player_victories += victory_delta
            hints.append(
                f"{p_name} wins the exchange ({p_total} vs {o_total}) — +{victory_delta} victory."
            )
        elif o_total > p_total:
            winner_side = "opponent"
            victory_delta = 2 if (o_total - p_total) >= 3 else 1
            contest.opponent_victories += victory_delta
            hints.append(
                f"{o_name} wins the exchange ({o_total} vs {p_total}) — +{victory_delta} victory."
            )
        else:  # tie at the top — boost to each side, no victory (SRD)
            for name in (p_name, o_name):
                boost = Aspect(text=f"Fleeting Opening by {name}", kind="boost", free_invokes=1)
                encounter.situation_aspects.append(boost)
                fate_aspect_created_span(
                    actor=name, aspect=boost.text, free_invokes=1, _tracer=_tracer
                )
            hints.append(f"Tie ({p_total}={o_total}) — no victory; each side gains a boost.")

    encounter.fate_commits.clear()
    encounter.narrator_hints.extend(str(h) for h in hints)

    if contest.player_victories >= contest.target:
        encounter.resolved = True
        encounter.outcome = "player_victory"
    elif contest.opponent_victories >= contest.target:
        encounter.resolved = True
        encounter.outcome = "opponent_victory"

    fate_contest_exchange_span(
        winner_side=winner_side,
        victory_delta=victory_delta,
        player_victories=contest.player_victories,
        opponent_victories=contest.opponent_victories,
        round_number=round_number,
        _tracer=_tracer,
    )
    if encounter.resolved:
        fate_contest_resolved_span(
            winner_side=("player" if contest.player_victories >= contest.target else "opponent"),
            player_victories=contest.player_victories,
            opponent_victories=contest.opponent_victories,
            _tracer=_tracer,
        )
    _watcher_publish(
        "state_transition",
        {
            "field": "encounter",
            "op": "fate_contest_resolved",
            "encounter_type": encounter.encounter_type,
            "winner_side": winner_side,
            "player_victories": contest.player_victories,
            "opponent_victories": contest.opponent_victories,
            "resolved": encounter.resolved,
            "round_number": round_number,
            "source": "fate_contest",
        },
        component="encounter",
    )
    return FateContestResult(
        resolution_order=", ".join(walked),
        resolved=encounter.resolved,
        player_victories=contest.player_victories,
        opponent_victories=contest.opponent_victories,
        narrator_hints=hints,
    )
