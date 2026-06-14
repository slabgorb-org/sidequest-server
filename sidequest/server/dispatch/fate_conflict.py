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

from sidequest.game.encounter import (
    EncounterActor,
    FateAction,
    FateSealedCommit,
    StructuredEncounter,
)
from sidequest.game.session import GameSnapshot
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


def fate_waiting_actors(
    *, encounter: StructuredEncounter, snapshot: GameSnapshot
) -> list[str]:
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

    seated = [
        a for a in encounter.actors if not a.withdrawn and a.side in ("player", "opponent")
    ]
    return [a.name for a in sorted(seated, key=lambda a: rating(a.name), reverse=True)]
