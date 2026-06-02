"""The belief-flow engine for the wry_whimsy political substrate (Plan 2, spec §5).

Pure logic: given the live ``PoliticalState`` dials, the world's content defs,
and one classified witnessed act, mutate the dials and return the ordered list
of events. No I/O, no snapshot, no OTEL — the caller (the dispatch subsystem)
records the ledger-derived spans and the narrator directive. This keeps the
causal math unit-testable in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sidequest.game.political_state import BeliefLedgerEntry, PoliticalState
from sidequest.genre.models.premises import BlocDef, PremiseDef

PoliticalEffect = Literal["drained", "coupled", "awakened", "collapsed", "tipped"]
TargetKind = Literal["premise", "bloc"]

# Spec §4.4 soft conservation: a drained Premise routes this fraction of the
# drain into its propping Blocs' defiance; the remainder dissipates. A tuning
# knob, NOT a hard physics law (the fixed-pool conservation law is a v2 spike).
COUPLING_FRACTION = 0.5


@dataclass(frozen=True)
class PoliticalEvent:
    """One applied change, for OTEL + the narrator directive."""

    effect: PoliticalEffect  # "drained" | "coupled" | "awakened" | "collapsed" | "tipped"
    target_kind: TargetKind  # "premise" | "bloc"
    target_id: str
    delta: int  # signed change (0 for collapse/tip markers)
    new_value: int
    detail: str = ""  # collapse outcome / tipped_outcome for the narrator


def _clamp(value: int) -> int:
    # Dial domain [0, 100] per PremiseDef.belief_reserve / BlocDef.defiance field constraints.
    return max(0, min(100, value))


def apply_witnessed_act(
    *,
    state: PoliticalState,
    premises: list[PremiseDef],
    blocs: list[BlocDef],
    act_id: str,
    witnesses: list[str],
    turn: int,
) -> list[PoliticalEvent]:
    """Apply one witnessed act to the live dials. Mutates ``state`` in place.

    1. Drain every premise whose ``drained_by`` includes ``act_id`` (clamped ≥0),
       and soft-couple ``COUPLING_FRACTION`` of the *authored* ``belief_delta``
       (not the clamped actual drain) into its propping blocs — so a
       near-depleted premise still triggers full coupling.
    2. Awaken every bloc whose ``awakening_acts`` includes ``act_id``.
    3. Final pass: fire ``collapsed`` for any premise at/under its collapse
       threshold and ``tipped`` for any bloc at/over its tipping threshold
       (a single pass so coupling-induced crossings are not missed).

    Callers must validate that ``witnesses`` is non-empty — the "no witness
    moves nothing" rule (spec §5) is enforced at the dispatch layer, not here.
    """
    events: list[PoliticalEvent] = []
    witnesses = list(witnesses)

    def _record(effect: str, kind: str, tid: str, delta: int, new_value: int) -> None:
        state.ledger.append(
            BeliefLedgerEntry(
                turn=turn,
                act_id=act_id,
                target_id=tid,
                target_kind=kind,
                effect=effect,
                delta=delta,
                new_value=new_value,
                witnesses=witnesses,
            )
        )

    # 1. Drain premises + soft-couple propping blocs.
    for pdef in premises:
        drain = next((d for d in pdef.drained_by if d.act == act_id), None)
        if drain is None:
            continue
        pstate = state.premises.get(pdef.premise_id)
        if pstate is None or pstate.collapsed:
            continue
        before = pstate.belief_reserve
        pstate.belief_reserve = _clamp(before - drain.belief_delta)
        applied = pstate.belief_reserve - before  # negative
        if applied:
            events.append(
                PoliticalEvent(
                    "drained", "premise", pdef.premise_id, applied, pstate.belief_reserve
                )
            )
            _record("drained", "premise", pdef.premise_id, applied, pstate.belief_reserve)

        # Coupling keys off the *authored* belief_delta, not the clamped actual
        # drain, so a near-depleted premise still routes full defiance.
        coupled = int(drain.belief_delta * COUPLING_FRACTION)
        if coupled > 0:
            for bloc_id in pdef.propped_by:
                bstate = state.blocs.get(bloc_id)
                if bstate is None or bstate.tipped:
                    continue
                defiance_before = bstate.defiance
                bstate.defiance = _clamp(defiance_before + coupled)
                defiance_applied = bstate.defiance - defiance_before
                if defiance_applied:
                    events.append(
                        PoliticalEvent(
                            "coupled", "bloc", bloc_id, defiance_applied, bstate.defiance
                        )
                    )
                    _record("coupled", "bloc", bloc_id, defiance_applied, bstate.defiance)

    # 2. Awaken blocs whose awakening_acts include the act.
    for bdef in blocs:
        awk = next((a for a in bdef.awakening_acts if a.act == act_id), None)
        if awk is None:
            continue
        bstate = state.blocs.get(bdef.bloc_id)
        if bstate is None or bstate.tipped:
            continue
        before = bstate.defiance
        bstate.defiance = _clamp(before + awk.defiance_delta)
        applied = bstate.defiance - before
        if applied:
            events.append(
                PoliticalEvent("awakened", "bloc", bdef.bloc_id, applied, bstate.defiance)
            )
            _record("awakened", "bloc", bdef.bloc_id, applied, bstate.defiance)

    # 3. Threshold pass (collapse / tip), once, after all dial movement.
    premise_by_id = {p.premise_id: p for p in premises}
    bloc_by_id = {b.bloc_id: b for b in blocs}
    for pid, pstate in state.premises.items():
        pdef = premise_by_id.get(pid)
        if pdef is None or pstate.collapsed:
            continue
        if pstate.belief_reserve <= pdef.collapse.threshold:
            pstate.collapsed = True
            events.append(
                PoliticalEvent(
                    "collapsed", "premise", pid, 0, pstate.belief_reserve, pdef.collapse.outcome
                )
            )
            _record("collapsed", "premise", pid, 0, pstate.belief_reserve)
    for bid, bstate in state.blocs.items():
        bdef = bloc_by_id.get(bid)
        if bdef is None or bstate.tipped:
            continue
        if bstate.defiance >= bdef.tipping_threshold:
            bstate.tipped = True
            events.append(
                PoliticalEvent("tipped", "bloc", bid, 0, bstate.defiance, bdef.tipped_outcome)
            )
            _record("tipped", "bloc", bid, 0, bstate.defiance)

    return events
