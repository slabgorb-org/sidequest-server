"""Mutation acquisition — MP arithmetic + deterministic rolls + OTEL.

Refusals (cap hit, insufficient MP) return applied=False with a reason,
mirroring StrainResult — the narrator describes the limit, never
improvises past it. Engine misuse (unknown actor) raises loudly.
"""

from __future__ import annotations

from pydantic import BaseModel

from sidequest.mutation.models import MutationCatalog
from sidequest.mutation.rolls import deterministic_roll
from sidequest.mutation.state import CharacterMutationState, MutationState, StigmaRecord
from sidequest.telemetry.spans.awn import (
    awn_mutation_acquired_span,
    awn_mutation_mp_spend_span,
    awn_mutation_refused_span,
    awn_mutation_stigma_span,
)


class AcquireResult(BaseModel):
    model_config = {"extra": "forbid"}

    applied: bool
    actor: str
    mutation_id: str = ""
    roll: int = 0
    mp_delta: int = 0
    mp_remaining: int = 0
    reason: str = ""


def _character(state: MutationState, actor: str) -> CharacterMutationState:
    if actor not in state.characters:
        raise KeyError(f"actor {actor!r} has no mutation state; known: {sorted(state.characters)}")
    return state.characters[actor]


def acquire_random_negative(
    state: MutationState,
    catalog: MutationCatalog,
    *,
    actor: str,
    session_id: str,
    source: str,
) -> AcquireResult:
    cs = _character(state, actor)
    eco = catalog.mp_economy
    if len(cs.negative_ids) >= eco.max_negatives:
        awn_mutation_refused_span(actor=actor, mutation_id="", reason="max_negatives")
        return AcquireResult(
            applied=False,
            actor=actor,
            mp_remaining=cs.mp_remaining,
            reason=f"max_negatives ({eco.max_negatives}) reached",
        )
    # Bounded dedupe re-roll: deterministic because every attempt consumes
    # a persisted sequence number. Cap = table size, then fail loud.
    for _ in range(len(catalog.negatives) * 4):
        state.roll_sequence += 1
        roll = deterministic_roll(
            session_id=session_id,
            actor=actor,
            purpose="negative_d100",
            sequence=state.roll_sequence,
            sides=100,
        )
        nd = catalog.negative_for_roll(roll)
        if nd.id not in cs.negative_ids:
            cs.negative_ids.append(nd.id)
            cs.acquisition_log.append(nd.id)
            cs.mp_remaining += eco.per_negative_mp
            awn_mutation_acquired_span(
                actor=actor,
                mutation_id=nd.id,
                source=source,
                roll=roll,
                mp_delta=eco.per_negative_mp,
                mp_remaining=cs.mp_remaining,
            )
            return AcquireResult(
                applied=True,
                actor=actor,
                mutation_id=nd.id,
                roll=roll,
                mp_delta=eco.per_negative_mp,
                mp_remaining=cs.mp_remaining,
            )
    awn_mutation_refused_span(actor=actor, mutation_id="", reason="dedupe_exhausted")
    raise ValueError(
        f"could not roll a non-duplicate negative for {actor!r}; "
        f"owned={cs.negative_ids}, table={[n.id for n in catalog.negatives]}"
    )


def _positive_spend_cost(
    cs: CharacterMutationState, catalog: MutationCatalog, *, picked: bool, category: str
) -> tuple[int, str]:
    eco = catalog.mp_economy
    if picked:
        return eco.spend_pick_positive, "pick"
    if any(pid.split("/", 1)[0] == category for pid in cs.positive_ids):
        return eco.spend_same_category, "same_category"
    return eco.spend_random_positive, "random"


def acquire_positive(
    state: MutationState,
    catalog: MutationCatalog,
    *,
    actor: str,
    session_id: str,
    source: str,
    mutation_id: str | None = None,
    category: str | None = None,
) -> AcquireResult:
    """Picked (mutation_id given) or random (optionally within category)."""
    cs = _character(state, actor)

    if mutation_id is not None:
        target = catalog.positive_by_id(mutation_id)  # KeyError loud on bad id
        candidates = [target]
        picked = True
        resolved_category = target.category
    else:
        pool = [
            p
            for p in catalog.positives
            if p.id not in cs.positive_ids and (category is None or p.category == category)
        ]
        if not pool:
            awn_mutation_refused_span(actor=actor, mutation_id="", reason="pool_exhausted")
            return AcquireResult(
                applied=False,
                actor=actor,
                mp_remaining=cs.mp_remaining,
                reason=f"pool_exhausted (category={category!r})",
            )
        candidates = sorted(pool, key=lambda p: p.id)
        picked = False
        resolved_category = category or ""

    if picked and mutation_id in cs.positive_ids:
        awn_mutation_refused_span(
            actor=actor, mutation_id=mutation_id or "", reason="already_owned"
        )
        return AcquireResult(
            applied=False,
            actor=actor,
            mutation_id=mutation_id or "",
            mp_remaining=cs.mp_remaining,
            reason="already_owned",
        )

    if not picked:
        state.roll_sequence += 1
        roll = deterministic_roll(
            session_id=session_id,
            actor=actor,
            purpose="positive_pick",
            sequence=state.roll_sequence,
            sides=len(candidates),
        )
        chosen = candidates[roll - 1]
        resolved_category = chosen.category
    else:
        roll = 0
        chosen = candidates[0]

    cost, spend_kind = _positive_spend_cost(cs, catalog, picked=picked, category=resolved_category)
    if cs.mp_remaining < cost:
        awn_mutation_refused_span(actor=actor, mutation_id=chosen.id, reason="insufficient_mp")
        return AcquireResult(
            applied=False,
            actor=actor,
            mutation_id=chosen.id,
            mp_remaining=cs.mp_remaining,
            reason=f"insufficient_mp (need {cost}, have {cs.mp_remaining})",
        )

    cs.mp_remaining -= cost
    cs.positive_ids.append(chosen.id)
    cs.acquisition_log.append(chosen.id)
    awn_mutation_mp_spend_span(
        actor=actor,
        spend_kind=spend_kind,
        cost=cost,
        mp_remaining=cs.mp_remaining,
    )
    awn_mutation_acquired_span(
        actor=actor,
        mutation_id=chosen.id,
        source=source,
        roll=roll,
        mp_delta=-cost,
        mp_remaining=cs.mp_remaining,
    )
    return AcquireResult(
        applied=True,
        actor=actor,
        mutation_id=chosen.id,
        roll=roll,
        mp_delta=-cost,
        mp_remaining=cs.mp_remaining,
    )


def roll_stigma(
    state: MutationState,
    catalog: MutationCatalog,
    *,
    actor: str,
    session_id: str,
    concealable: bool = False,
) -> StigmaRecord | None:
    """Roll d6 body-part x d6 nature x d12 flavor. Concealable costs MP;
    returns None (refusal) when the actor can't afford concealment."""
    cs = _character(state, actor)
    eco = catalog.mp_economy
    if concealable:
        if cs.mp_remaining < eco.concealable_stigma_cost:
            awn_mutation_refused_span(actor=actor, mutation_id="", reason="insufficient_mp_stigma")
            return None
        cs.mp_remaining -= eco.concealable_stigma_cost
        awn_mutation_mp_spend_span(
            actor=actor,
            spend_kind="concealable_stigma",
            cost=eco.concealable_stigma_cost,
            mp_remaining=cs.mp_remaining,
        )
    rolls: list[int] = []
    for purpose, sides in (("stigma_body", 6), ("stigma_nature", 6), ("stigma_flavor", 12)):
        state.roll_sequence += 1
        rolls.append(
            deterministic_roll(
                session_id=session_id,
                actor=actor,
                purpose=purpose,
                sequence=state.roll_sequence,
                sides=sides,
            )
        )
    record = StigmaRecord(
        body_part=catalog.stigma.body_part[rolls[0] - 1],
        nature=catalog.stigma.nature[rolls[1] - 1],
        flavor=catalog.stigma.flavor[rolls[2] - 1],
        concealable=concealable,
    )
    cs.stigma.append(record)
    awn_mutation_stigma_span(
        actor=actor,
        body_part=record.body_part,
        nature=record.nature,
        flavor=record.flavor,
        concealable=concealable,
    )
    return record
