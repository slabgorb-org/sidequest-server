"""Post-resolution PC-down handler — the genre lethality policy's mechanical
consequence when a confrontation resolves against the player at 0 HP.

PLAYTEST BUG (EH-2 burning_peace, 2026-06-05): after a combat DEFEAT
(``opponent_victory``) the PC is left parked at HP 0/10 with full free-input
agency — no downed flag, no recovery, no death state, and no OTEL span recording
any decision. A career GM reads the mismatch instantly: the narration had the PC
*survive* a defeat (riders ride off, she's scorched but alive) yet mechanically
she sits at 0/10 and the game just continues "What do you do?".

ROOT CAUSE: ``_resolve_opponent_reprisal`` (``server.dispatch.dice``) ablates the
PC to 0 HP and resolves the encounter via ``check_hp_depletion`` — but, unlike the
player-strike / player-cast paths which run the CWN/WWN downed seam
(``run_cwn_wwn_downed_seam``) after dropping a *defender*, the reprisal applies NO
mechanical consequence to the *player* who just went down.

This module is the single policy-driven seam. It reads the mandatory genre
``lethality_policy.verdicts_on_zero_hp.pc`` (the authoritative statement of how a
genre treats a downed PC — "Crunch in the Genre") and applies ONE mechanical
consequence per resolution:

  - NON-LETHAL verdict (``defeated`` / ``captured`` / ``humiliated`` / ``maimed``
    / ``unscathed``): a recoverable setback. Recover the PC to a floor (1 HP —
    alive, barely) and attach a "Recovering" Wound status carrying the cost. The
    PC keeps agency but HP + status reflect the near-defeat (Genre Truth: the bond
    frays but does not sever). This is the EH/`defeated`, wry_whimsy, c&c case.
  - LETHAL verdict (``dead`` / ``dying``): the PC stays at 0 HP and takes a
    "Downed" Scar status naming the verdict, flagged ``incapacitating=True``.
    That marker is now ENFORCED at turn intake: ``handlers.player_action`` reads
    it and refuses a downed PC's subsequent actions (sq-playtest 2026-06-07
    barsoom-3 — a dead PC kept full agency for four rounds). The function also
    returns an :class:`IncapacitationEvent` per LETHAL down so the kill-turn
    dispatch can broadcast the player-facing CHARACTER_INCAPACITATED surface
    (death banner / seat lock / re-roll CTA). Full death-clock ticking remains
    ADR-114 Part 2.

Always emits ``encounter.post_resolution_lethality`` — the OTEL lie-detector for
"did anything handle the 0-HP exit". Idempotent: a PC already carrying this
seam's status (tagged ``created_in_encounter == enc.encounter_type``) is skipped,
so the production call site(s) can call it without double-applying.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sidequest.game.encounter import StructuredEncounter
from sidequest.game.session import GameSnapshot
from sidequest.game.status import Status, StatusSeverity
from sidequest.genre.models.pack import GenrePack
from sidequest.protocol.messages import (
    CharacterIncapacitatedMessage,
    CharacterIncapacitatedPayload,
)
from sidequest.telemetry.spans.encounter import (
    SPAN_POST_RESOLUTION_LETHALITY,
    post_resolution_lethality_span,
)
from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

logger = logging.getLogger(__name__)

__all__ = [
    "SPAN_POST_RESOLUTION_LETHALITY",
    "IncapacitationEvent",
    "apply_post_resolution_lethality",
    "build_incapacitated_message",
    "incapacitation_headline",
    "verdict_from_status_text",
]


def verdict_from_status_text(status_text: str) -> str:
    """Recover the lethality verdict from a Downed status this module authored.

    The lethal status text is authored below as ``"Downed — {verdict}
    (mortally wounded)"``. The turn-intake gate re-surfaces the death banner
    from the persisted status (which carries no separate verdict field), so it
    reads the verdict back out of the one place that authored it. Returns ``""``
    for any text that isn't in the authored shape — a total function, never a
    guess (``incapacitation_headline`` then degrades to the generic line)."""
    prefix = f"{_DOWNED_PREFIX} — "
    if not status_text.startswith(prefix):
        return ""
    rest = status_text[len(prefix) :]
    verdict = rest.split(" (", 1)[0].strip()
    return verdict if verdict in _LETHAL_VERDICTS else ""


def incapacitation_headline(character_name: str, verdict: str) -> str:
    """Player-facing one-line death notice for the UI banner.

    Genre-agnostic and unambiguous (SOUL.md Genre Truth: a death must land AS a
    death). The narrator still writes the prose beat from the lethality
    directive; this is the structural banner line, not a replacement for it."""
    if verdict == "dying":
        return f"{character_name} is down and bleeding out."
    if verdict == "dead":
        return f"{character_name} has fallen."
    return f"{character_name} is out of the fight."


def build_incapacitated_message(
    *,
    character_name: str,
    verdict: str,
    status_text: str,
    player_id: str = "",
    can_reroll: bool = True,
) -> CharacterIncapacitatedMessage:
    """Construct the player-facing CHARACTER_INCAPACITATED wire message. Shared by
    the kill-turn dispatch surface and the turn-intake gate so both render the
    same banner."""
    return CharacterIncapacitatedMessage(
        payload=CharacterIncapacitatedPayload(
            character_name=character_name,
            verdict=verdict,
            status_text=status_text,
            headline=incapacitation_headline(character_name, verdict),
            can_reroll=can_reroll,
        ),
        player_id=player_id,
    )


@dataclass(frozen=True)
class IncapacitationEvent:
    """A PC was taken OUT of play by a just-resolved confrontation (LETHAL
    verdict only). Returned by :func:`apply_post_resolution_lethality` so the
    dispatch caller can broadcast the player-facing death surface
    (CHARACTER_INCAPACITATED) at the moment of death — the turn-intake gate
    (``handlers.player_action``) is the durable lock for SUBSEQUENT actions, but
    the kill turn itself needs the proactive notice."""

    actor: str
    verdict: str
    status_text: str
    encounter_type: str
    outcome: str


# Outcomes where the player SIDE lost (a PC may be at 0 HP). A dial-threshold
# opponent_victory with the PC still above 0 HP is naturally skipped by the
# per-PC hp<=0 gate below.
_PC_DOWN_OUTCOMES = frozenset({"opponent_victory", "mutual_destruction"})

# verdicts_on_zero_hp.pc partition (LethalityVerdictKind). dead/dying = the PC is
# out of the fight permanently/until stabilized; everything else is a recoverable
# defeat.
_LETHAL_VERDICTS = frozenset({"dead", "dying"})

# The PC recovers to this floor on a non-lethal defeat: alive but on the brink.
# The "meaningful cost" lives in the Recovering status + the narrator directive
# the LethalityArbiter already ships from the same policy.
_RECOVERY_FLOOR_HP = 1

_RECOVERING_PREFIX = "Recovering"
_DOWNED_PREFIX = "Downed"


def _already_handled(core, *, encounter_type: str) -> bool:
    """True if this seam already stamped a status for *this* encounter on *core*."""
    for status in core.statuses:
        if status.created_in_encounter != encounter_type:
            continue
        if status.text.startswith(_RECOVERING_PREFIX) or status.text.startswith(_DOWNED_PREFIX):
            return True
    return False


def apply_post_resolution_lethality(
    *,
    snapshot: GameSnapshot,
    encounter: StructuredEncounter | None,
    pack: GenrePack | None,
    turn: int,
) -> list[IncapacitationEvent]:
    """Apply the genre lethality policy's mechanical consequence to any PC left at
    0 HP by a just-resolved PC-down confrontation.

    Operates on the *resolved* ``encounter`` object explicitly (the one the caller
    just resolved — not ``snapshot.encounter``, which the dispatch path keeps as a
    separate reference). No-op unless ``encounter`` is resolved with a PC-down
    outcome. Reads ``pack.lethality_policy.verdicts_on_zero_hp.pc`` (loader-
    mandatory) and either recovers the PC to a floor (non-lethal) or flags it
    Downed (lethal). Emits ``encounter.post_resolution_lethality`` per PC handled.
    Idempotent.

    Returns the list of :class:`IncapacitationEvent` for PCs taken OUT of play
    (LETHAL verdict only) so the dispatch caller can broadcast the player-facing
    death surface on the kill turn. Empty on a non-lethal recover, a no-op, or a
    missing policy.
    """
    enc = encounter
    if enc is None or not enc.resolved:
        return []
    if (enc.outcome or "") not in _PC_DOWN_OUTCOMES:
        return []

    policy = pack.lethality_policy if pack is not None else None
    if policy is None:
        # lethality_policy is loader-mandatory (genre/loader.py raises on a
        # missing file). Reaching here means a pack was assembled without one —
        # a pack-authoring bug, not a runtime condition to paper over. Emit a
        # loud span and refuse to invent a verdict (No Silent Fallbacks).
        logger.warning(
            "post_resolution_lethality.no_policy genre=%s outcome=%s — "
            "lethality_policy missing; cannot decide the 0-HP exit",
            snapshot.genre_slug,
            enc.outcome,
        )
        with post_resolution_lethality_span(
            decision="no_policy",
            outcome=enc.outcome or "",
            verdict="",
            actor="",
            hp_before=-1,
            hp_after=-1,
        ):
            pass
        return []

    verdict = policy.verdicts_on_zero_hp.pc
    lethal = verdict in _LETHAL_VERDICTS
    # Only PCs seated on the player side of THIS encounter are casualties of it.
    player_actor_names = {a.name for a in enc.actors if a.side == "player"}

    incapacitations: list[IncapacitationEvent] = []
    for char in snapshot.characters:
        core = char.core
        if core.name not in player_actor_names:
            continue
        if core.hp.current > 0:
            continue
        if _already_handled(core, encounter_type=enc.encounter_type):
            continue

        hp_before = core.hp.current
        if lethal:
            status_text = f"{_DOWNED_PREFIX} — {verdict} (mortally wounded)"
            core.statuses.append(
                Status(
                    text=status_text,
                    severity=StatusSeverity.Scar,
                    created_turn=turn,
                    created_in_encounter=enc.encounter_type,
                    # The durable "this PC is OUT of play" marker — read by the
                    # turn-intake gate (handlers.player_action) so a dead PC's
                    # subsequent actions never reach the narrator, and surfaced
                    # to the UI as CHARACTER_INCAPACITATED. sq-playtest barsoom-3.
                    incapacitating=True,
                )
            )
            decision = "lethal_down"
            incapacitations.append(
                IncapacitationEvent(
                    actor=core.name,
                    verdict=verdict,
                    status_text=status_text,
                    encounter_type=enc.encounter_type,
                    outcome=enc.outcome or "",
                )
            )
            # sq-playtest 2026-06-07 SILENT death-spiral: this status was applied
            # with zero narrator awareness — Groucho sat "Downed — dying" in state
            # while the prose had him crewing a boarding action. The directive is
            # the narrator's only channel for a server-applied mechanical truth.
            snapshot.next_turn_directives.append(
                f"MECHANICAL TRUTH (weave into the narration): {core.name} is DOWN "
                f"at 0 HP — status: {status_text!r}. {core.name} is out of the "
                "action and dying. The narration MUST reflect this; do not "
                f"narrate {core.name} acting, speaking tactically, or fighting."
            )
        else:
            status_text = f"{_RECOVERING_PREFIX} — {verdict}; {policy.default_reversibility}"
            core.hp.current = _RECOVERY_FLOOR_HP
            core.statuses.append(
                Status(
                    text=status_text,
                    severity=StatusSeverity.Wound,
                    created_turn=turn,
                    created_in_encounter=enc.encounter_type,
                )
            )
            decision = "non_lethal_recover"
            snapshot.next_turn_directives.append(
                f"MECHANICAL TRUTH (weave into the narration): {core.name} was "
                f"taken to the brink — now at {core.hp.current} HP with status "
                f"{status_text!r}. Narrate the cost of the defeat."
            )

        logger.info(
            "post_resolution_lethality.applied decision=%s actor=%s verdict=%s "
            "hp_before=%s hp_after=%s encounter=%s outcome=%s",
            decision,
            core.name,
            verdict,
            hp_before,
            core.hp.current,
            enc.encounter_type,
            enc.outcome,
        )
        # op="status_added" → _maybe_persist_encounter_row persists an
        # ENCOUNTER_STATUS_ADDED row: the forensic timeline gains the authoring
        # event for the Downed/Recovering status (the 2026-06-07 timeline had
        # the status in state with no event trail).
        _watcher_publish(
            "state_transition",
            {
                "field": "encounter",
                "op": "status_added",
                "actor": core.name,
                "status": status_text,
                "decision": decision,
                "verdict": verdict,
                "hp_before": hp_before,
                "hp_after": core.hp.current,
                "encounter_type": enc.encounter_type,
                "outcome": enc.outcome or "",
                "source": "post_resolution_lethality",
            },
            component="encounter",
        )

        with post_resolution_lethality_span(
            decision=decision,
            outcome=enc.outcome or "",
            verdict=verdict,
            actor=core.name,
            hp_before=hp_before,
            hp_after=core.hp.current,
        ):
            pass

    return incapacitations
