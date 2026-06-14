"""Deterministic Fate opponent action heuristic (ADR-144 F2d).

A live Fate conflict is "half-alive" until an opponent can *act*: ``run_fate_exchange``
(``sidequest/server/dispatch/fate_conflict.py``) rolls a REACTIVE DEFENSE for an
opponent but skips any opponent slot that holds no sealed commit, so an opponent
never attacks. This module supplies the missing PROACTIVE decision — *who* an
opponent strikes and *with what skill* — as a pure, deterministic function. A later
F2d task seats/rolls/seals the decision inside the exchange; that is not this module's
job.

GAME-TIER and pure: this module never imports from ``sidequest.server.*`` and does no
dice rolling, sealing, I/O, or OTEL. ``decide_opponent_action`` is a function of state
alone. Skill-name constants are defined locally (NOT imported from fate_conflict) to
keep the game→server layering one-directional.
"""

from __future__ import annotations

from dataclasses import dataclass

from sidequest.game.encounter import EncounterActor, StructuredEncounter
from sidequest.game.fate_sheet import FateSheet
from sidequest.game.session import GameSnapshot

#: The Fate attack skills per conflict track, in canonical preference order. The
#: first entry is the track's canonical fallback (an unskilled swing still uses it).
_ATTACK_SKILLS: dict[str, list[str]] = {
    "physical": ["Fight", "Shoot"],
    "mental": ["Provoke"],
}


@dataclass(frozen=True)
class OpponentDecision:
    """An opponent's chosen proactive Fate action for this exchange.

    ``action`` is always ``"attack"`` this slice (F2d covers attacks only). ``skill``
    is the attack skill the opponent leads with; ``target`` is the player-side PC name.
    """

    action: str
    skill: str
    target: str


def _track(*, mental: bool) -> str:
    return "mental" if mental else "physical"


def _is_wounded(sheet: FateSheet, track: str) -> bool:
    """A PC is wounded on this track if it has any FILLED consequence slot OR any
    checked stress box on the conflict track."""
    if any(c.aspect is not None for c in sheet.consequences):
        return True
    stress = sheet.stress.get(track)
    return stress is not None and any(box.checked for box in stress.boxes)


def _live_player_actors(encounter: StructuredEncounter) -> list[EncounterActor]:
    """Live player-side actors in seating order (every tiebreak is this order)."""
    return [a for a in encounter.actors if a.side == "player" and not a.withdrawn]


def _threat_rating(sheet: FateSheet, track: str) -> int:
    """A PC's threat on this track, rated by the SINGLE canonical attack skill
    (physical "Fight" / mental "Provoke") — NOT the opponent's multi-skill list.
    The list ["Fight","Shoot"] governs only the opponent's own skill selection;
    PC threat-ranking is a single named skill (ADR-144 F2d spec)."""
    return sheet.skills.get(_ATTACK_SKILLS[track][0], 0)


def _select_target(
    *, encounter: StructuredEncounter, snapshot: GameSnapshot, opponent: EncounterActor, track: str
) -> str | None:
    live = _live_player_actors(encounter)
    if not live:
        return None

    # 1. Finish the most-pressured target: a live PC already wounded on this track.
    for actor in live:
        core = snapshot.find_creature_core(actor.name)
        if core is not None and core.fate_sheet is not None and _is_wounded(core.fate_sheet, track):
            return actor.name

    # 2. Retaliate against the PC who attacked this opponent this exchange.
    live_names = {a.name for a in live}
    for commit in encounter.fate_commits:
        if (
            commit.action == "attack"
            and commit.target == opponent.name
            and commit.actor in live_names
        ):
            return commit.actor

    # 3. The highest-threat live PC by the canonical attack skill; seating-order
    # tiebreak. `live` is non-empty here (the empty case returned None above), and the
    # first candidate's rating (>= 0) always beats the -1 sentinel, so `best` is always
    # assigned — this is the single return for the non-empty case.
    best = live[0]
    best_rating = -1
    for actor in live:
        core = snapshot.find_creature_core(actor.name)
        sheet = core.fate_sheet if core is not None else None
        # A player actor without a core/fate_sheet is deliberately rated threat 0 (not
        # raised) — PCs may legitimately lack a Fate facet, and a sheetless PC must not
        # crash opponent AI. This asymmetry with the sheetless-OPPONENT ValueError below
        # is intentional: a seated opponent with no FateSheet is an impossible state.
        rating = _threat_rating(sheet, track) if sheet is not None else 0
        if rating > best_rating:
            best_rating = rating
            best = actor
    return best.name


def _select_skill(opponent_sheet: FateSheet, track: str) -> str:
    """The opponent's best attack skill on this track, tiebroken by declared order;
    the track's canonical skill (at rating 0) when the opponent has none rated."""
    skills = _ATTACK_SKILLS[track]
    best_skill = skills[0]
    best_rating = -1
    for skill in skills:
        rating = opponent_sheet.skills.get(skill, 0)
        if rating > best_rating:
            best_rating = rating
            best_skill = skill
    return best_skill


def decide_opponent_action(
    *,
    encounter: StructuredEncounter,
    snapshot: GameSnapshot,
    opponent: EncounterActor,
    mental: bool,
) -> OpponentDecision | None:
    """Pick an opponent's proactive Fate action, deterministically.

    Returns an attack ``OpponentDecision``, or ``None`` when no live player-side PC
    exists (the side is cleared — do not seat a phantom attack). Raises ``ValueError``
    if the opponent has no fate_sheet/core — an impossible seated state (No Silent
    Fallbacks), matching ``_resolve_attack``'s loudness in fate_conflict.py.
    """
    track = _track(mental=mental)

    opponent_core = snapshot.find_creature_core(opponent.name)
    if opponent_core is None or opponent_core.fate_sheet is None:
        raise ValueError(
            f"Fate opponent {opponent.name!r} is seated but has no fate_sheet — "
            "impossible seated state (a Fate-bound creature must carry a FateSheet)."
        )

    target = _select_target(encounter=encounter, snapshot=snapshot, opponent=opponent, track=track)
    if target is None:
        return None

    skill = _select_skill(opponent_core.fate_sheet, track)
    return OpponentDecision(action="attack", skill=skill, target=target)
