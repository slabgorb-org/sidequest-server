"""The witnessed_act dispatch subsystem (wry_whimsy political substrate, Plan 2).

Engages when the player commits a publicly-witnessed act that contradicts a
belief-powered authority or shows a population that defiance survives. Applies
the act to the live PoliticalState dials, injects the contradiction into witness
beliefs (ADR-053), emits OTEL per change, and tells the narrator what moved.
"""

from __future__ import annotations

import logging
from typing import Any

from sidequest.agents.subsystems import SubsystemOutput
from sidequest.game.political_engine import (
    PoliticalEvent,
    apply_witnessed_act,
    inject_witnessed_contradiction,
)
from sidequest.protocol.dispatch import NarratorDirective, SubsystemDispatch
from sidequest.telemetry.spans import (
    SPAN_BLOC_DEFIANCE_RAISED,
    SPAN_BLOC_TIPPED,
    SPAN_PREMISE_BELIEF_DRAINED,
    SPAN_PREMISE_COLLAPSED,
    Span,
)

logger = logging.getLogger(__name__)


def _emit_political_events(
    events: list[PoliticalEvent], *, act_id: str, witnesses: list[str], turn: int
) -> None:
    witness_attr = ",".join(witnesses)
    for ev in events:
        if ev.effect == "drained":
            with Span.open(
                SPAN_PREMISE_BELIEF_DRAINED,
                {
                    "premise_id": ev.target_id,
                    "act_id": act_id,
                    "delta": ev.delta,
                    "new_reserve": ev.new_value,
                    "witnesses": witness_attr,
                    "turn": turn,
                },
            ):
                pass
        elif ev.effect in ("coupled", "awakened"):
            with Span.open(
                SPAN_BLOC_DEFIANCE_RAISED,
                {
                    "bloc_id": ev.target_id,
                    "act_id": act_id,
                    "source": ev.effect,
                    "delta": ev.delta,
                    "new_defiance": ev.new_value,
                    "turn": turn,
                },
            ):
                pass
        elif ev.effect == "collapsed":
            with Span.open(
                SPAN_PREMISE_COLLAPSED,
                {
                    "premise_id": ev.target_id,
                    "act_id": act_id,
                    "new_reserve": ev.new_value,
                    "turn": turn,
                },
            ):
                pass
        elif ev.effect == "tipped":
            with Span.open(
                SPAN_BLOC_TIPPED,
                {
                    "bloc_id": ev.target_id,
                    "act_id": act_id,
                    "new_defiance": ev.new_value,
                    "turn": turn,
                },
            ):
                pass


def _summarize(events: list[PoliticalEvent]) -> str:
    # NOTE: switches on the same ev.effect values as _emit_political_events — keep the two in sync.
    parts: list[str] = []
    for ev in events:
        if ev.effect == "drained":
            parts.append(f"belief in {ev.target_id} fell to {ev.new_value}")
        elif ev.effect in ("coupled", "awakened"):
            parts.append(f"{ev.target_id} defiance rose to {ev.new_value}")
        elif ev.effect == "collapsed":
            parts.append(f"{ev.target_id} COLLAPSED — {ev.detail}")
        elif ev.effect == "tipped":
            parts.append(f"{ev.target_id} TIPPED — {ev.detail}")
    return "; ".join(parts)


async def run_witnessed_act_dispatch(
    dispatch: SubsystemDispatch,
    *,
    snapshot: Any,
    pack: Any,
    player_name: str = "",
    npcs: list[Any] | None = None,
) -> SubsystemOutput:
    """Apply a witnessed act to the political layer. Mutates the snapshot dials."""
    params = dispatch.params or {}
    act_id = params.get("act_id") or params.get("act_archetype")
    if not act_id or not isinstance(act_id, str):
        raise ValueError(
            f"witnessed_act dispatch missing params['act_id']; got params={dispatch.params!r}"
        )

    state = getattr(snapshot, "political_state", None)
    if state is None:
        logger.warning(
            "witnessed_act.no_political_state act=%s world=%s — precondition gate should have dropped this",
            act_id,
            getattr(snapshot, "world_slug", "?"),
        )
        return SubsystemOutput(directives=[], data={"error": "no_political_state"})

    world = pack.worlds.get(snapshot.world_slug) if getattr(pack, "worlds", None) else None
    if world is None:
        logger.warning("witnessed_act.world_not_found world=%s act=%s", snapshot.world_slug, act_id)
        return SubsystemOutput(directives=[], data={"error": "world_not_found"})

    premises = list(getattr(world, "premises", None) or [])
    blocs = list(getattr(world, "blocs", None) or [])
    witnesses = [w for w in (params.get("witnesses") or []) if isinstance(w, str)]

    # Spec §5: an act with no witness moves nothing.
    if not witnesses:
        return SubsystemOutput(
            directives=[
                NarratorDirective(
                    kind="must_narrate",
                    payload=(
                        "The act had no witness, so no belief shifted — exposing a "
                        "humbug in an empty room changes nothing."
                    ),
                    visibility=dispatch.visibility,
                )
            ],
            data={"error": "no_witness"},
        )

    # Turn counter for the ledger / OTEL / injected belief turn_learned. The
    # codebase-wide source is turn_manager.interaction (matches the ADR-053
    # sibling scenario_clue_intake.py); GameSnapshot has no `round` field.
    turn = snapshot.turn_manager.interaction
    events = apply_witnessed_act(
        state=state,
        premises=premises,
        blocs=blocs,
        act_id=act_id,
        witnesses=witnesses,
        turn=turn,
    )

    if not events:
        logger.warning(
            "witnessed_act.no_effect act=%s world=%s — act matched no premise/bloc here",
            act_id,
            snapshot.world_slug,
        )
        return SubsystemOutput(
            directives=[
                NarratorDirective(
                    kind="must_narrate",
                    payload=(
                        f"The act ('{act_id}') did not match any standing illusion or "
                        "population here; narrate it, but no political dial moved."
                    ),
                    visibility=dispatch.visibility,
                )
            ],
            data={"error": "no_effect", "act_id": act_id},
        )

    # ADR-053 reuse: seed witness beliefs for every premise this act drained.
    npc_list = list(npcs or getattr(snapshot, "npcs", None) or [])
    premise_by_id = {p.premise_id: p for p in premises}
    drained_ids = {ev.target_id for ev in events if ev.effect == "drained"}
    for pid in drained_ids:
        pdef = premise_by_id.get(pid)
        if pdef is not None:
            inject_witnessed_contradiction(
                npcs=npc_list, witnesses=witnesses, premise=pdef, turn=turn
            )

    _emit_political_events(events, act_id=act_id, witnesses=witnesses, turn=turn)

    return SubsystemOutput(
        directives=[
            NarratorDirective(
                kind="must_narrate",
                payload=(
                    "The political layer moved (this is mechanically real, not color): "
                    + _summarize(events)
                    + ". Narrate the consequence in keeping with what shifted."
                ),
                visibility=dispatch.visibility,
            )
        ],
        data={
            "act_id": act_id,
            "events": [
                {
                    "effect": ev.effect,
                    "target_id": ev.target_id,
                    "delta": ev.delta,
                    "new_value": ev.new_value,
                }
                for ev in events
            ],
        },
    )
