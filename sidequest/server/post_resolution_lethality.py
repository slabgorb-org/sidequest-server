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
    "Downed" Scar status naming the verdict. (Full death-clock ticking +
    incapacitation enforcement of the at-0 PC is ADR-114 Part 2 — routed; this
    seam stops the silent 0/10-with-full-agency state and emits the span.)

Always emits ``encounter.post_resolution_lethality`` — the OTEL lie-detector for
"did anything handle the 0-HP exit". Idempotent: a PC already carrying this
seam's status (tagged ``created_in_encounter == enc.encounter_type``) is skipped,
so the production call site(s) can call it without double-applying.
"""

from __future__ import annotations

import logging

from sidequest.game.encounter import StructuredEncounter
from sidequest.game.session import GameSnapshot
from sidequest.game.status import Status, StatusSeverity
from sidequest.genre.models.pack import GenrePack
from sidequest.telemetry.spans.encounter import (
    SPAN_POST_RESOLUTION_LETHALITY,
    post_resolution_lethality_span,
)

logger = logging.getLogger(__name__)

__all__ = ["SPAN_POST_RESOLUTION_LETHALITY", "apply_post_resolution_lethality"]

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
) -> None:
    """Apply the genre lethality policy's mechanical consequence to any PC left at
    0 HP by a just-resolved PC-down confrontation.

    Operates on the *resolved* ``encounter`` object explicitly (the one the caller
    just resolved — not ``snapshot.encounter``, which the dispatch path keeps as a
    separate reference). No-op unless ``encounter`` is resolved with a PC-down
    outcome. Reads ``pack.lethality_policy.verdicts_on_zero_hp.pc`` (loader-
    mandatory) and either recovers the PC to a floor (non-lethal) or flags it
    Downed (lethal). Emits ``encounter.post_resolution_lethality`` per PC handled.
    Idempotent.
    """
    enc = encounter
    if enc is None or not enc.resolved:
        return
    if (enc.outcome or "") not in _PC_DOWN_OUTCOMES:
        return

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
        return

    verdict = policy.verdicts_on_zero_hp.pc
    lethal = verdict in _LETHAL_VERDICTS
    # Only PCs seated on the player side of THIS encounter are casualties of it.
    player_actor_names = {a.name for a in enc.actors if a.side == "player"}

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
            core.statuses.append(
                Status(
                    text=f"{_DOWNED_PREFIX} — {verdict} (mortally wounded)",
                    severity=StatusSeverity.Scar,
                    created_turn=turn,
                    created_in_encounter=enc.encounter_type,
                )
            )
            decision = "lethal_down"
        else:
            core.hp.current = _RECOVERY_FLOOR_HP
            core.statuses.append(
                Status(
                    text=(f"{_RECOVERING_PREFIX} — {verdict}; {policy.default_reversibility}"),
                    severity=StatusSeverity.Wound,
                    created_turn=turn,
                    created_in_encounter=enc.encounter_type,
                )
            )
            decision = "non_lethal_recover"

        with post_resolution_lethality_span(
            decision=decision,
            outcome=enc.outcome or "",
            verdict=verdict,
            actor=core.name,
            hp_before=hp_before,
            hp_after=core.hp.current,
        ):
            pass
