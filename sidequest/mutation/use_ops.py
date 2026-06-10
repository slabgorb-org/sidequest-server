"""In-play mutation use — ownership, usage limits, Strain, save-vs.

Strain routes through the EXISTING CwnRulesetModule.apply_system_strain
with kind="temporary" and source="mutation:<id>" (plan deviation note 1:
the kind taxonomy is mechanical; provenance rides source). Save-vs uses
the resolver-callable shape codified by innate_v1_cast.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel

from sidequest.game.creature_core import CreatureCore
from sidequest.game.ruleset.cwn import CwnRulesetModule
from sidequest.game.system_strain import StrainResult
from sidequest.genre.models.rules import CwnConfig
from sidequest.mutation.models import MutationCatalog
from sidequest.mutation.state import MutationState, UsageCounter
from sidequest.telemetry.spans.awn import (
    awn_mutation_refused_span,
    awn_mutation_used_span,
)

SaveResult = Literal["success", "fail"]
SaveResolver = Callable[[str, str], SaveResult]


class UseMutationResult(BaseModel):
    model_config = {"extra": "forbid"}

    applied: bool
    actor: str
    mutation_id: str
    reason: str = ""
    strain: StrainResult | None = None
    uses_remaining: int = -1  # -1 = at_will (unlimited); only meaningful when applied=True
    save_stat: str | None = None
    save_result: SaveResult | None = None
    effect: str = ""


def use_mutation(
    *,
    state: MutationState,
    catalog: MutationCatalog,
    module: CwnRulesetModule,
    cfg: CwnConfig | None,
    core: CreatureCore,
    actor: str,
    mutation_id: str,
    target_id: str = "",
    save_resolver: SaveResolver | None = None,
) -> UseMutationResult:
    cs = state.characters.get(actor)
    if cs is None or mutation_id not in cs.positive_ids:
        reason = "not_owned" if cs is not None else "not_owned (actor has no mutation state)"
        awn_mutation_refused_span(actor=actor, mutation_id=mutation_id, reason=reason)
        return UseMutationResult(
            applied=False, actor=actor, mutation_id=mutation_id, reason=reason,
        )

    md = catalog.positive_by_id(mutation_id)

    # Usage limit
    uses_remaining = -1
    counter: UsageCounter | None = None
    if md.usage != "at_will":
        counter = cs.usage.setdefault(mutation_id, UsageCounter(period=md.usage))
        if counter.used >= md.uses_per_period:
            awn_mutation_refused_span(actor=actor, mutation_id=mutation_id, reason="limit_exhausted")
            return UseMutationResult(
                applied=False, actor=actor, mutation_id=mutation_id,
                reason=f"limit_exhausted ({md.usage}: {counter.used}/{md.uses_per_period})",
            )

    # Save-vs needs a resolver BEFORE any cost is paid
    if md.save is not None and md.save.stat is not None and save_resolver is None:
        raise ValueError(
            f"mutation {mutation_id!r} has save.stat={md.save.stat!r} but no "
            f"save_resolver was provided — the production caller must wire one."
        )

    # Strain cost
    strain: StrainResult | None = None
    if md.strain_cost > 0:
        strain = module.apply_system_strain(
            core=core, kind="temporary", amount=md.strain_cost,
            source=f"mutation:{mutation_id}", cfg=cfg,
        )
        if not strain.applied:
            awn_mutation_refused_span(actor=actor, mutation_id=mutation_id, reason="strain_over_max")
            return UseMutationResult(
                applied=False, actor=actor, mutation_id=mutation_id,
                reason=f"strain_over_max ({strain.reason})", strain=strain,
            )

    # Save-vs resolution (cost already paid — AWN: the power fires, the target saves)
    save_stat: str | None = None
    save_result: SaveResult | None = None
    if md.save is not None and md.save.stat is not None:
        assert save_resolver is not None  # guarded above
        save_stat = md.save.stat
        save_result = save_resolver(md.save.stat, target_id)

    if counter is not None:
        counter.used += 1
        uses_remaining = md.uses_per_period - counter.used

    awn_mutation_used_span(
        actor=actor, mutation_id=mutation_id, strain_cost=md.strain_cost,
        uses_remaining=uses_remaining,
        save_stat=save_stat or "", save_result=save_result or "",
    )
    return UseMutationResult(
        applied=True, actor=actor, mutation_id=mutation_id, strain=strain,
        uses_remaining=uses_remaining, save_stat=save_stat, save_result=save_result,
        effect=md.effect,
    )
