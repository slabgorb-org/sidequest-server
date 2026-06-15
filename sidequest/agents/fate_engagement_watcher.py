"""Fate honesty lie-detector watcher — Story 116-4 / ADR-144 F2c.

Sibling of :mod:`sidequest.agents.dispatch_engagement_watcher` (and its
improvised-combat detector) for the Fate path. It catches the narrator claiming a
Fate **outcome** — an advantage created, a foe taken out — that the engine state
does not show: convincing prose with zero mechanical backing, the SOUL
"Illusionism" failure mode the OTEL GM panel exists to catch.

The watcher reads **narration vs authoritative state**, never a span buffer
(spans are ephemeral GM-panel signals; the snapshot is the durable truth — the
same discipline as ``detect_improvised_combat``). Two state witnesses this slice:

- ``create_advantage``: the prose claims a new advantage/aspect was created, but
  ``encounter.situation_aspects`` is empty — the engine placed none.
- ``taken_out``: the prose claims a participant is out of the fight, but no
  ``encounter.actors`` entry is ``withdrawn`` — the engine took no one out.

Pure decision (:func:`detect_fate_narration_mismatch`) + thin OTEL-emitting
wrapper (:func:`run_fate_engagement_watcher`), the same split — and the same
**non-fatal** contract — as the dispatch watcher: this runs POST-narration in the
WS turn pipeline, so a crash here must never abort turn delivery (playtest
2026-06-07). Any exception is caught, logged at ERROR, and surfaced as the loud
``dispatch_engagement.watcher.crashed`` span (reused, per CLAUDE.md "Don't
Reinvent"); the turn pipeline continues.

The witnesses are deliberately **conservative** — they fire only on the canonical
Fate claim constructions and err toward under-flagging. A lie-detector that cries
wolf is worse than none: false beeps flood the GM panel and erode trust. Tunable
as findings accrue (the markers are the only knobs).

**Gate (carried Delivery Finding, 116-4):** the witnesses gate on an active,
unresolved ``snapshot.encounter`` only. Distinguishing a Fate-bound encounter
from a native one is not cleanly expressible in pure snapshot state (no
fate-binding flag on the encounter; PCs' fate sheets and the router package are
both absent on the pure-state path the witnesses read), so a native-ruleset
combat turn that narrates "taken out" without setting ``withdrawn`` could
false-positive. Carried for playtest tuning per the 116-4 TEA finding; the
create-advantage witness is inherently Fate-scoped (``situation_aspects`` is a
Fate-only structure) and only the taken-out marker is generic.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from opentelemetry import trace

from sidequest.game.session import GameSnapshot
from sidequest.protocol.dispatch import DispatchPackage
from sidequest.telemetry.spans.dispatch_engagement import (
    dispatch_engagement_watcher_crashed_span,
)
from sidequest.telemetry.spans.fate import fate_narration_mismatch_span

logger = logging.getLogger(__name__)

# Curated create-advantage claim markers — the canonical Fate constructions a
# narrator uses to assert a NEW advantage/aspect was placed. Kept tight: this
# fires a GM-panel beep (observability), never a control-flow block, and the
# empty-``situation_aspects`` gate does the discriminating, so a missed synonym is
# far cheaper than crying wolf. Matched case-insensitively as substrings.
_CREATE_ADVANTAGE_CLAIM_MARKERS: tuple[str, ...] = (
    "create an advantage",
    "creates an advantage",
    "creating an advantage",
    "created an advantage",
    "gain the advantage",
    "gains the advantage",
    "gained the advantage",
    "gains an advantage",
)

# Curated taken-out claim markers — the narrator asserting a participant is out of
# the conflict. Same beep-not-block discipline; the no-``withdrawn``-actor gate
# discriminates. Matched case-insensitively as substrings.
_TAKEN_OUT_CLAIM_MARKERS: tuple[str, ...] = (
    "taken out",
    "out of the fight",
    "out of the conflict",
    "is defeated",
    "are defeated",
)


@dataclass(frozen=True)
class FateNarrationMismatch:
    """One detected narration-vs-state Fate mismatch.

    ``subsystem`` is the claimed outcome kind (``create_advantage`` |
    ``taken_out``), ``claim`` the prose marker(s) that triggered it, and
    ``reason`` why the authoritative state contradicts the claim.
    """

    subsystem: str
    claim: str
    reason: str


def _matched_markers(narration_lower: str, markers: tuple[str, ...]) -> list[str]:
    return [m for m in markers if m in narration_lower]


def _check_create_advantage_claim(narration: str, snapshot: GameSnapshot) -> str | None:
    """Evidence string when the prose claims an advantage was created but the
    engine placed none (``situation_aspects`` empty), else ``None``."""
    encounter = snapshot.encounter
    if encounter is None:
        return None
    hits = _matched_markers(narration.lower(), _CREATE_ADVANTAGE_CLAIM_MARKERS)
    if not hits:
        return None
    if encounter.situation_aspects:
        return None  # an advantage IS in play — the claim is mechanically backed
    return ", ".join(hits[:3])


def _check_taken_out_claim(narration: str, snapshot: GameSnapshot) -> str | None:
    """Evidence string when the prose claims a participant is out of the fight but
    no ``encounter.actors`` entry is ``withdrawn``, else ``None``."""
    encounter = snapshot.encounter
    if encounter is None:
        return None
    hits = _matched_markers(narration.lower(), _TAKEN_OUT_CLAIM_MARKERS)
    if not hits:
        return None
    if any(a.withdrawn for a in encounter.actors):
        return None  # someone IS withdrawn — the claim is mechanically backed
    return ", ".join(hits[:3])


def detect_fate_narration_mismatch(
    *,
    narration: str,
    snapshot: GameSnapshot,
    package: DispatchPackage | None = None,
) -> list[FateNarrationMismatch]:
    """Detect Fate narration-vs-state mismatches without touching OTEL.

    No-op (returns ``[]``) on empty narration or when there is no active,
    unresolved encounter — a resolved encounter's ``situation_aspects`` /
    ``withdrawn`` flags are stale fiction (the same staleness gate
    ``build_fate_projection`` reads). ``package`` is threaded for parity with the
    sibling watchers and future router-aware gating; this slice's witnesses read
    snapshot state only.
    """
    if not narration:
        return []
    encounter = snapshot.encounter
    if encounter is None or encounter.resolved:
        return []

    mismatches: list[FateNarrationMismatch] = []
    create_claim = _check_create_advantage_claim(narration, snapshot)
    if create_claim is not None:
        mismatches.append(
            FateNarrationMismatch(
                subsystem="create_advantage",
                claim=create_claim,
                reason=(
                    "narration claims an advantage was created but "
                    "encounter.situation_aspects is empty — the engine placed no aspect"
                ),
            )
        )
    taken_out_claim = _check_taken_out_claim(narration, snapshot)
    if taken_out_claim is not None:
        mismatches.append(
            FateNarrationMismatch(
                subsystem="taken_out",
                claim=taken_out_claim,
                reason=(
                    "narration claims a participant is out of the fight but no "
                    "encounter actor is withdrawn — the engine took no one out"
                ),
            )
        )
    return mismatches


def run_fate_engagement_watcher(
    *,
    narration: str,
    package: DispatchPackage | None,
    snapshot: GameSnapshot,
    tracer: trace.Tracer | None = None,
) -> None:
    """Run the Fate honesty watcher and emit one ``fate.narration.mismatch`` span
    per detected mismatch.

    **Non-fatal by contract** — identical discipline to
    :func:`sidequest.agents.dispatch_engagement_watcher.run_improvised_combat_watcher`:
    a pure-observability post-narration pass, so any exception is caught, logged,
    and surfaced as the reused ``dispatch_engagement.watcher.crashed`` span rather
    than tearing down WS turn delivery.
    """
    try:
        mismatches = detect_fate_narration_mismatch(
            narration=narration, snapshot=snapshot, package=package
        )
        for m in mismatches:
            fate_narration_mismatch_span(
                subsystem=m.subsystem, claim=m.claim, reason=m.reason, _tracer=tracer
            )
    except Exception as exc:  # noqa: BLE001 — observability must never abort the turn
        logger.error(
            "fate_engagement.watcher_crashed error_type=%s error=%s "
            "(turn pipeline continues; Fate-honesty coverage lost this turn)",
            type(exc).__name__,
            exc,
            exc_info=True,
        )
        with dispatch_engagement_watcher_crashed_span(
            error_type=type(exc).__name__,
            error=str(exc),
            _tracer=tracer,
        ):
            pass


__all__ = [
    "FateNarrationMismatch",
    "detect_fate_narration_mismatch",
    "run_fate_engagement_watcher",
]
