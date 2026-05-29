"""Router-vs-engine lie-detector watcher — Story 59-3 / ADR-113.

Post-turn observer that compares what the Intent Router *dispatched* against
what the engines actually engaged on the resulting snapshot. Emits one
``dispatch_engagement.{subsystem}.mismatch`` OTEL span per detected
mismatch so the GM panel surfaces "convincing prose with zero mechanical
backing" turns immediately.

The watcher is a **pure decision** with a thin OTEL-emitting wrapper:

- :func:`detect_dispatch_engagement_mismatch` — pure function, no I/O, no
  tracer touch. Returns :class:`DispatchMismatch` records for callers that
  want to introspect decisions without spinning up an exporter.
- :func:`run_dispatch_engagement_watcher` — calls the pure function and
  emits one span per mismatch. The live integration site
  (``_execute_narration_turn`` in the websocket session handler) calls
  this wrapper.

Replaces the 59-1 self-report-reprompt path
(``confrontation_intent_mismatch_reprompt_failed_span`` + the reprompt
loop in ``_execute_narration_turn``) per memory
``feedback_one_mechanism_per_problem``. The new mechanism is router-
driven, not narrator-driven: the producer is the Intent Router (objective
reality), not the narrator (the suspect under investigation).

Fail-loud discipline (memory ``feedback_no_fallbacks_hard``):
- A malformed dispatch (missing ``type`` / ``actor`` / ``fact_id`` /
  ``npc_name``) is a real router defect — and it is reported as a
  **mismatch span**, the loudest channel available here: the
  ``dispatch_engagement.{subsystem}.mismatch`` span reaches the GM panel
  with evidence naming the missing key. The watcher does NOT raise on it.
  This watcher runs POST-narration in the WS turn pipeline; an uncaught
  ``KeyError`` here propagates to ``ws_endpoint`` and crashes turn
  *delivery* (playtest 2026-05-25: narration succeeded, len=1626, but the
  watcher crash closed the socket → MP UI hung). A crash that prevents the
  span from ever exporting is the *silent-worst* failure mode, not a loud
  one — the corrected contract surfaces the defect louder AND keeps the
  turn deliverable.
- Unknown subsystem names raise :class:`KeyError` via
  :func:`span_name_for_subsystem`.

The watcher does NOT correct, retry, or re-dispatch. It observes and
emits. Correction belongs to a later story (or to the operator reading
the GM panel).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from opentelemetry import trace

from sidequest.game.session import GameSnapshot
from sidequest.protocol.dispatch import DispatchPackage, SubsystemDispatch
from sidequest.telemetry.spans.dispatch_engagement import (
    dispatch_engagement_mismatch_span,
)


@dataclass(frozen=True)
class DispatchMismatch:
    """One detected router-vs-engine mismatch.

    Carries enough information for the wrapper to emit a useful span AND
    for callers that consume the pure function's output to filter /
    aggregate without re-deriving facts.
    """

    subsystem: str
    idempotency_key: str
    dispatched_type: str
    evidence: str


# ---------------------------------------------------------------------------
# Per-subsystem engagement witnesses
#
# Each witness answers: given this dispatch and this post-turn snapshot,
# did the engine engage? Returns ``None`` on engagement, or a short
# evidence string on mismatch. The evidence string is what the GM panel
# shows as the "why" of the lie-detector beep — keep it terse.
#
# Dispatch param key contract (TEA deviation note, RED phase):
#   - confrontation: params["type"]   → encounter.encounter_type
#   - magic_working: params["actor"]  → working_log[*].actor
#   - scenario_clue: params["fact_id"] → scenario_state.discovered_clues
#   - npc_agency:    params["npc_name"] → npc_pool[*].name (story 59-7)
#   - distinctive_detail_hint: always engaged (directive-only, no snapshot dep)
#   - reflect_absence: always engaged (directive-only, no snapshot dep)
# ---------------------------------------------------------------------------


# Evidence string for a dispatch missing its required identifying param.
# A missing key is a router defect the watcher SURFACES (mismatch span),
# not an exception it raises — raising would crash post-narration WS
# delivery (see module docstring). The evidence names the missing key so
# the GM panel shows exactly what the router omitted.
_MALFORMED_EVIDENCE = "malformed dispatch: router omitted required params['{key}'] for {subsystem}"


def _check_confrontation_engaged(dispatch: SubsystemDispatch, snapshot: GameSnapshot) -> str | None:
    if "type" not in dispatch.params:
        return _MALFORMED_EVIDENCE.format(subsystem="confrontation", key="type")
    dispatched_type: str = dispatch.params["type"]
    encounter = snapshot.encounter
    if encounter is None:
        return "snapshot.encounter is None"
    if encounter.encounter_type != dispatched_type:
        return (
            f"snapshot.encounter.encounter_type={encounter.encounter_type!r} "
            f"!= dispatched type={dispatched_type!r}"
        )
    return None


def _check_magic_working_engaged(dispatch: SubsystemDispatch, snapshot: GameSnapshot) -> str | None:
    if "actor" not in dispatch.params:
        return _MALFORMED_EVIDENCE.format(subsystem="magic_working", key="actor")
    actor: str = dispatch.params["actor"]
    magic_state = snapshot.magic_state
    if magic_state is None:
        return "snapshot.magic_state is None (world has no magic config loaded)"
    if not any(r.actor == actor for r in magic_state.working_log):
        return f"no WorkingRecord with actor={actor!r} in magic_state.working_log"
    return None


def _check_scenario_clue_engaged(dispatch: SubsystemDispatch, snapshot: GameSnapshot) -> str | None:
    if "fact_id" not in dispatch.params:
        return _MALFORMED_EVIDENCE.format(subsystem="scenario_clue", key="fact_id")
    fact_id: str = dispatch.params["fact_id"]
    scenario = snapshot.scenario_state
    if scenario is None:
        return "snapshot.scenario_state is None"
    if fact_id not in scenario.discovered_clues:
        return f"fact_id={fact_id!r} not in scenario_state.discovered_clues"
    return None


def _check_npc_agency_engaged(dispatch: SubsystemDispatch, snapshot: GameSnapshot) -> str | None:
    if "npc_name" not in dispatch.params:
        return _MALFORMED_EVIDENCE.format(subsystem="npc_agency", key="npc_name")
    npc_name: str = dispatch.params["npc_name"]
    needle = npc_name.lower()
    # npc_agency resolves against the authored roster (snapshot.npcs) first,
    # then the present-in-scene npc_pool (playtest #C1). The engagement check
    # must consider BOTH or it false-flags a mismatch for every roster NPC
    # (the crew, Old Tam) the subsystem correctly engaged.
    in_roster = any(n.core.name.lower() == needle for n in snapshot.npcs)
    in_pool = any(m.name.lower() == needle for m in snapshot.npc_pool)
    if not (in_roster or in_pool):
        return f"npc_name={npc_name!r} not in snapshot.npcs or snapshot.npc_pool"
    return None


def _check_distinctive_detail_engaged(
    dispatch: SubsystemDispatch, snapshot: GameSnapshot
) -> str | None:
    return None


def _check_reflect_absence_engaged(
    dispatch: SubsystemDispatch, snapshot: GameSnapshot
) -> str | None:
    return None


_DISPATCHED_TYPE_KEY: dict[str, str] = {
    "confrontation": "type",
    "magic_working": "actor",
    "scenario_clue": "fact_id",
    "npc_agency": "npc_name",
    "distinctive_detail_hint": "target",
    "reflect_absence": "addressee_hint",
}


_WITNESSES = {
    "confrontation": _check_confrontation_engaged,
    "magic_working": _check_magic_working_engaged,
    "scenario_clue": _check_scenario_clue_engaged,
    "npc_agency": _check_npc_agency_engaged,
    "distinctive_detail_hint": _check_distinctive_detail_engaged,
    "reflect_absence": _check_reflect_absence_engaged,
}


# ---------------------------------------------------------------------------
# Dispatch traversal — yields every SubsystemDispatch in the package
# (per_player AND cross_player). Watcher must cover both — PvP / cross-
# player dispatches are the same shape and must not slip past silently.
# ---------------------------------------------------------------------------


def _iter_all_dispatches(package: DispatchPackage) -> Iterable[SubsystemDispatch]:
    for pd in package.per_player:
        yield from pd.dispatch
    for ca in package.cross_player:
        yield from ca.dispatch


# ---------------------------------------------------------------------------
# Public API — pure function + OTEL-emitting wrapper
# ---------------------------------------------------------------------------


def detect_dispatch_engagement_mismatch(
    *,
    package: DispatchPackage | None,
    snapshot: GameSnapshot,
) -> list[DispatchMismatch]:
    """Detect router-vs-engine mismatches without touching OTEL.

    Returns one :class:`DispatchMismatch` per dispatch whose subsystem
    engagement witness returned non-None evidence. Quiet turns
    (``package=None`` or empty package) return ``[]``.

    Subsystems whose names are not in :data:`_WITNESSES` are *ignored* —
    not every router subsystem is the watcher's concern. As of story 59-7,
    all six live-path subsystems have witnesses: ``confrontation``,
    ``magic_working``, ``scenario_clue``, ``npc_agency``,
    ``distinctive_detail_hint``, ``reflect_absence``.
    """
    if package is None:
        return []
    mismatches: list[DispatchMismatch] = []
    for dispatch in _iter_all_dispatches(package):
        check = _WITNESSES.get(dispatch.subsystem)
        if check is None:
            continue  # subsystem outside the watcher's vocabulary
        evidence = check(dispatch, snapshot)
        if evidence is None:
            continue
        dispatched_type_key = _DISPATCHED_TYPE_KEY[dispatch.subsystem]
        dispatched_type = str(dispatch.params.get(dispatched_type_key, ""))
        mismatches.append(
            DispatchMismatch(
                subsystem=dispatch.subsystem,
                idempotency_key=dispatch.idempotency_key,
                dispatched_type=dispatched_type,
                evidence=evidence,
            )
        )
    return mismatches


def run_dispatch_engagement_watcher(
    *,
    package: DispatchPackage | None,
    snapshot: GameSnapshot,
    tracer: trace.Tracer | None = None,
) -> None:
    """Run the watcher and emit one OTEL span per detected mismatch.

    No-op when ``package is None`` (the live SDK path between 59-3 ship
    and 59-4 ship — ``turn_context.dispatch_package`` stays None until
    59-4 wires the producer onto the live turn pipeline).
    """
    mismatches = detect_dispatch_engagement_mismatch(package=package, snapshot=snapshot)
    for m in mismatches:
        with dispatch_engagement_mismatch_span(
            subsystem=m.subsystem,
            idempotency_key=m.idempotency_key,
            dispatched_type=m.dispatched_type,
            evidence=m.evidence,
            _tracer=tracer,
        ):
            pass


__all__ = [
    "DispatchMismatch",
    "detect_dispatch_engagement_mismatch",
    "run_dispatch_engagement_watcher",
]
